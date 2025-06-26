from modules.base_fsm import BaseHandlerFSM, State, Message, MessageID
from modules.iridium_LL import IridiumLowLevel
import time
import logging

class IridiumHandlerFSM(BaseHandlerFSM):
    def __init__(self):
        super().__init__("Iridium")
        self.modem = IridiumLowLevel()
        self._on_entry_flag = True
        self.logger = logging.getLogger("IridiumHandlerFSM")
        self.logger.setLevel(logging.INFO)

    def init(self) -> bool:
        """Inicializa el módem Iridium enviando el comando AT y esperando OK."""
        self.logger.info("Inicializando módem Iridium...")
        response = self.modem.send_command("AT")
        if response is not None and response.get('status') == 'OK':
            self.logger.info("Módem inicializado correctamente.")
            return True
        else:
            self.logger.error(f"Error en la inicialización: {response}")
            return False

    def full_test(self) -> bool:
        """Ejecuta un test completo del módem (por ejemplo, comprobación de señal)."""
        self.logger.info("Ejecutando test del módem Iridium...")
        response = self.modem.send_command("AT+CSQ")
        if response is not None and response.get('status') == 'OK' and '+CSQ' in response.get('payload', ''):
            self.logger.info("Test completo exitoso.")
            return True
        else:
            self.logger.error(f"Error en el test del módem: {response}")
            return False

    def update(self):
        """Lógica de los estados."""
        if self.state == State.INIT and self._on_entry_flag:
            self.logger.info("Entrando en el estado INIT")
            if self.init():
                self.set_state(State.TEST)
            else:
                self.set_state(State.ERROR)
            self._on_entry_flag = False

        elif self.state == State.TEST and self._on_entry_flag:
            self.logger.info("Entrando en el estado TEST")
            if self.full_test():
                self.set_state(State.IDLE)
            else:
                self.set_state(State.ERROR)
            self._on_entry_flag = False

        elif self.state == State.IDLE and self._on_entry_flag:
            self.logger.info("Entrando en el estado IDLE")
            self._on_entry_flag = False

        elif self.state == State.DISABLE and self._on_entry_flag:
            self.logger.info("Entrando en el estado DISABLE")
            self.modem.close()
            self._on_entry_flag = False

        elif self.state == State.ERROR and self._on_entry_flag:
            self.logger.error("Entrando en el estado ERROR")
            self.modem.close()
            self._on_entry_flag = False

    def stop_scheduler(self):
        """Método vacío para simetría con otros FSMs."""
        pass
