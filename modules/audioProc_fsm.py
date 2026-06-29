"""
FSM handler for the AudioProc module.
"""

import json
from pathlib import Path
from threading import Thread

from modules.support.base_fsm import BaseHandlerFSM, State, Message, MessageID, ResultCode
from modules.support.data_logger import SensorDataLogger, data_source_for
from modules.support.ll_factory import get_low_level_class
from modules.support.system_config import PROJECT_ROOT, get_data_path


def _project_relative_path(path):
    if path is None:
        return None
    try:
        return str(Path(path).resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _project_absolute_path(path):
    if path is None:
        return None
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return PROJECT_ROOT / candidate


class AudioProcHandlerFSM(BaseHandlerFSM):
    def __init__(self):
        super().__init__("AudioProc")
        self.ll = get_low_level_class("AudioProc")()
        self._pending_params = {}
        self.status_queue = None
        self._processing_thread = None
        self.data_logger = SensorDataLogger("AudioProc", include_module=False, file_stem="audioProc")

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

    def _latest_behringer_recording(self):
        readings_path = get_data_path() / "behringer_readings.jsonl"
        if not readings_path.exists():
            return None
        for line in reversed(readings_path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            file_path = (entry.get("data") or {}).get("file")
            if file_path:
                candidate = _project_absolute_path(file_path)
                if candidate.exists():
                    return str(candidate)
        return None

    def _existing_output_for(self, input_path):
        input_rel = _project_relative_path(input_path)
        readings_path = get_data_path() / "audioProc_readings.jsonl"
        if not readings_path.exists():
            return None
        for line in reversed(readings_path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            data = entry.get("data") or {}
            if data.get("input_file") != input_rel:
                continue
            output_file = data.get("output_file")
            output_path = _project_absolute_path(output_file) if output_file else None
            if output_path and output_path.exists() and output_path.name.startswith("audioProc_"):
                return output_file
        return None

    def _log_success(self, input_path, output_path, source=None):
        data = {
            "input_file": _project_relative_path(input_path),
            "output_file": _project_relative_path(output_path),
        }
        self.data_logger.log(data, source=source)
        return data

    def handle_message(self, message: Message):
        if self._ignore_scheduler_while_error(message):
            return

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
        elif message.id == MessageID.SIG_TIMEOUT:
            self._pending_params = {"input": None, "origin": "scheduler"}
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
            input_path = self._pending_params.get("input") or self._latest_behringer_recording()
            if not input_path:
                self._emit_action_result("process", ResultCode.ERROR, error="no_input_available")
                self.set_state(State.ERROR, self.status_queue)
                self._on_entry_flag = False
                return
            input_path = str(_project_absolute_path(input_path))

            existing_output = self._existing_output_for(input_path)
            if existing_output is not None:
                data = {"input": _project_relative_path(input_path), "output": existing_output}
                self._emit_action_result("process", ResultCode.OK, data=data, details={"skipped": "already_processed"})
                self.set_state(State.IDLE, self.status_queue)
                self._on_entry_flag = False
                return

            def _run(path):
                try:
                    self.logger.info("Processing file in background: %s", path)
                    output_path = self.ll.process(path)
                    if output_path is None:
                        self._emit_action_result("process", ResultCode.ERROR, data={"input": _project_relative_path(path)})
                        self.set_state(State.ERROR, self.status_queue)
                    else:
                        source = data_source_for(self.ll)
                        logged = self._log_success(path, output_path, source=source)
                        self._emit_action_result("process", ResultCode.OK, data={"input": logged["input_file"], "output": logged["output_file"]})
                        self.set_state(State.IDLE, self.status_queue)
                except Exception as exc:
                    self.logger.exception("Processing thread failed: %s", exc)
                    self._emit_action_result("process", ResultCode.ERROR, data={"input": _project_relative_path(path)}, error=str(exc))
                    self.set_state(State.ERROR, self.status_queue)

            self._processing_thread = Thread(target=_run, args=(input_path,), daemon=True)
            self._processing_thread.start()
            self._on_entry_flag = False

        elif self.state == State.DISABLE and self._on_entry_flag:
            self.logger.info("Entering DISABLE")
            self.ll.deinit()
            self._on_entry_flag = False

        elif self.state == State.ERROR and self._on_entry_flag:
            self.logger.error("Entering ERROR")
            self.ll.deinit()
            self._on_entry_flag = False
