"""
Módulo de bajo nivel para controlar el anemómetro Windsonic mediante puerto serie.
Permite inicialización, configuración, adquisición de datos espaciados 1 segundo, y test funcional.
Las adquisiciones se guardan en archivos de texto con timestamp en la carpeta "windsonic".
"""

import os
import serial
import serial.tools.list_ports
import threading
import datetime
import time
from modules.log_utils import get_logger

STX = '\x02'
ETX = '\x03'

class WindsonicLowLevel:
    """
    Controlador de bajo nivel para el anemómetro Windsonic.
    """
    def __init__(self):
        """Establece entorno, logger y variables internas."""
        self.serial_connection = None
        self.samples = 10
        self.spacing = 1
        self.identification = 'Q'
        self.acquisition_thread = None
        self.is_acquiring = False
        self.last_acquisition_ok = False

        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.output_dir = os.path.join(base_dir, "windsonic")
        os.makedirs(self.output_dir, exist_ok=True)

        #self.log_file = os.path.join(base_dir, "..", "logs", "windsonic_LL.log")
        self.logger = get_logger("windsonic_LL")

    def config(self, samples=10, spacing=1):
        """Configura cantidad de muestras y espaciamiento entre adquisiciones."""
        self.samples = samples
        self.spacing = spacing

    def init(self) -> bool:
        """Escanea puertos serie para encontrar y conectar el anemómetro."""
        #manual_ports = [f"/dev/ttyS{i}" for i in range(6)]
        ports = [p.device for p in serial.tools.list_ports.comports()]
        #all_ports = manual_ports + [p for p in ports if p not in manual_ports]

        for port_name in ports:
            if not os.path.exists(port_name):
                self.logger.info(f"Puerto {port_name} no existe, se omite.")
                continue
            try:
                self.logger.info(f"Intentando abrir puerto {port_name}...")
                ser = serial.Serial(port_name, 9600, timeout=1)
                ser.write((self.identification + '?').encode())
                ser.write(self.identification.encode())
                response = ser.readline().decode(errors='ignore').strip()
                print(f"Respuesta del anemómetro: {response}") #FIXME: sacar este print
            
                if self.verify_checksum(response):
                    fields = response[1:-1].split(',')
                    if len(fields) >= 5 and fields[0] == self.identification:
                        self.serial_connection = ser
                        self.logger.info(f"Anemómetro detectado en {port_name}")
                        self.config()  # cargar configuración por defecto
                        return True
            except Exception as e:
                self.logger.warning(f"Error en puerto {port_name}: {e}")
        self.logger.error("No se pudo encontrar el anemómetro Windsonic.")
        return False

    def deinit(self) -> bool:
        """Libera los recursos y cierra la conexión serie."""
        try:
            if self.serial_connection and self.serial_connection.is_open:
                self.serial_connection.close()
                self.logger.info("Conexión serie cerrada.")
            return True
        except Exception as e:
            self.logger.warning(f"Error al cerrar conexión: {e}")
            return False
        finally:
            self.serial_connection = None

    from typing import Optional

    def acquire(self, num_acq: Optional[int] = None) -> bool:
        """Inicia adquisición de num_acq muestras espaciadas self.spacing segundos."""
        if not self.serial_connection or not self.serial_connection.is_open:
            self.logger.warning("Conexión serie no inicializada.")
            return False
        if num_acq is None:
            num_acq = self.samples
        self.is_acquiring = True
        self.last_acquisition_ok = False
        self.acquisition_thread = threading.Thread(target=self._acquisition_loop, args=(int(num_acq),), daemon=True)
        self.acquisition_thread.start()
        return True

    def _acquisition_loop(self, num_acq: int):
        date_str = datetime.datetime.now().strftime('%Y%m%d')
        filename = os.path.join(self.output_dir, f"mediciones_{date_str}.txt")
        acquired = 0
        try:
            for i in range(num_acq):
                if not self.serial_connection or not self.serial_connection.is_open:
                    break
                self.serial_connection.write(self.identification.encode())
                data = self.serial_connection.readline().decode(errors='ignore').strip()
                if self.verify_checksum(data):
                    parsed = self.parse_data(data)
                    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    with open(filename, 'a') as f:
                        f.write(f"{timestamp} - {parsed}\n")
                    acquired += 1
                else:
                    self.logger.warning("Respuesta inválida o checksum incorrecto.")
                time.sleep(self.spacing)
            if acquired == num_acq:
                self.logger.info(f"Adquisición finalizada: {acquired} muestras adquiridas correctamente.")
                self.last_acquisition_ok = True
            else:
                self.logger.error(f"Error: solo {acquired} de {num_acq} muestras adquiridas.")
                self.last_acquisition_ok = False
        except Exception as e:
            self.logger.error(f"Error durante adquisición: {e}")
            self.last_acquisition_ok = False
        finally:
            self.is_acquiring = False

    def is_acquisition_done(self) -> tuple[bool, bool]:
        """Indica si finalizó la adquisición y si fue exitosa."""
        done = not self.is_acquiring and (self.acquisition_thread is None or not self.acquisition_thread.is_alive())
        return done, self.last_acquisition_ok

    def test(self) -> bool:
        """Verifica si hay conexión activa con el anemómetro."""
        return self.serial_connection is not None and self.serial_connection.is_open

    def verify_checksum(self, data):
        """Verifica el checksum XOR entre <STX> y <ETX> según protocolo Gill."""
        stx_index = data.find('\x02') + 1
        etx_index = data.find('\x03')
        if stx_index == 0 or etx_index == -1 or etx_index <= stx_index:
            return False
        data_to_check = data[stx_index:etx_index]
        try:
            checksum_received = int(data[etx_index + 1:], 16)
        except ValueError:
            return False
        checksum_calculated = 0
        for char in data_to_check:
            checksum_calculated ^= ord(char)
        return checksum_calculated == checksum_received

    def parse_data(self, data):
        """Extrae los campos entre <STX> y <ETX> de una cadena recibida."""
        stx_index = data.find('\x02') + 1
        etx_index = data.find('\x03')
        if stx_index == 0 or etx_index == -1 or etx_index <= stx_index:
            return None
        return data[stx_index:etx_index]

    def full_test(self) -> tuple[bool, dict]:
        """
        Realiza un test completo del anemómetro Windsonic.
        Devuelve (resultado_global, detalles_dict)
        No cierra ni modifica el estado de la conexión serie.
        """
        detalles = {}
        resultado_global = True

        # 1. Verificar inicialización y conexión
        detalles["inicializado"] = self.serial_connection is not None and getattr(self.serial_connection, 'is_open', False)
        self.logger.info(f"[full_test] inicializado: {detalles['inicializado']}")
        if not detalles["inicializado"]:
            detalles["error"] = "No inicializado o sin conexión serie."
            self.logger.error("[full_test] No inicializado o sin conexión serie.")
            return False, detalles

        # 2. Comunicación básica
        try:
            if self.serial_connection is not None and getattr(self.serial_connection, 'is_open', False):
                self.serial_connection.write(self.identification.encode())
                response = self.serial_connection.readline().decode(errors='ignore').strip()
                detalles["respuesta_ident"] = response
                detalles["checksum_ok"] = self.verify_checksum(response)
                self.logger.info(f"[full_test] comunicacion: {detalles['checksum_ok']}")
                if not detalles["checksum_ok"]:
                    self.logger.error("[full_test] Respuesta de identificación inválida o checksum incorrecto.")
                    resultado_global = False
            else:
                detalles["respuesta_ident"] = None
                detalles["checksum_ok"] = False
                self.logger.error("[full_test] Comunicación básica: conexión no abierta.")
                self.logger.info(f"[full_test] comunicacion: False")
                resultado_global = False
        except Exception as e:
            self.logger.error(f"[full_test] Error en comunicación básica: {e}")
            detalles["comunicacion"] = False
            self.logger.info(f"[full_test] comunicacion: False")
            resultado_global = False
        else:
            detalles["comunicacion"] = True

        # 3. Prueba de adquisición real
        test_samples = 2
        date_str = datetime.datetime.now().strftime('%Y%m%d')
        filename = os.path.join(self.output_dir, f"mediciones_{date_str}.txt")
        try:
            # Contar líneas antes
            prev_lines = 0
            if os.path.exists(filename):
                with open(filename, 'r') as f:
                    prev_lines = sum(1 for _ in f)
            ok = self.acquire(test_samples)
            detalles["adquisicion_lanzada"] = ok
            self.logger.info(f"[full_test] adquisicion_lanzada: {ok}")
            if not ok:
                self.logger.error("[full_test] No se pudo lanzar adquisición de prueba.")
                resultado_global = False
            else:
                # Esperar a que termine
                timeout = 10
                waited = 0
                while not self.is_acquisition_done()[0] and waited < timeout:
                    time.sleep(0.5)
                    waited += 0.5
                done, success = self.is_acquisition_done()
                detalles["adquisicion_done"] = done
                detalles["adquisicion_ok"] = success
                self.logger.info(f"[full_test] adquisicion_done: {done}")
                self.logger.info(f"[full_test] adquisicion_ok: {success}")
                # Contar líneas después
                new_lines = 0
                if os.path.exists(filename):
                    with open(filename, 'r') as f:
                        lines = f.readlines()
                        new_lines = len(lines) - prev_lines
                        detalles["lineas_nuevas"] = new_lines
                        self.logger.info(f"[full_test] lineas_nuevas: {new_lines}")
                        if new_lines < test_samples:
                            self.logger.error(f"[full_test] Solo {new_lines} líneas nuevas tras adquisición.")
                            resultado_global = False
                        # Verificar formato de línea
                        if new_lines > 0:
                            formato_ok = all("-" in l for l in lines[-new_lines:])
                            detalles["formato_linea_ok"] = formato_ok
                            self.logger.info(f"[full_test] formato_linea_ok: {formato_ok}")
                        else:
                            detalles["formato_linea_ok"] = False
                            self.logger.info(f"[full_test] formato_linea_ok: False")
                else:
                    detalles["lineas_nuevas"] = 0
                    detalles["formato_linea_ok"] = False
                    self.logger.info(f"[full_test] lineas_nuevas: 0")
                    self.logger.info(f"[full_test] formato_linea_ok: False")
                    resultado_global = False
                if not success:
                    self.logger.error("[full_test] La adquisición de prueba no fue exitosa.")
                    resultado_global = False
        except Exception as e:
            self.logger.error(f"[full_test] Error durante adquisición de prueba: {e}")
            detalles["adquisicion_exception"] = str(e)
            self.logger.info(f"[full_test] adquisicion_exception: {e}")
            resultado_global = False

        # 4. Permisos de escritura
        try:
            testfile = os.path.join(self.output_dir, "test_perm.txt")
            with open(testfile, "w") as f:
                f.write("test")
            os.remove(testfile)
            detalles["permiso_fs"] = True
            self.logger.info(f"[full_test] permiso_fs: True")
        except Exception as e:
            self.logger.error(f"[full_test] No hay permisos de escritura en windsonic/: {e}")
            detalles["permiso_fs"] = False
            self.logger.info(f"[full_test] permiso_fs: False")
            resultado_global = False

        # 5. Espacio en disco
        try:
            statvfs = os.statvfs(self.output_dir)
            espacio_libre = statvfs.f_frsize * statvfs.f_bavail
            detalles["espacio_libre_bytes"] = espacio_libre
            self.logger.info(f"[full_test] espacio_libre_bytes: {espacio_libre}")
            if espacio_libre < 1 * 1024 * 1024:  # 1 MB
                self.logger.error("[full_test] Espacio en disco insuficiente (<1MB).")
                resultado_global = False
        except Exception as e:
            self.logger.error(f"[full_test] Error verificando espacio en disco: {e}")
            detalles["espacio_libre_bytes"] = 0
            self.logger.info(f"[full_test] espacio_libre_bytes: 0")
            resultado_global = False

        # 6. Estado del hilo de adquisición
        hilo_vivo = self.acquisition_thread.is_alive() if self.acquisition_thread else False
        detalles["hilo_adquisicion_vivo"] = hilo_vivo
        self.logger.info(f"[full_test] hilo_adquisicion_vivo: {hilo_vivo}")
        if hilo_vivo:
            self.logger.error("[full_test] El hilo de adquisición sigue vivo tras la prueba.")
            resultado_global = False

        # 7. Chequeo de dependencias
        try:
            import serial
            detalles["serial"] = True
            self.logger.info(f"[full_test] serial: True")
        except ImportError:
            self.logger.error("[full_test] pyserial no está instalado.")
            detalles["serial"] = False
            self.logger.info(f"[full_test] serial: False")
            resultado_global = False

        # (NO deinit aquí, no se cierra la conexión ni se modifica el estado global)

        self.logger.info(f"[full_test] Resultado global: {resultado_global}")
        return resultado_global, detalles

if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.append(str(Path(__file__).resolve().parent.parent))

    print("Ejecutando prueba de conexión con Windsonic...")
    w = WindsonicLowLevel()
    if w.init():
        print("Conexión establecida. Realizando adquisición de prueba...")
        w.full_test()
        if w.acquire(3):
            while not w.is_acquisition_done()[0]:
                time.sleep(0.5)
            print("Adquisición finalizada.")
        else:
            print("Error al iniciar adquisición.")
        w.deinit()
    else:
        print("No se pudo establecer conexión con el anemómetro.")
