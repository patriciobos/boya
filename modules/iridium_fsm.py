from modules.base_fsm import BaseHandlerFSM, State, Message, MessageID, ResultCode, Scheduler
from modules.iridium_LL import IridiumLowLevel
import time

class IridiumHandlerFSM(BaseHandlerFSM):
    def __init__(self):
        super().__init__("Iridium")
        self.modem = IridiumLowLevel()
        self.scheduler = None
        self.status_queue = None
        self._pending_params = {}

    def start_scheduler(self, interval_sec=300):
        self.scheduler = Scheduler(
            name=self.name,
            queue=self.queue,
            get_state_fn=lambda: self.state,
            interval_sec=interval_sec
        )
        self.scheduler.start()

    def stop_scheduler(self):
        if self.scheduler:
            self.scheduler.stop()
            self.scheduler = None

    def update(self):
        if self._last_state != self.state:
            self._on_entry_flag = True
            self._on_exit_flag = False
            self._last_state = self.state

        if self.state == State.INIT and self._on_entry_flag:
            self.logger.info("Entrando a INIT")
            time.sleep(.1) #FIXME: hay un problema aparente de concurrencia en la inicilización
            success = self.modem.init()
            result = ResultCode.OK if success else ResultCode.ERROR
            if self.status_queue:
                self.status_queue.put((self.name, Message(MessageID.STATE_INIT_OK, {"result": result.value})))
            self.set_state(State.TEST if success else State.ERROR, self.status_queue)
            self._on_entry_flag = False

        elif self.state == State.TEST and self._on_entry_flag:
            self.logger.info("Entrando a TEST")
            test_ok, detalles = self.modem.full_test()
            if self.status_queue:
                self.status_queue.put((self.name, Message(
                    MessageID.ACTION_RESULT,
                    {
                        "state": self.state.name,
                        "action": "full_test",
                        "result": ResultCode.OK.value if test_ok else ResultCode.ERROR.value,
                        "details": detalles
                    }
                )))
            self.set_state(State.IDLE if test_ok else State.ERROR, self.status_queue)
            self._on_entry_flag = False

        elif self.state == State.IDLE and self._on_entry_flag:
            self.logger.info("Entrando a IDLE")
            if self.scheduler is None:
                self.start_scheduler(interval_sec=300)
            self._on_entry_flag = False

        elif self.state == State.REPORT and self._on_entry_flag:
            self.logger.info("Entrando a REPORT (consulta de estado)")
            status = self.modem.check_status()
            if self.status_queue:
                self.status_queue.put((self.name, Message(
                    MessageID.ACTION_RESULT,
                    {
                        "state": self.state.name,
                        "action": "check_status",
                        "result": ResultCode.OK.value,
                        "status": status
                    }
                )))
            self.set_state(State.IDLE, self.status_queue)
            self._on_entry_flag = False

        elif self.state == State.DISABLE and self._on_entry_flag:
            self.logger.info("Entrando a DISABLE")
            self.stop_scheduler()
            self.modem.close()
            self._on_entry_flag = False

        elif self.state == State.ERROR and self._on_entry_flag:
            self.logger.error("Entrando a ERROR")
            self.stop_scheduler()
            self.modem.close()
            self._on_entry_flag = False

    def handle_message(self, message: Message):
        if self.state == State.DISABLE:
            if message.id == MessageID.SIG_INIT:
                self.set_state(State.INIT, self.status_queue)
            return

        if message.id == MessageID.SIG_DEINIT:
            self.set_state(State.DISABLE, self.status_queue)

        elif message.id == MessageID.SIG_TEST:
            self.set_state(State.TEST, self.status_queue)

        elif message.id == MessageID.SIG_TIMEOUT:
            self.set_state(State.REPORT, self.status_queue)

        elif message.id == MessageID.SIG_QUERY:
            if self.status_queue:
                self.status_queue.put((self.name, Message(
                    MessageID.ACTION_RESULT,
                    {
                        "state": self.state.name,
                        "action": "query",
                        "result": ResultCode.OK.value
                    }
                )))
