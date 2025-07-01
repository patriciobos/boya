import serial
import time
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.log_utils import get_logger

class IridiumLowLevel:
    def __init__(self, port=None, baudrate=19200):
        """Inicializa el acceso al módem Iridium a través del puerto serie (sin abrir el puerto)."""
        self.port = port  # None por defecto, se setea si se detecta
        self.baudrate = baudrate
        self.serial_port = None
        self.logger = get_logger("iridium_LL")
        

    def init(self):
        """Escanea los puertos serie ttyS0-ttyS6 y abre el primero donde responde el módem Iridium. Devuelve True/False."""
        found = False
        for i in range(7):
            port_name = f"/dev/ttyS{i}"
            try:
                ser = serial.Serial(port_name, self.baudrate, timeout=1)
                self.logger.info(f"Intentando abrir {port_name}...")
                ser.write(b'AT\r\n')
                time.sleep(1)
                resp = ser.read_all()
                if resp and b'OK' in resp:
                    self.serial_port = ser
                    self.port = port_name
                    self.logger.info(f"Módem Iridium detectado en {port_name}")
                    found = True
                    self._log_device_info()
                    break
                ser.close()
            except Exception as e:
                self.logger.info(f"Puerto {port_name} no disponible o sin módem: {e}")
        if not found:
            self.logger.error("No se pudo encontrar el módem Iridium en los puertos ttyS0-ttyS6.")
            self.serial_port = None
            self.port = None
        return found

    def _log_device_info(self):
        """Consulta modelo y versión de firmware y lo registra en el log."""
        try:
            modelo_resp = self.send_command("AT+CGMM",2)
            version_resp = self.send_command("AT+CGMR",2)
            modelo = modelo_resp['payload'] if modelo_resp else ''
            version = version_resp['payload'] if version_resp else ''
            self.logger.info(f"Modelo del módem: {modelo}")
            self.logger.info(f"Versiones de firmware:\n{version}")
        except Exception as e:
            self.logger.error(f"Error al obtener modelo o versión: {e}")


    def send_command(self, command: str, timeout: float = 1.0):
        """
        Envía un comando AT y devuelve un dict con:
            - 'echo': eco del comando (str)
            - 'payload': respuesta útil (str)
            - 'status': 'OK', 'ERROR' o ''
        Si la respuesta es vacía, devuelve None y loguea el timeout.
        """
        import time
        if self.serial_port is None or self.port is None:
            self.logger.error("Puerto no abierto o módem no detectado.")
            return None
        try:
            self.serial_port.reset_input_buffer()
            self.serial_port.write((command + "\r\n").encode())
            response_bytes = b''
            start = time.time()
            status = ''
            while time.time() - start < timeout:
                chunk = self.serial_port.read(256)
                if chunk:
                    response_bytes += chunk
                    if b'OK' in response_bytes:
                        status = 'OK'
                        break
                    if b'ERROR' in response_bytes:
                        status = 'ERROR'
                        break
                else:
                    time.sleep(0.01)
            elapsed = time.time() - start
            if not response_bytes:
                self.logger.warning(f"[send_command] Timeout ({timeout}s) expirado sin respuesta para '{command}'")
                return None
            response = response_bytes.decode('utf-8', errors='replace')
            # Separar líneas y limpiar
            lines = [line.strip() for line in response.splitlines() if line.strip()]
            echo = lines[0] if lines and lines[0] == command else ''
            # Buscar status en las últimas líneas
            status_line = ''
            if lines and lines[-1] in ('OK', 'ERROR'):
                status_line = lines[-1]
                payload_lines = lines[1:-1] if echo else lines[:-1]
            else:
                payload_lines = lines[1:] if echo else lines[:]
            payload = '\n'.join(payload_lines) if payload_lines else ''
            # Log de depuración con tiempos
            self.logger.info(f"[send_command] Comando: {command} | Tiempo de respuesta: {elapsed:.3f}s | Eco: '{echo}' | Status: '{status or status_line}' | Payload: '{payload}'")
            #self.logger.info(f"Respuesta del módem cruda: {response_bytes}")
            #self.logger.info(f"Respuesta del módem: {response}")
            return {'echo': echo, 'payload': payload, 'status': status or status_line}
        except Exception as e:
            self.logger.error(f"Error al enviar el comando {command}: {e}")
            return None

    def close(self):
        """Cierra el puerto serie."""
        if self.serial_port and self.serial_port.is_open:
            self.serial_port.close()
            self.logger.info("Puerto serie cerrado.")
        self.port = None

    def full_test(self) -> tuple[bool, dict]:
        """
        Realiza un test completo del módem Iridium.
        Devuelve (resultado_global, detalles_dict)
        """
        detalles = {}
        resultado_global = True

        # 1. Verificar inicialización y conexión
        detalles["inicializado"] = self.serial_port is not None and self.serial_port.is_open and self.port is not None
        if not detalles["inicializado"]:
            detalles["error_init"] = "No inicializado o sin conexión serie."
            self.logger.error("[full_test] No inicializado o sin conexión serie.")
            return False, detalles

        # 2. Comunicación básica: comando AT
        try:
            response = self.send_command("AT")
            if response is None:
                detalles["respuesta_AT"] = None
                detalles["ok_AT"] = False
                self.logger.info(f"[full_test] respuesta_AT: <timeout>")
                self.logger.info(f"[full_test] ok_AT: False")
                self.logger.error("[full_test] El módem no respondió a AT (timeout).")
                resultado_global = False
            else:
                detalles["respuesta_AT"] = response['status']  # Solo status, el payload de AT es vacío
                detalles["ok_AT"] = response['status'] == 'OK'
                self.logger.info(f"[full_test] status_AT: {response['status']}")
                self.logger.info(f"[full_test] ok_AT: {detalles['ok_AT']}")
                if not detalles["ok_AT"]:
                    self.logger.error("[full_test] El módem no respondió OK a AT.")
                    resultado_global = False
        except Exception as e:
            self.logger.error(f"[full_test] Error en comunicación AT: {e}")
            detalles["error_AT"] = str(e)
            resultado_global = False

        # 3. Permisos de escritura en log
        try:
            testfile = "iridium_test_perm.txt"
            with open(testfile, "w") as f:
                f.write("test")
            os.remove(testfile)
            detalles["permiso_fs"] = True
            self.logger.info(f"[full_test] permiso_fs: True")
        except Exception as e:
            self.logger.error(f"[full_test] No hay permisos de escritura en cwd: {e}")
            detalles["permiso_fs"] = False
            self.logger.info(f"[full_test] permiso_fs: False")
            resultado_global = False

        # 4. Chequeo de dependencias
        try:
            import serial
            detalles["serial"] = True
            self.logger.info(f"[full_test] serial: True")
        except ImportError:
            self.logger.error("[full_test] pyserial no está instalado.")
            detalles["serial"] = False
            self.logger.info(f"[full_test] serial: False")
            resultado_global = False

        # 5. Espacio en disco
        try:
            statvfs = os.statvfs(".")
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

        self.logger.info(f"[full_test] Resultado global: {resultado_global}")
        return resultado_global, detalles
    
    def check_status(self) -> dict:
        """Consulta y loguea el estado general del módem: señal, red, antena y buzón SBD."""
        estado = {}

        if not self.serial_port or not self.serial_port.is_open:
            self.logger.error("No se puede consultar el estado: puerto no abierto.")
            return {"error": "Puerto no abierto"}

        # RSSI
        try:
            rssi_resp = self.send_command("AT+CSQ")
            rssi = rssi_resp['payload'] if rssi_resp else ''
            estado["csq"] = rssi
            self.logger.info(f"[check_status] Intensidad de señal (CSQ): {rssi}")
        except Exception as e:
            estado["csq"] = f"Error: {e}"
            self.logger.error(f"[check_status] Error al consultar CSQ: {e}")

        # Registro en red
        try:
            creg_resp = self.send_command("AT+CREG?")
            creg = creg_resp['payload'] if creg_resp else ''
            estado["creg"] = creg
            self.logger.info(f"[check_status] Registro en red (CREG): {creg}")
        except Exception as e:
            estado["creg"] = f"Error: {e}"
            self.logger.error(f"[check_status] Error al consultar CREG: {e}")

        # Estado de antena (si está soportado)
        try:
            ant_resp = self.send_command("AT+ANTST")
            ant = ant_resp['payload'] if ant_resp else ''
            estado["antena"] = ant
            self.logger.info(f"[check_status] Estado de antena (ANTST): {ant}")
        except Exception as e:
            estado["antena"] = f"Error: {e}"
            self.logger.warning(f"[check_status] No se pudo consultar ANTST: {e}")

        # Estado de buzón SBD
        try:
            sbdix_resp = self.send_command("AT+SBDIX", timeout=7.0)
            sbdix = sbdix_resp['payload'] if sbdix_resp else ''
            estado["sbdix"] = sbdix
            self.logger.info(f"[check_status] Estado del buzón SBD (SBDIX): {sbdix}")
        except Exception as e:
            estado["sbdix"] = f"Error: {e}"
            self.logger.error(f"[check_status] Error al consultar SBDIX: {e}")

        return estado


if __name__ == "__main__":

    print("Inicializando módem Iridium...")
    modem = IridiumLowLevel()
    if modem.init() and modem.serial_port and modem.serial_port.is_open:
        print("Init [OK]")
        print("Chequeando estado...")
        status = modem.check_status()
        for clave, valor in status.items():
            print(f"{clave}: {valor}")
        print("Ejecutando batería de tests...")
        resultado, detalles = modem.full_test()
        if resultado:
            print("Tests: OK")
        else:
            print("Tests: ERROR")
            print("Detalles de fallos:")
            for clave, valor in detalles.items():
                if clave.startswith("error") or valor is False:
                    print(f" - {clave}: {valor}")
        modem.close()
        print("Recursos liberados correctamente.")
    else:
        print("No se pudo inicializar el módem Iridium.")

