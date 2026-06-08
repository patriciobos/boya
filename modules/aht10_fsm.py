"""FSM handler for the AHT10 humidity/temperature sensor."""

from typing import Any, Dict, Optional

from modules.support.base_fsm import BaseHandlerFSM, State, Message, MessageID, ResultCode, Scheduler
from modules.support.data_logger import SensorDataLogger
from modules.support.ll_factory import get_low_level_class
from modules.support.system_config import get_schedule


class AHT10HandlerFSM(BaseHandlerFSM):
    def __init__(self):
        super().__init__("AHT10")
        self.ll = get_low_level_class("AHT10")()
        self._pending_params: dict[str, Any] = {}
        self.status_queue = None
        self.scheduler = None
        self._acquire_interval_sec = int(get_schedule("AHT10", 300) or 300)
        self.logger.info("AHT10 schedule interval: %ss", self._acquire_interval_sec)
        self.data_logger = SensorDataLogger("AHT10")

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

    def start_scheduler(self, interval_sec: int = 300, start_immediately: bool = False) -> None:
        self.scheduler = Scheduler(
            name=self.name,
            queue=self.queue,
            get_state_fn=lambda: self.state,
            interval_sec=interval_sec,
        )
        if start_immediately:
            self.scheduler.start()

    def stop_scheduler(self) -> None:
        if self.scheduler:
            self.scheduler.stop()
            self.scheduler = None

    def handle_message(self, message: Message):
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
            if self.scheduler is None:
                self.start_scheduler(interval_sec=self._acquire_interval_sec)
            self._on_entry_flag = False

        elif self.state == State.ACQUIRE and self._on_entry_flag:
            error_message = None
            data: dict[str, Any] = {}
            try:
                raw = self.ll.read_measurement_raw(
                    timeout=float(self._pending_params.get("timeout", 2.0)),
                    retry_on_null=bool(self._pending_params.get("retry_on_null", True)),
                )
                temperature_c, humidity_rh = self.ll.parse(raw)
                data = {
                    "temperature_c": temperature_c,
                    "humidity_rh": humidity_rh,
                    "raw": [int(x) for x in raw],
                }
                result = ResultCode.OK
                self.data_logger.log(data)
            except Exception as exc:
                error_message = str(exc)
                result = ResultCode.ERROR
            self._emit_action_result("acquire", result, data=data, error=error_message)
            self.set_state(State.IDLE if result == ResultCode.OK else State.ERROR, self.status_queue)
            self._on_entry_flag = False

        elif self.state == State.DISABLE and self._on_entry_flag:
            self.stop_scheduler()
            self.ll.deinit()
            self._on_entry_flag = False

        elif self.state == State.ERROR and self._on_entry_flag:
            self.stop_scheduler()
            self.ll.deinit()
            self._on_entry_flag = False
