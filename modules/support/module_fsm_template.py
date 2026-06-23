"""
Template FSM module for new hardware/software handlers.

Standard FSM contract:
- Base lifecycle states: DISABLE, INIT, TEST, IDLE, ERROR
- Optional action states: ACQUIRE, PROCESS, TRANSMIT
- Standard upstream message: ACTION_RESULT with action/result/data/error/details.
"""

from modules.support.base_fsm import (
    BaseHandlerFSM,
    State,
    Message,
    MessageID,
    ResultCode,
)
from modules.support.module_LL_template import (
    ModuleLowLevel,
)  # Replace with actual LL module


class ModuleHandlerFSM(BaseHandlerFSM):
    def __init__(self):
        super().__init__("ModuleName")
        self.ll = ModuleLowLevel()
        self._pending_params = {}
        self.status_queue = None
        self._acquire_params = {}

    def _emit_state_result(self, result: ResultCode, details=None):
        if self.status_queue:
            self.status_queue.put(
                (
                    self.name,
                    Message(
                        MessageID.STATE_RESULT,
                        {
                            "result": result.value,
                            "details": details or {},
                        },
                    ),
                )
            )

    def _emit_action_result(
        self, action: str, result: ResultCode, data=None, error=None, details=None
    ):
        payload = {
            "origin": self.name,
            "state": self.state.name,
            "action": action,
            "result": result.value,
            "data": data or {},
            "details": details or {},
        }
        if error is not None:
            payload["error"] = error
        if self.status_queue:
            self.status_queue.put(
                (self.name, Message(MessageID.ACTION_RESULT, payload))
            )

    def handle_message(self, message: Message):
        if self.state == State.DISABLE:
            if message.id == MessageID.SIG_INIT:
                self.set_state(State.INIT, self.status_queue)
            return

        if message.id == MessageID.SIG_DEINIT:
            self.set_state(State.DISABLE, self.status_queue)
        elif message.id == MessageID.SIG_TEST:
            self.set_state(State.TEST, self.status_queue)
        elif message.id in (MessageID.SIG_ACQUIRE, MessageID.SIG_TIMEOUT):
            self._pending_params = dict(
                getattr(message, "params", {}) or self._acquire_params
            )
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

        elif self.state == State.ACQUIRE and self._on_entry_flag:
            self.logger.info("Entering ACQUIRE")
            ok, details = self.ll.full_test()
            result = ResultCode.OK if ok else ResultCode.ERROR
            self._emit_action_result("acquire", result, details=details)
            self.set_state(State.IDLE if ok else State.ERROR, self.status_queue)
            self._on_entry_flag = False

        elif self.state == State.DISABLE and self._on_entry_flag:
            self.logger.info("Entering DISABLE")
            self.ll.deinit()
            self._on_entry_flag = False

        elif self.state == State.ERROR and self._on_entry_flag:
            self.logger.error("Entering ERROR")
            self.ll.deinit()
            self._on_entry_flag = False
