"""FSM handler for the XTRA2210 solar charge controller."""

from typing import Any, Dict, Optional

from modules.support.base_fsm import (
    BaseHandlerFSM,
    State,
    Message,
    MessageID,
    ResultCode,
)
from modules.support.data_logger import SensorDataLogger, data_source_for
from modules.support.ll_factory import get_low_level_class


class XTRA2210HandlerFSM(BaseHandlerFSM):
    def __init__(self):
        super().__init__("XTRA2210")
        self.ll = get_low_level_class("XTRA2210")()
        self._pending_params: dict[str, Any] = {}
        self.status_queue = None
        self.data_logger = SensorDataLogger("XTRA2210", include_module=False)

    def _emit_state_result(
        self, result: ResultCode, details: Optional[Dict[str, Any]] = None
    ):
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
        self,
        action: str,
        result: ResultCode,
        data: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ):
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
            self.status_queue.put(
                (self.name, Message(MessageID.ACTION_RESULT, payload))
            )

    def _is_firmware_mock(self, data: Dict[str, Any]) -> bool:
        identity = data.get("identity", {})
        firmware = identity.get("firmware") or data.get("firmware")
        return isinstance(firmware, str) and firmware.strip().lower() == "mock"

    def _normalize_measurement(self, data: Dict[str, Any]) -> Dict[str, Any]:
        pv = data.get("pv", {})
        load = data.get("load", {})
        battery = data.get("battery", {})
        temperatures = data.get("temperatures", {})
        return {
            "pv_voltage_v": data.get(
                "pv_voltage_v", pv.get("pv_voltage_v", pv.get("input_voltage"))
            ),
            "pv_current_a": data.get(
                "pv_current_a", pv.get("pv_current_a", pv.get("input_current"))
            ),
            "load_current_a": data.get(
                "load_current_a", load.get("load_current_a", load.get("current"))
            ),
            "battery_voltage_v": data.get(
                "battery_voltage_v",
                battery.get(
                    "battery_voltage_v",
                    battery.get(
                        "voltage",
                        battery.get(
                            "system_rated_voltage_v", data.get("system_rated_voltage_v")
                        ),
                    ),
                ),
            ),
            "battery_soc_pct": data.get(
                "battery_soc_pct", battery.get("battery_soc_pct", battery.get("soc"))
            ),
            "battery_temperature_c": data.get(
                "battery_temp_c",
                battery.get(
                    "battery_temp_c",
                    battery.get("temperature")
                    or temperatures.get("battery_temp_c", temperatures.get("battery")),
                ),
            ),
            "device_temperature_c": data.get(
                "device_temp_c",
                temperatures.get("device_temp_c", temperatures.get("device")),
            ),
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
                raw_data = self.ll.read_all_decoded()
                data = self._normalize_measurement(raw_data)
                result = ResultCode.OK
                source = (
                    "firmware mock"
                    if self._is_firmware_mock(raw_data)
                    else data_source_for(self.ll)
                )
                self.data_logger.log(data, source=source)
            except Exception as exc:
                error_message = str(exc)
                result = ResultCode.ERROR
            self._emit_action_result("acquire", result, data=data, error=error_message)
            self.set_state(
                State.IDLE if result == ResultCode.OK else State.ERROR,
                self.status_queue,
            )
            self._on_entry_flag = False

        elif self.state == State.DISABLE and self._on_entry_flag:
            self.ll.deinit()
            self._on_entry_flag = False

        elif self.state == State.ERROR and self._on_entry_flag:
            self.ll.deinit()
            self._on_entry_flag = False
