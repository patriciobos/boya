"""FSM handler for the AHT10 humidity/temperature sensor."""

from typing import Any, Dict, Optional

from modules.support.base_fsm import BaseHandlerFSM, State, Message, MessageID, ResultCode
from modules.support.data_logger import SensorDataLogger, data_source_for
from modules.support.ll_factory import get_low_level_class
from modules.support.system_config import get_config_value


class AHT10HandlerFSM(BaseHandlerFSM):
    def __init__(self):
        super().__init__("AHT10")
        self.ll = get_low_level_class("AHT10")()
        self._pending_params: dict[str, Any] = {}
        self.status_queue = None
        self.data_logger = SensorDataLogger("AHT10", include_module=False)
        self._last_valid_reading: dict[str, float] | None = None
        self.max_temperature_step_c = float(get_config_value("aht10_max_temperature_step_c", 5.0))
        self.max_humidity_step_rh = float(get_config_value("aht10_max_humidity_step_rh", 35.0))
        self.max_acquire_attempts = int(get_config_value("aht10_max_acquire_attempts", 3))

    def _plausibility_warning(self, temperature_c: float, humidity_rh: float) -> str | None:
        min_temp = float(getattr(self.ll, "MIN_PLAUSIBLE_TEMP_C", -40.0))
        max_temp = float(getattr(self.ll, "MAX_PLAUSIBLE_TEMP_C", 85.0))
        min_rh = float(getattr(self.ll, "MIN_PLAUSIBLE_RH", 0.0))
        max_rh = float(getattr(self.ll, "MAX_PLAUSIBLE_RH", 100.0))

        if not (
            min_temp <= temperature_c <= max_temp
            and min_rh <= humidity_rh <= max_rh
        ):
            return (
                f"AHT10 reading is out of plausible range: "
                f"temperature_c={temperature_c:.2f}, humidity_rh={humidity_rh:.2f}"
            )

        if self._last_valid_reading is None:
            return None

        previous_temp = self._last_valid_reading["temperature_c"]
        previous_humidity = self._last_valid_reading["humidity_rh"]
        temp_delta = abs(temperature_c - previous_temp)
        humidity_delta = abs(humidity_rh - previous_humidity)

        if temp_delta > self.max_temperature_step_c:
            return (
                f"AHT10 temperature jump is not plausible: "
                f"previous={previous_temp:.2f} current={temperature_c:.2f} "
                f"delta={temp_delta:.2f} max={self.max_temperature_step_c:.2f}"
            )

        if humidity_delta > self.max_humidity_step_rh:
            return (
                f"AHT10 humidity jump is not plausible: "
                f"previous={previous_humidity:.2f} current={humidity_rh:.2f} "
                f"delta={humidity_delta:.2f} max={self.max_humidity_step_rh:.2f}"
            )

        return None

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
        elif message.id == MessageID.SIG_ACQUIRE or message.id == MessageID.SIG_TIMEOUT:
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
                warning_message = None
                attempts = max(1, int(self._pending_params.get("max_attempts", self.max_acquire_attempts)))
                for attempt in range(1, attempts + 1):
                    raw = self.ll.read_measurement_raw(
                        timeout=float(self._pending_params.get("timeout", 2.0)),
                        retry_on_null=bool(self._pending_params.get("retry_on_null", True)),
                    )
                    temperature_c, humidity_rh = self.ll.parse(raw)
                    data = {
                        "temperature_c": round(temperature_c, 2),
                        "humidity_rh": round(humidity_rh, 2),
                    }
                    warning_message = self._plausibility_warning(temperature_c, humidity_rh)
                    if warning_message is None:
                        break
                    self.logger.warning(
                        "%s; retrying AHT10 acquisition (%s/%s)",
                        warning_message,
                        attempt,
                        attempts,
                    )

                if warning_message is None:
                    self._last_valid_reading = data
                else:
                    self.logger.warning(
                        "%s after %s attempts; logging last AHT10 value anyway: %s",
                        warning_message,
                        attempts,
                        data,
                    )

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
