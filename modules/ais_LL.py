"""modules/ais_LL.py

Driver low-level para un receptor GPS (NMEA) conectado por puerto serie.

Provee:
- `AISLowLevel` clase con `init`, `open`, `close`, `has_fix`, `parse_nmea`, `get_navigation`, `test`.
- Al ejecutar como script realiza un test funcional completo.
"""

import time
from datetime import datetime
from typing import Optional, Dict, Any, List
import logging
import os
import sys

import serial
from serial.tools import list_ports
import re

# permitir ejecutar este archivo como script desde la raíz del repo
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.log_utils import get_logger


def _nmea_coord_to_decimal(coord: str, hemi: str) -> Optional[float]:
    try:
        if not coord:
            return None
        # coord format: ddmm.mmmm or dddmm.mmmm
        parts = coord.split('.')
        if len(parts) < 2:
            return None
        degrees_len = 2 if len(parts[0]) in (4, 5) and len(parts[0]) - 2 == 2 else (3 if len(parts[0]) - 2 == 3 else 2)
        deg = int(coord[:degrees_len])
        minutes = float(coord[degrees_len:])
        dec = deg + minutes / 60.0
        if hemi in ('S', 'W'):
            dec = -dec
        return dec
    except Exception:
        return None


class AISLowLevel:
    """Driver low-level para GPS/NMEA sobre puerto serie.

    El diseño es intencionalmente simple: busca puertos serie, intenta abrirlos
    con baudios comunes y valida la presencia del equipo al recibir sentencias NMEA.
    """

    def __init__(self, logger_name: str = "ais_LL", dev: bool = False, log_file: Optional[str] = None,
                 preferred_port: Optional[str] = None, scan_window: float = 2.0, wait_for_fix: float = 12.0,
                 show_ports: bool = False):
        """Crear instancia.

        - `dev`: si True, fuerza `DEBUG` en logger; si False usa `INFO`.
        - `log_file`: opcional, ruta o nombre de archivo de log.
        """
        level = logging.DEBUG if dev else logging.INFO
        self.logger = get_logger(logger_name, log_file, level=level)
        self.serial: Optional[serial.Serial] = None
        self.port: Optional[str] = None
        self.baud: Optional[int] = None
        self._buffer: List[str] = []

        # configuración instanciada: preferencia de puerto, ventana de escaneo y espera por fix
        # si no se pasó preferred_port, permitir recoger de la variable de entorno
        self.preferred_port = preferred_port or os.getenv('PREFERRED_PORT')
        if preferred_port is None and self.preferred_port:
            self.logger.debug("Usando PREFERRED_PORT desde env: %s", self.preferred_port)
        # asegurar ventana mínima
        try:
            self.scan_window = max(1.0, float(scan_window)) if scan_window is not None else 1.0
        except Exception:
            self.scan_window = 1.0
        try:
            self.wait_for_fix = float(wait_for_fix)
        except Exception:
            self.wait_for_fix = 12.0
        # show_ports: when True, log detailed per-port info and errors (noisy)
        self.show_ports = bool(show_ports)

        # navigation state
        self.nav: Dict[str, Any] = {
            "lat": None,
            "lon": None,
            "timestamp": None,
            "fix": False,
            "fix_quality": 0,
            "num_sats": 0,
            "hdop": None,
            "satellites_in_view": {},  # prn -> snr
            "used_sats": [],
        }

    def init(self, timeout: float = 5.0, baud_candidates: Optional[List[int]] = None, preferred_port: Optional[str] = None, scan_window: Optional[float] = None) -> bool:
        """Escanea puertos serie y valida la presencia del receptor leyendo NMEA.

        timeout: tiempo máximo en segundos para intentar detectar el dispositivo por puerto.
        baud_candidates: lista de baudios a probar; por defecto [4800, 9600, 38400].
        """
        # prioridad de fuentes: argumento -> atributo de instancia -> variable de entorno
        if preferred_port is not None:
            self.preferred_port = preferred_port
        elif self.preferred_port is None:
            self.preferred_port = os.getenv('PREFERRED_PORT')
            if self.preferred_port:
                self.logger.debug("Usando PREFERRED_PORT desde env: %s", self.preferred_port)

        # The hardware uses 115200 8N1 by design per user's note.
        if baud_candidates is None:
            baud_candidates = [115200]

        retries = 3

        # ventana de escaneo: argumento -> atributo de instancia
        if scan_window is not None:
            try:
                self.scan_window = max(1.0, float(scan_window))
            except Exception:
                self.scan_window = 1.0
        # ensure we have a valid scan_window on the instance
        scan_window = self.scan_window

        ports = list(list_ports.comports())
        if self.show_ports:
            self.logger.debug("Puertos serie detectados: %d", len(ports))
            for idx, p in enumerate(ports):
                try:
                    self.logger.debug("  [%d] device=%s, name=%s, description=%s, hwid=%s, vid=%s, pid=%s, serial_number=%s",
                                      idx, getattr(p, 'device', None), getattr(p, 'name', None), getattr(p, 'description', None),
                                      getattr(p, 'hwid', None), getattr(p, 'vid', None), getattr(p, 'pid', None), getattr(p, 'serial_number', None))
                except Exception:
                    self.logger.debug("  [%d] port: %s", idx, p)
        else:
            # log only a compact summary when not showing per-port details
            self.logger.debug("Puertos serie detectados: %d (use show_ports=True for details)", len(ports))

        if not ports:
            self.logger.info("No se detectaron puertos serie.")
            return False

        # construir lista de puertos a intentar:
        # - ordenar por número de dispositivo (ttyS*, ttyUSB*, ttyACM*) ascendente
        # - si hay preferred_port, moverlo al frente
        def _port_sort_key(p):
            dev = getattr(p, 'device', '') or ''
            m = re.search(r'(?:ttyUSB|ttyACM|ttyS)(\d+)$', dev)
            if m:
                try:
                    return int(m.group(1))
                except Exception:
                    return float('inf')
            return float('inf')

        port_iter = list(ports)
        port_iter.sort(key=_port_sort_key)
        # if preferred specified, try it first
        if self.preferred_port:
            found_idx = None
            for i, p in enumerate(port_iter):
                if getattr(p, 'device', None) == self.preferred_port:
                    found_idx = i
                    break
            if found_idx is None:
                self.logger.warning("Puerto preferido %s no está presente entre los puertos detectados.", self.preferred_port)
            else:
                # move preferred to front
                port_iter.insert(0, port_iter.pop(found_idx))

        end_time_global = time.time() + timeout
        for p in port_iter:
            for b in baud_candidates:
                attempt = 0
                while attempt < retries and time.time() <= end_time_global:
                    attempt += 1
                    try:
                        if self.show_ports:
                            self.logger.debug("Intento %d: probando %s @ %d", attempt, p.device, b)
                        ser = serial.Serial(p.device, baudrate=b, timeout=0.5, bytesize=8, parity='N', stopbits=1)
                        # enviar caracter de wake para sacar al dispositivo de bajo consumo
                        try:
                            ser.write(b'\r')
                            ser.flush()
                            time.sleep(0.05)
                        except Exception:
                            pass

                        start = time.time()
                        while time.time() - start < scan_window:
                            try:
                                raw = ser.readline()
                            except Exception:
                                raw = b''
                            if not raw:
                                continue
                            try:
                                line = raw.decode(errors='ignore').strip()
                            except Exception:
                                line = ''
                            if not line:
                                continue
                            # verificar checksum y tipo
                            if not _nmea_validate_checksum(line):
                                if self.show_ports:
                                    self.logger.debug("Checksum inválido en %s: %s", p.device, line)
                                continue
                            if line.startswith("$") and ("GPRMC" in line or "GPGGA" in line):
                                self.logger.info("Dispositivo NMEA detectado en %s @ %d (intento %d)", p.device, b, attempt)
                                self.serial = ser
                                self.port = p.device
                                self.baud = b
                                self.parse_nmea(line)
                                return True
                        # nothing found this attempt
                        try:
                            ser.close()
                        except Exception:
                            pass
                    except Exception as e:
                        if self.show_ports:
                            self.logger.debug("Error abriendo %s @ %d (intento %d): %s", p.device, b, attempt, e)
                        # small pause between retries
                        time.sleep(0.2)
                        continue

        self.logger.warning("No se detectó receptor NMEA en los puertos serie disponibles. Deinitializando.")
        self.deinit()
        return False

    def deinit(self) -> None:
        """Liberar recursos y resetear estado."""
        self.close()
        self.port = None
        self.baud = None
        self._buffer.clear()
        self.nav = {
            "lat": None,
            "lon": None,
            "timestamp": None,
            "fix": False,
            "fix_quality": 0,
            "num_sats": 0,
            "hdop": None,
            "satellites_in_view": {},
            "used_sats": [],
        }

    def open(self) -> bool:
        """Asegura que el puerto serie esté abierto.
        Devuelve True si está abierto o pudo abrirse."""
        if self.serial and self.serial.is_open:
            return True
        if not self.port or not self.baud:
            self.logger.warning("Puerto o baud no definidos; llamar a init() primero.")
            return False
        try:
            self.serial = serial.Serial(self.port, baudrate=self.baud, timeout=0.5, bytesize=8, parity='N', stopbits=1)
            return True
        except Exception as e:
            self.logger.exception("Error abriendo puerto %s: %s", self.port, e)
            # reintentar 2 veces más
            for i in range(2):
                try:
                    time.sleep(0.2)
                    self.serial = serial.Serial(self.port, baudrate=self.baud, timeout=0.5, bytesize=8, parity='N', stopbits=1)
                    return True
                except Exception:
                    continue
            self.deinit()
            return False

    def close(self) -> None:
        if self.serial:
            try:
                self.serial.close()
            except Exception:
                pass
        self.serial = None

    def has_fix(self) -> bool:
        return bool(self.nav.get("fix", False)) and int(self.nav.get("fix_quality", 0)) > 0

    def read_lines(self, timeout: float = 2.0) -> Optional[List[str]]:
        """Lee líneas NMEA del puerto durante `timeout` segundos y las retorna."""
        lines: List[str] = []
        # Si el puerto no está abierto, intentar hasta 3 veces antes de fallar
        if not self.open():
            for attempt in range(3):
                self.logger.debug("Polling: intento %d para abrir puerto", attempt + 1)
                if self.open():
                    break
                time.sleep(0.2)
            else:
                self.logger.warning("Puerto no disponible tras 3 intentos de polling; devolviendo None")
                self.deinit()
                return None
        end = time.time() + timeout
        while time.time() < end:
            ser = self.serial
            if ser is None:
                break
            try:
                raw = ser.readline()
            except Exception:
                break
            if not raw:
                continue
            try:
                line = raw.decode(errors='ignore').strip()
            except Exception:
                continue
            if line:
                # validar checksum antes de aceptar
                if not _nmea_validate_checksum(line):
                    self.logger.debug("Línea con checksum inválido ignorada: %s", line)
                    continue
                lines.append(line)
                self.parse_nmea(line)
        return lines

    def parse_nmea(self, line: str) -> None:
        """Parsea una línea NMEA básica y actualiza el estado de `self.nav`."""
        if not line or not line.startswith("$"):
            return
        try:
            # remover checksum
            if '*' in line:
                body, _chk = line.split('*', 1)
            else:
                body = line
            parts = body.split(',')
            tag = parts[0][1:]
            if tag in ('GPRMC', 'GNRMC'):
                # $GPRMC,hhmmss.ss,A,lat,NS,lon,EW,sog,cog,ddmmyy,...
                status = parts[2] if len(parts) > 2 else ''
                lat = _nmea_coord_to_decimal(parts[3], parts[4]) if len(parts) > 4 else None
                lon = _nmea_coord_to_decimal(parts[5], parts[6]) if len(parts) > 6 else None
                date_str = parts[9] if len(parts) > 9 else ''
                time_str = parts[1] if len(parts) > 1 else ''
                ts = None
                if date_str and time_str:
                    try:
                        ts = datetime.strptime(date_str + time_str.split('.')[0], '%d%m%y%H%M%S')
                    except Exception:
                        ts = None
                self.nav.update({
                    'lat': lat,
                    'lon': lon,
                    'timestamp': ts,
                    'fix': (status == 'A')
                })

            elif tag in ('GPGGA', 'GNGGA'):
                # $GPGGA,time,lat,NS,lon,EW,fix,num_sats,hdop,altitude,...
                time_str = parts[1] if len(parts) > 1 else ''
                lat = _nmea_coord_to_decimal(parts[2], parts[3]) if len(parts) > 3 else None
                lon = _nmea_coord_to_decimal(parts[4], parts[5]) if len(parts) > 5 else None
                fix_q = int(parts[6]) if len(parts) > 6 and parts[6].isdigit() else 0
                num_sats = int(parts[7]) if len(parts) > 7 and parts[7].isdigit() else 0
                hdop = float(parts[8]) if len(parts) > 8 and parts[8] else None
                self.nav.update({
                    'lat': lat or self.nav.get('lat'),
                    'lon': lon or self.nav.get('lon'),
                    'fix_quality': fix_q,
                    'num_sats': num_sats,
                    'hdop': hdop,
                    'fix': fix_q > 0,
                })

            elif tag in ('GPGSA', 'GNGSA'):
                # $GPGSA,A,3,PRN...,PDOP,HDOP,VDOP
                used = [p for p in parts[3:15] if p]
                hdop = None
                if len(parts) > 16 and parts[16]:
                    try:
                        hdop = float(parts[16])
                    except Exception:
                        hdop = None
                self.nav['used_sats'] = used
                if hdop:
                    self.nav['hdop'] = hdop

            elif tag in ('GPGSV', 'GNGSV'):
                # $GPGSV,#msgs,msg_i,#sv,sv1_prn,sv1_el,sv1_az,sv1_snr,...
                # recorrer en bloques de 4 campos
                try:
                    blocks = parts[4:]
                    for i in range(0, len(blocks), 4):
                        if i + 3 < len(blocks):
                            prn = blocks[i]
                            snr = blocks[i + 3]
                            if prn:
                                try:
                                    self.nav['satellites_in_view'][prn] = int(snr) if snr and snr.isdigit() else None
                                except Exception:
                                    self.nav['satellites_in_view'][prn] = None
                except Exception:
                    pass

        except Exception as e:
            self.logger.debug("Error parseando NMEA: %s -- %s", e, line)

    def get_navigation(self) -> Dict[str, Any]:
        """Retorna un diccionario con la navegación mínima esperada por módulos superiores."""
        return {
            'lat': self.nav.get('lat'),
            'lon': self.nav.get('lon'),
            'timestamp': self.nav.get('timestamp'),
            'fix': self.nav.get('fix', False),
            'fix_quality': self.nav.get('fix_quality', 0),
            'num_sats': self.nav.get('num_sats', 0),
            'hdop': self.nav.get('hdop'),
            'satellites_in_view': dict(self.nav.get('satellites_in_view', {})),
            'used_sats': list(self.nav.get('used_sats', [])),
        }

    def test(self, wait_for_fix: Optional[float] = None) -> Dict[str, Any]:
        """Realiza un test funcional: abre puerto, lee NMEA y espera fix si es necesario.

        Retorna un dict con resultados básicos.
        """
        result: Dict[str, Any] = {"port_opened": False, "device_present": False, "has_fix": False}
        # if already initialized/open, avoid re-scanning ports
        wf = wait_for_fix if wait_for_fix is not None else getattr(self, 'wait_for_fix', 10.0)
        if self.serial and getattr(self.serial, 'is_open', False):
            ok = True
        else:
            ok = self.init(timeout=max(5.0, wf))
        result['device_present'] = ok
        if not ok:
            return result
        result['port_opened'] = self.open()
        # leer y actualizar estado
        start = time.time()
        wf = wait_for_fix if wait_for_fix is not None else getattr(self, 'wait_for_fix', 10.0)
        while time.time() - start < wf:
            lines = self.read_lines(timeout=1.0)
            if lines is None:
                self.logger.warning("Puerto caído durante test funcional; abortando test.")
                result['port_opened'] = False
                result['device_present'] = False
                return result
            if self.has_fix():
                result['has_fix'] = True
                break
        result.update(self.get_navigation())
        self.logger.info("Test result: %s", result)
        return result


