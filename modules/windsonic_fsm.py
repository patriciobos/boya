from modules.support.base_fsm import BaseHandlerFSM, State, Message, MessageID, ResultCode
from modules.support.data_logger import SensorDataLogger
from modules.support.ll_factory import get_low_level_class


class WindsonicHandlerFSM(BaseHandlerFSM):
    def __init__(self):
        super().__init__("Windsonic")
        self.ll = get_low_level_class("Windsonic")()
        self._pending_params = {}
        self.status_queue = None
        self._acquire_count = 5
        self.data_logger = SensorDataLogger("Windsonic")

    def _emit_state_result(self, result: ResultCode, details=None):
        if self.status_queue:
            self.status_queue.put((self.name, Message(MessageID.STATE_RESULT, {
                "result": result.value,
                "details": details or {},
            })))

    def _emit_action_result(self, action: str, result: ResultCode, data=None, error=None, details=None):
        payload = {
            "origin": self.name,
            "state": self.state.name,
            "action": action,
            "result": result.value,
            "data": data or {},
            "details": details or {},
        }
        if error:
            payload["error"] = error
        if self.status_queue:
            self.status_queue.put((self.name, Message(MessageID.ACTION_RESULT, payload)))

    def set_config(self, samples=None, spacing=None):
        if samples is not None or spacing is not None:
            self.ll.config(
                samples=samples if samples is not None else self.ll.samples,
                spacing=spacing if spacing is not None else self.ll.spacing,
            )
            self.logger.info("Windsonic config updated: samples=%s spacing=%s", self.ll.samples, self.ll.spacing)

    def handle_message(self, message: Message):
        params = getattr(message, "params", {}) or {}
        if self.state == State.DISABLE:
            if message.id == MessageID.SIG_INIT:
                self.set_state(State.INIT, self.status_queue)
            return

        if message.id == MessageID.SIG_DEINIT:
            self.set_state(State.DISABLE, self.status_queue)
        elif message.id == MessageID.SIG_TEST:
            self.set_state(State.TEST, self.status_queue)
        elif message.id == MessageID.SIG_ACQUIRE:
            self._pending_params = {"num": params.get("num", self._acquire_count)}
            self.set_state(State.ACQUIRE, self.status_queue)
        elif message.id == MessageID.SIG_TIMEOUT:
            self._pending_params = {"num": self._acquire_count}
            self.set_state(State.ACQUIRE, self.status_queue)

    def update(self):
        if self._last_state != self.state:
            self._on_entry_flag = True
            self._on_exit_flag = False
            self._last_state = self.state

        if self.state == State.INIT and self._on_entry_flag:
            self.logger.info("Entering INIT")
            success = self.ll.init()
            result = ResultCode.OK if success else ResultCode.ERROR
            self._emit_state_result(result)
            self.set_state(State.TEST if success else State.ERROR, self.status_queue)
            self._on_entry_flag = False

        elif self.state == State.TEST and self._on_entry_flag:
            self.logger.info("Entering TEST")
            ok, details = self.ll.full_test()
            result = ResultCode.OK if ok else ResultCode.ERROR
            self._emit_action_result("test", result, details=details)
            self.set_state(State.IDLE if ok else State.ERROR, self.status_queue)
            self._on_entry_flag = False

        elif self.state == State.IDLE and self._on_entry_flag:
            self.logger.info("Entering IDLE")
            self._on_entry_flag = False

        elif self.state == State.ACQUIRE:
            if self._on_entry_flag:
                self.logger.info("Entering ACQUIRE")
                success = self.ll.acquire(self._pending_params.get("num", self._acquire_count))
                if not success:
                    self._emit_action_result("acquire", ResultCode.ERROR, error=self.ll.last_error)
                    self.set_state(State.ERROR, self.status_queue)
                self._on_entry_flag = False

            done, success = self.ll.is_acquisition_done()
            if done:
                result = ResultCode.OK if success else ResultCode.ERROR
                data = {
                    "samples": self._pending_params.get("num", self._acquire_count),
                    "status": "acquisition_completed" if success else "acquisition_failed",
                }
                if result == ResultCode.OK:
                    self.data_logger.log(data)
                    self._emit_action_result("acquire", result, data=data)
                else:
                    self._emit_action_result("acquire", result, data=data, error=self.ll.last_error)
                self.set_state(State.IDLE if success else State.ERROR, self.status_queue)

        elif self.state == State.DISABLE and self._on_entry_flag:
            self.logger.info("Entering DISABLE")
            self.ll.deinit()
            self._on_entry_flag = False

        elif self.state == State.ERROR and self._on_entry_flag:
            self.logger.error("Entering ERROR")
            self.ll.deinit()
            self._on_entry_flag = False
