"""FSM handler for the AIS/GPS serial module."""

from typing import Any, Dict, Optional

from modules.support.base_fsm import BaseHandlerFSM, State, Message, MessageID, ResultCode
from modules.support.data_logger import SensorDataLogger, data_source_for
from modules.support.ll_factory import get_low_level_class
from modules.ais_LL import _nmea_validate_checksum


class AISHandlerFSM(BaseHandlerFSM):
    def __init__(self):
        super().__init__("AIS")
        self.ll = get_low_level_class("AIS")()
        self._pending_params: dict[str, Any] = {}
        self.status_queue = None
        self.data_logger = SensorDataLogger("AIS", include_module=False)

    def _emit_state_result(self, result: ResultCode, details: Optional[Dict[str, Any]] = None):
        if self.status_queue:
            self.status_queue.put((self.name, Message(MessageID.STATE_RESULT, {
                "result": result.value,
                "details": details or {},
            })))

    def _emit_action_result(self, action: str, result: ResultCode, data: Optional[Dict[str, Any]] = None, error: Optional[str] = None):
        payload = {
            "origin": self.name,
            "state": self.state.name,
            "action": action,
            "result": result.value,
            "data": data or {},
        }
        if error is not None:
            payload["error"] = error
        if self.status_queue:
            self.status_queue.put((self.name, Message(MessageID.ACTION_RESULT, payload)))

    def _normalize_measurement(self, navigation: Dict[str, Any], lines: list[str]) -> Dict[str, Any]:
        return {
            "gps_fix": bool(navigation.get("fix")),
            "lat": navigation.get("lat"),
            "lon": navigation.get("lon"),
            "satellites": int(navigation.get("num_sats") or 0),
            "hdop": navigation.get("hdop"),
            "own_transmit_messages": sum(1 for line in lines if isinstance(line, str) and line.startswith("!AIVDO")),
        }

    def _fresh_traffic_summary(self, lines: list[str]) -> Dict[str, int]:
        valid_nmea = 0
        valid_ais = 0

        for line in lines:
            if not isinstance(line, str):
                continue
            line = line.strip()
            if not line:
                continue

            if line.startswith(("!AIVDO", "!AIVDM")) and _nmea_validate_checksum(line):
                valid_ais += 1
            elif line.startswith("$") and _nmea_validate_checksum(line):
                valid_nmea += 1
                parse_nmea = getattr(self.ll, "parse_nmea", None)
                if callable(parse_nmea):
                    parse_nmea(line)

        return {
            "lines_seen": len([line for line in lines if isinstance(line, str) and line.strip()]),
            "valid_nmea_lines": valid_nmea,
            "valid_ais_lines": valid_ais,
        }

    def handle_message(self, message: Message):
        if self._ignore_scheduler_while_error(message):
            return

        if self.state == State.DISABLE:
            if message.id == MessageID.SIG_INIT:
                self.set_state(State.INIT, self.status_queue)
            return

        if message.id == MessageID.SIG_DEINIT:
            self.set_state(State.DISABLE, self.status_queue)
        elif message.id == MessageID.SIG_TEST:
            self.set_state(State.TEST, self.status_queue)
        elif message.id in (MessageID.SIG_ACQUIRE, MessageID.SIG_TIMEOUT):
            self._pending_params = getattr(message, "params", {}) or {}
            self.set_state(State.ACQUIRE, self.status_queue)

    def update(self):
        if self._last_state != self.state:
            self._on_entry_flag = True
            self._on_exit_flag = False
            self._last_state = self.state

        if self.state == State.INIT and self._on_entry_flag:
            success = self.ll.init()
            result = ResultCode.OK if success else ResultCode.ERROR
            self._emit_state_result(result)
            self.set_state(State.TEST if success else State.ERROR, self.status_queue)
            self._on_entry_flag = False

        elif self.state == State.TEST and self._on_entry_flag:
            ok, details = self.ll.full_test()
            result = ResultCode.OK if ok else ResultCode.ERROR
            self._emit_action_result("test", result, data=details)
            self.set_state(State.IDLE if ok else State.ERROR, self.status_queue)
            self._on_entry_flag = False

        elif self.state == State.IDLE and self._on_entry_flag:
            self._on_entry_flag = False

        elif self.state == State.ACQUIRE and self._on_entry_flag:
            error_message = None
            data: dict[str, Any] = {}
            try:
                if not getattr(self.ll, "is_open", False):
                    self.ll.open()
                reset_navigation = getattr(self.ll, "_reset_navigation", None)
                if callable(reset_navigation):
                    reset_navigation()
                seconds = float(self._pending_params.get("seconds", 1.0))
                lines = self.ll.read_lines(seconds=seconds)
                traffic = self._fresh_traffic_summary(lines)
                if traffic["valid_nmea_lines"] + traffic["valid_ais_lines"] == 0:
                    raise RuntimeError(
                        "No fresh AIS/GPS traffic detected "
                        f"(lines={traffic['lines_seen']}, valid_nmea={traffic['valid_nmea_lines']}, "
                        f"valid_ais={traffic['valid_ais_lines']})"
                    )
                navigation = self.ll.get_navigation()
                data = self._normalize_measurement(navigation, lines)
                result = ResultCode.OK
                self.data_logger.log(data, source=data_source_for(self.ll))
            except Exception as exc:
                error_message = str(exc)
                result = ResultCode.ERROR
            self._emit_action_result("acquire", result, data=data, error=error_message)
            self.set_state(State.IDLE if result == ResultCode.OK else State.ERROR, self.status_queue)
            self._on_entry_flag = False

        elif self.state == State.DISABLE and self._on_entry_flag:
            self.ll.deinit()
            self._on_entry_flag = False

        elif self.state == State.ERROR and self._on_entry_flag:
            self.ll.deinit()
            self._on_entry_flag = False