def _nmea_validate_checksum(line: str) -> bool:
    """Valida el checksum NMEA de la línea (si incluye '*XX').

    Retorna True si no hay checksum (no estricto) o si el checksum coincide.
    """
    try:
        if '*' not in line:
            # si no hay checksum, considerarlo inválido para esta aplicación
            return False
        body, chk = line.strip().split('*', 1)
        # eliminar inicio '$'
        if body.startswith('$'):
            body = body[1:]
        calc = 0
        for c in body:
            calc ^= ord(c)
        try:
            chk_int = int(chk.strip(), 16)
        except Exception:
            return False
        return calc == chk_int
    except Exception:
        return False
    # end of _nmea_validate_checksum


if __name__ == '__main__':
    # Test funcional cuando se ejecuta como script
    preferred = os.getenv('PREFERRED_PORT')
    try:
        scan_window = float(os.getenv('SCAN_WINDOW', '2.0'))
    except Exception:
        scan_window = 2.0
    try:
        wait_for_fix = float(os.getenv('WAIT_FOR_FIX', '12.0'))
    except Exception:
        wait_for_fix = 12.0

    ll = AISLowLevel(dev=True, preferred_port=preferred, scan_window=scan_window, wait_for_fix=wait_for_fix)
    ll.logger.info("Script start: preferred=%s scan_window=%s wait_for_fix=%s", ll.preferred_port, ll.scan_window, ll.wait_for_fix)

    ok = ll.init(timeout=max(5, wait_for_fix))
    import json
    if not ok:
        ll.logger.error("init() no detectó dispositivo en el puerto preferido/escaneados.")
        print(json.dumps({"port_opened": False, "device_present": False, "has_fix": False}, indent=2))
    else:
        res = ll.test(wait_for_fix=wait_for_fix)
        print(json.dumps(res, indent=2, default=str))