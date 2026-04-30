"""
FSM handler for the AudioProc module.
"""

from threading import Thread

from modules.support.base_fsm import BaseHandlerFSM, State, Message, MessageID, ResultCode
from modules.audioProc_LL import AudioProcLowLevel


class AudioProcHandlerFSM(BaseHandlerFSM):
    def __init__(self):
        super().__init__("AudioProc")
        self.ll = AudioProcLowLevel()
        self._pending_params = {}
        self.status_queue = None
        self.scheduler = None
        self._processing_thread = None

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
        # Transitional compatibility for routers that still inspect root-level file/output.
        if data:
            if "input" in data:
                payload["file"] = data["input"]
            if "output" in data:
                payload["output"] = data["output"]
        if self.status_queue:
            self.status_queue.put((self.name, Message(MessageID.ACTION_RESULT, payload)))

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
        elif message.id == MessageID.SIG_PROCESS:
            input_path = params.get("input") or params.get("file")
            self._pending_params = {"input": input_path, "origin": params.get("origin")}
            self.set_state(State.PROCESS, self.status_queue)

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

        elif self.state == State.PROCESS and self._on_entry_flag:
            self.logger.info("Entering PROCESS")
            input_path = self._pending_params.get("input")
            if not input_path:
                self._emit_action_result("process", ResultCode.ERROR, error="no_input_provided")
                self.set_state(State.ERROR, self.status_queue)
                self._on_entry_flag = False
                return

            def _run(path):
                try:
                    self.logger.info("Processing file in background: %s", path)
                    output_path = self.ll.process(path)
                    if output_path is None:
                        self._emit_action_result("process", ResultCode.ERROR, data={"input": path})
                        self.set_state(State.ERROR, self.status_queue)
                    else:
                        self._emit_action_result("process", ResultCode.OK, data={"input": path, "output": output_path})
                        self.set_state(State.IDLE, self.status_queue)
                except Exception as exc:
                    self.logger.exception("Processing thread failed: %s", exc)
                    self._emit_action_result("process", ResultCode.ERROR, data={"input": path}, error=str(exc))
                    self.set_state(State.ERROR, self.status_queue)

            self._processing_thread = Thread(target=_run, args=(input_path,), daemon=True)
            self._processing_thread.start()
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
