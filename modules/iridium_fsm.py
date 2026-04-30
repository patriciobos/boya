from modules.support.base_fsm import BaseHandlerFSM, State, Message, MessageID, ResultCode, Scheduler
from modules.iridium_LL import IridiumLowLevel


class IridiumHandlerFSM(BaseHandlerFSM):
    def __init__(self):
        super().__init__("Iridium")
        self.ll = IridiumLowLevel()
        self.scheduler = None
        self.status_queue = None
        self._pending_params = {}
        self._acquire_interval_sec = 300

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

    def start_scheduler(self, interval_sec=300):
        self._acquire_interval_sec = interval_sec
        self.scheduler = Scheduler(
            name=self.name,
            queue=self.queue,
            get_state_fn=lambda: self.state,
            interval_sec=interval_sec,
        )
        self.scheduler.start()

    def stop_scheduler(self):
        if self.scheduler:
            self.scheduler.stop()
            self.scheduler = None

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
        elif message.id in (MessageID.SIG_ACQUIRE, MessageID.SIG_TIMEOUT):
            self._pending_params = {}
            self.set_state(State.ACQUIRE, self.status_queue)
        elif message.id == MessageID.SIG_TRANSMIT:
            self._pending_params = {
                "mode": params.get("mode", "text"),
                "payload": params.get("payload"),
                "text": params.get("text"),
                "clear_after_success": params.get("clear_after_success", True),
                "max_attempts": params.get("max_attempts", 3),
                "retry_delay_s": params.get("retry_delay_s", 10.0),
            }
            self.set_state(State.TRANSMIT, self.status_queue)

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
            if self.scheduler is None:
                self.start_scheduler(interval_sec=self._acquire_interval_sec)
            self._on_entry_flag = False

        elif self.state == State.ACQUIRE and self._on_entry_flag:
            self.logger.info("Entering ACQUIRE")
            status = self.ll.check_status()
            self._emit_action_result("acquire", ResultCode.OK, data={"status": status})
            self.set_state(State.IDLE, self.status_queue)
            self._on_entry_flag = False

        elif self.state == State.TRANSMIT and self._on_entry_flag:
            self.logger.info("Entering TRANSMIT")
            mode = self._pending_params.get("mode")
            max_attempts = int(self._pending_params.get("max_attempts", 3))
            retry_delay_s = float(self._pending_params.get("retry_delay_s", 10.0))
            clear_after_success = bool(self._pending_params.get("clear_after_success", True))
            try:
                if mode == "binary":
                    payload = self._pending_params.get("payload")
                    if not isinstance(payload, (bytes, bytearray)):
                        raise ValueError("binary transmit requires bytes payload")
                    ok, details = self.ll.send_sbd_binary(
                        bytes(payload),
                        clear_after_success=clear_after_success,
                        max_attempts=max_attempts,
                        retry_delay_s=retry_delay_s,
                    )
                else:
                    text = self._pending_params.get("text")
                    if text is None:
                        payload = self._pending_params.get("payload")
                        text = payload.decode("utf-8") if isinstance(payload, bytes) else str(payload or "")
                    ok, details = self.ll.send_sbd_text(
                        text,
                        clear_after_success=clear_after_success,
                        max_attempts=max_attempts,
                        retry_delay_s=retry_delay_s,
                    )

                result = ResultCode.OK if ok else ResultCode.ERROR
                self._emit_action_result("transmit", result, details=details)
                self.set_state(State.IDLE if ok else State.ERROR, self.status_queue)
            except Exception as exc:
                self.logger.exception("Transmit failed: %s", exc)
                self._emit_action_result("transmit", ResultCode.ERROR, error=str(exc))
                self.set_state(State.ERROR, self.status_queue)
            self._on_entry_flag = False

        elif self.state == State.DISABLE and self._on_entry_flag:
            self.logger.info("Entering DISABLE")
            self.stop_scheduler()
            self.ll.deinit()
            self._on_entry_flag = False

        elif self.state == State.ERROR and self._on_entry_flag:
            self.logger.error("Entering ERROR")
            self.stop_scheduler()
            self.ll.deinit()
            self._on_entry_flag = False
