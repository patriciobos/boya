import serial
import time
import logging

class IridiumLowLevel:
    def __init__(self, port='/dev/ttyUSB0', baudrate=19200):
        """Inicializa el acceso al módem Iridium a través del puerto serie."""
        self.port = port
        self.baudrate = baudrate
        self.serial_port = None
        self.logger = logging.getLogger("IridiumLowLevel")
        self.logger.setLevel(logging.INFO)
        self._open_port()

    def _open_port(self):
        """Abre el puerto serie para el módem Iridium."""
        try:
            self.serial_port = serial.Serial(self.port, self.baudrate, timeout=1)
            self.logger.info(f"Conectado al puerto {self.port} a {self.baudrate} baudios.")
        except serial.SerialException as e:
            self.logger.error(f"Error al abrir el puerto serie: {e}")
            self.serial_port = None

    def send_command(self, command: str) -> str:
        """Envía un comando AT y espera la respuesta."""
        if self.serial_port is None:
            self.logger.error("Puerto no abierto.")
            return ""
        try:
            self.serial_port.write((command + "\r\n").encode())  # Enviar el comando
            time.sleep(1)  # Espera para recibir la respuesta
            response = self.serial_port.read_all().decode('utf-8')
            self.logger.info(f"Respuesta del módem: {response}")
            return response
        except Exception as e:
            self.logger.error(f"Error al enviar el comando {command}: {e}")
            return ""

    def close(self):
        """Cierra el puerto serie."""
        if self.serial_port and self.serial_port.is_open:
            self.serial_port.close()
            self.logger.info("Puerto serie cerrado.")
