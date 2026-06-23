import math

from modules.support.base_fsm import (
    BaseHandlerFSM,
    State,
    Message,
    MessageID,
    ResultCode,
)
from modules.support.data_logger import SensorDataLogger, data_source_for
from modules.support.ll_factory import get_low_level_class


def _circular_mean_deg(values):
    if not values:
        return None
    sin_sum = sum(math.sin(math.radians(value)) for value in values)
    cos_sum = sum(math.cos(math.radians(value)) for value in values)
    if sin_sum == 0 and cos_sum == 0:
        return None
    return round(math.degrees(math.atan2(sin_sum, cos_sum)) % 360.0, 2)


def _summarize_wind_samples(samples, requested_samples, success):
    speeds = [
        float(sample["speed"]) for sample in samples if sample.get("speed") is not None
    ]
    directions = [
        float(sample["direction_deg"])
        for sample in samples
        if sample.get("direction_valid") and sample.get("direction_deg") is not None
    ]
    data = {
        "samples": requested_samples,
        "valid_samples": len(speeds),
    }
    if speeds:
        data.update(
            {
                "wind_speed_mps_avg": round(sum(speeds) / len(speeds), 3),
                "wind_speed_mps_min": round(min(speeds), 3),
                "wind_speed_mps_max": round(max(speeds), 3),
            }
        )
    direction_avg = _circular_mean_deg(directions)
    if direction_avg is not None:
        data["wind_direction_deg_avg"] = direction_avg
        data["direction_valid"] = True
    else:
        data["wind_direction_deg_avg"] = None
        data["direction_valid"] = False
    return data


class WindsonicHandlerFSM(BaseHandlerFSM):
    def __init__(self):
        super().__init__("Windsonic")
        self.ll = get_low_level_class("Windsonic")()
        self._pending_params = {}
        self.status_queue = None
        self._acquire_count = 5
        self.data_logger = SensorDataLogger("Windsonic", include_module=False)

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
        if self.status_queue:
            self.status_queue.put(
                (self.name, Message(MessageID.ACTION_RESULT, payload))
            )

    def set_config(self, samples=None, spacing=None):
        if samples is not None or spacing is not None:
            self.ll.config(
                samples=samples if samples is not None else self.ll.samples,
                spacing=spacing if spacing is not None else self.ll.spacing,
            )
            self.logger.info(
                "Windsonic config updated: samples=%s spacing=%s",
                self.ll.samples,
                self.ll.spacing,
            )

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
                if not getattr(self.ll, "is_open", False):
                    self.ll.open()
                success = self.ll.acquire(
                    self._pending_params.get("num", self._acquire_count)
                )
                if not success:
                    self._emit_action_result(
                        "acquire", ResultCode.ERROR, error=self.ll.last_error
                    )
                    self.set_state(State.ERROR, self.status_queue)
                self._on_entry_flag = False

            done, success = self.ll.is_acquisition_done()
            if done:
                result = ResultCode.OK if success else ResultCode.ERROR
                requested_samples = self._pending_params.get("num", self._acquire_count)
                samples = getattr(self.ll, "last_samples", [])
                data = _summarize_wind_samples(samples, requested_samples, success)
                if result == ResultCode.OK:
                    self.data_logger.log(data, source=data_source_for(self.ll))
                    self._emit_action_result("acquire", result, data=data)
                else:
                    self._emit_action_result(
                        "acquire", result, data=data, error=self.ll.last_error
                    )
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
