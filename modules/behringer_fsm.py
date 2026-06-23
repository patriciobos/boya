import os
import sys
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.support.base_fsm import (
    BaseHandlerFSM,
    State,
    Message,
    MessageID,
    ResultCode,
)
from modules.support.data_logger import SensorDataLogger, data_source_for
from modules.support.ll_factory import get_low_level_class
from modules.support.storage_guard import STORAGE_WARNING_INVALID_DURATION_USING_DEFAULT
from modules.support.system_config import PROJECT_ROOT, get_config_value


def _project_relative_path(path):
    if path is None:
        return None
    try:
        return str(Path(path).resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _file_size_bytes(path):
    if path is None:
        return None
    try:
        return Path(path).stat().st_size
    except OSError:
        return None


class BehringerHandlerFSM(BaseHandlerFSM):
    def __init__(self):
        super().__init__("Behringer")
        self.ll = get_low_level_class("Behringer")()
        self._pending_params = {}
        self.status_queue = None
        self._duration_warnings = []
        self._acquire_duration = self._configured_acquire_duration()
        self.data_logger = SensorDataLogger("Behringer", include_module=False)

    def _configured_acquire_duration(self) -> float:
        raw_duration = get_config_value("duration[s]", 60)
        try:
            duration = float(raw_duration)
            if duration <= 0:
                raise ValueError("duration must be positive")
            return duration
        except (TypeError, ValueError):
            self._duration_warnings = [STORAGE_WARNING_INVALID_DURATION_USING_DEFAULT]
            self.logger.warning(
                "Invalid duration[s] config for Behringer acquisition: %r. Using 60 seconds.",
                raw_duration,
            )
            return 60.0

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
        if error:
            payload["error"] = error
        # Transitional compatibility for current router.
        if data and "file" in data:
            payload["file"] = data["file"]
        if self.status_queue:
            self.status_queue.put(
                (self.name, Message(MessageID.ACTION_RESULT, payload))
            )

    def handle_message(self, message: Message):
        if self._ignore_scheduler_while_error(message):
            return

        params = getattr(message, "params", {}) or {}
        if self.state == State.ERROR and message.id in (
            MessageID.SIG_ACQUIRE,
            MessageID.SIG_PROCESS,
            MessageID.SIG_TIMEOUT,
            MessageID.SIG_TRANSMIT,
        ):
            self.logger.warning(
                "Behringer explicit ERROR guard ignored operational message: %s | Params: %s",
                message.id.value,
                params,
            )
            return

        if self.state == State.DISABLE:
            if message.id == MessageID.SIG_INIT:
                self.set_state(State.INIT, self.status_queue)
            return

        if message.id == MessageID.SIG_DEINIT:
            self.set_state(State.DISABLE, self.status_queue)
        elif message.id == MessageID.SIG_TEST:
            self.set_state(State.TEST, self.status_queue)
        elif message.id == MessageID.SIG_ACQUIRE:
            self._pending_params = {
                "duration": params.get("duration", self._acquire_duration)
            }
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
            self._on_entry_flag = False

        elif self.state == State.ACQUIRE:
            if self._on_entry_flag:
                self.logger.info("Entering ACQUIRE")
                duration = self._pending_params.get("duration", self._acquire_duration)
                success = self.ll.record(duration)
                if not success:
                    self._emit_action_result(
                        "acquire", ResultCode.ERROR, error=self.ll.last_error
                    )
                    self.set_state(State.ERROR, self.status_queue)
                self._on_entry_flag = False

            done, success = self.ll.is_recording_done()
            if done:
                result = ResultCode.OK if success else ResultCode.ERROR
                recording_metadata = (
                    getattr(self.ll, "last_recording_metadata", {}) or {}
                )
                data = {
                    "file": _project_relative_path(self.ll.output_path),
                    "duration_s": self._pending_params.get(
                        "duration", self._acquire_duration
                    ),
                    "sample_rate_hz": getattr(self.ll, "sample_rate", 192000),
                    "channels": getattr(self.ll, "output_channels", 1),
                    "size_bytes": _file_size_bytes(self.ll.output_path),
                }
                if recording_metadata:
                    data["recording"] = recording_metadata
                    data["storage"] = {
                        "expected_size_bytes": recording_metadata.get(
                            "expected_size_bytes"
                        ),
                        "max_file_size_bytes": recording_metadata.get(
                            "max_file_size_bytes"
                        ),
                        "free_bytes_before": recording_metadata.get(
                            "free_bytes_before"
                        ),
                        "free_bytes_after": recording_metadata.get("free_bytes_after"),
                        "recordings_dir_used_bytes": recording_metadata.get(
                            "recordings_dir_used_bytes"
                        ),
                        "warnings": recording_metadata.get("warnings", []),
                    }
                if self._duration_warnings:
                    data.setdefault("storage", {})["warnings"] = (
                        data.get("storage", {}).get("warnings", [])
                        + self._duration_warnings
                    )
                if result == ResultCode.OK:
                    self.data_logger.log(data, source=data_source_for(self.ll))
                self._emit_action_result("acquire", result, data=data)
                self.set_state(
                    State.IDLE if success else State.ERROR, self.status_queue
                )

        elif self.state == State.DISABLE and self._on_entry_flag:
            self.logger.info("Entering DISABLE")
            self.ll.deinit()
            self._on_entry_flag = False

        elif self.state == State.ERROR and self._on_entry_flag:
            self.logger.error("Entering ERROR")
            self.ll.deinit()
            self._on_entry_flag = False


if __name__ == "__main__":
    import json
    from modules.support.base_fsm import run_fsm_self_test

    ok, report = run_fsm_self_test(BehringerHandlerFSM(), timeout_s=60.0)
    print(json.dumps(report, indent=2, default=str))
    raise SystemExit(0 if ok else 1)
