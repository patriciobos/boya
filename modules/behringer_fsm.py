import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.support.base_fsm import BaseHandlerFSM, State, Message, MessageID, ResultCode, Scheduler
from modules.support.data_logger import SensorDataLogger
from modules.support.ll_factory import get_low_level_class
from modules.support.system_config import get_schedule


class BehringerHandlerFSM(BaseHandlerFSM):
    def __init__(self):
        super().__init__("Behringer")
        self.ll = get_low_level_class("Behringer")()
        self._pending_params = {}
        self.status_queue = None
        self.scheduler = None
        self._acquire_duration = 10
        self._acquire_interval_sec = int(get_schedule("Behringer", 21600) or 21600)
        self.logger.info("Behringer schedule interval: %ss", self._acquire_interval_sec)
        self.data_logger = SensorDataLogger("Behringer")

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
        # Transitional compatibility for current router.
        if data and "file" in data:
            payload["file"] = data["file"]
        if self.status_queue:
            self.status_queue.put((self.name, Message(MessageID.ACTION_RESULT, payload)))

    def start_scheduler(self, interval_sec=3600, duration_sec=10):
        self._acquire_duration = duration_sec
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
        elif message.id == MessageID.SIG_ACQUIRE:
            self._pending_params = {"duration": params.get("duration", self._acquire_duration)}
            self.set_state(State.ACQUIRE, self.status_queue)
        elif message.id == MessageID.SIG_TIMEOUT:
            self._pending_params = {"duration": self._acquire_duration}
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
            if self.scheduler is None:
                self.start_scheduler(interval_sec=self._acquire_interval_sec, duration_sec=self._acquire_duration)
            self._on_entry_flag = False

        elif self.state == State.ACQUIRE:
            if self._on_entry_flag:
                self.logger.info("Entering ACQUIRE")
                duration = self._pending_params.get("duration", self._acquire_duration)
                success = self.ll.record(duration)
                if not success:
                    self._emit_action_result("acquire", ResultCode.ERROR, error=self.ll.last_error)
                    self.set_state(State.ERROR, self.status_queue)
                self._on_entry_flag = False

            done, success = self.ll.is_recording_done()
            if done:
                result = ResultCode.OK if success else ResultCode.ERROR
                data = {"file": self.ll.output_path, "duration": self._pending_params.get("duration", self._acquire_duration)}
                if result == ResultCode.OK:
                    self.data_logger.log(data)
                self._emit_action_result("acquire", result, data=data)
                self.set_state(State.IDLE if success else State.ERROR, self.status_queue)

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


if __name__ == "__main__":
    import json
    from modules.support.base_fsm import run_fsm_self_test

    ok, report = run_fsm_self_test(BehringerHandlerFSM(), timeout_s=60.0)
    print(json.dumps(report, indent=2, default=str))
    raise SystemExit(0 if ok else 1)