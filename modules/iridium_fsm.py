import json
from datetime import datetime, timezone

from modules.support.base_fsm import BaseHandlerFSM, State, Message, MessageID, ResultCode
from modules.support.iridium_protocol import (
    ALIVE_PAYLOAD_SIZE,
    build_alive_payload,
    build_status_bitmaps,
)
from modules.support.ll_factory import get_low_level_class
from modules.support.system_config import get_data_path, get_logs_path


class IridiumHandlerFSM(BaseHandlerFSM):
    def __init__(self):
        super().__init__("Iridium")
        self.ll = get_low_level_class("Iridium")()
        self.status_queue = None
        self._pending_params = {}

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
        if self.status_queue:
            self.status_queue.put((self.name, Message(MessageID.ACTION_RESULT, payload)))

    def _load_system_status(self):
        path = get_logs_path() / "system_status.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            self.logger.warning("Could not parse system status file: %s", path)
            return {}

    def _load_latest_ais_position(self):
        path = get_data_path() / "ais_readings.jsonl"
        if not path.exists():
            return {"gps_fix": False, "lat": None, "lon": None}
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            data = entry.get("data") or {}
            return {
                "gps_fix": bool(data.get("gps_fix")),
                "lat": data.get("lat"),
                "lon": data.get("lon"),
            }
        return {"gps_fix": False, "lat": None, "lon": None}

    def _build_alive_payload(self):
        system_status = self._load_system_status()
        fsm_bits, ll_bits = build_status_bitmaps(system_status)
        ais = self._load_latest_ais_position()
        payload = build_alive_payload(
            timestamp=datetime.now(timezone.utc),
            fsm_status_bits=fsm_bits,
            ll_status_bits=ll_bits,
            gps_fix=ais["gps_fix"],
            lat=ais["lat"],
            lon=ais["lon"],
        )
        details = {
            "message_type": "alive",
            "payload_size_bytes": ALIVE_PAYLOAD_SIZE,
            "fsm_status_bits": fsm_bits,
            "ll_status_bits": ll_bits,
            "gps_fix": ais["gps_fix"],
            "lat_present": ais["lat"] is not None,
            "lon_present": ais["lon"] is not None,
        }
        return payload, details

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
        elif message.id in (MessageID.SIG_ACQUIRE, MessageID.SIG_TIMEOUT):
            self._pending_params = {}
            self.set_state(State.ACQUIRE, self.status_queue)
        elif message.id == MessageID.SIG_TRANSMIT:
            self._pending_params = {
                "mode": params.get("mode", "text"),
                "payload": params.get("payload"),
                "text": params.get("text"),
                "clear_after_success": params.get("clear_after_success", True),
                "max_attempts": params.get("max_attempts", 3),
                "retry_delay_s": params.get("retry_delay_s", 10.0),
            }
            self.set_state(State.TRANSMIT, self.status_queue)

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
            status = self.ll.check_status()
            self._emit_action_result("acquire", ResultCode.OK, data={"status": status})
            self.set_state(State.IDLE, self.status_queue)
            self._on_entry_flag = False

        elif self.state == State.TRANSMIT and self._on_entry_flag:
            self.logger.info("Entering TRANSMIT")
            mode = self._pending_params.get("mode")
            max_attempts = int(self._pending_params.get("max_attempts", 3))
            retry_delay_s = float(self._pending_params.get("retry_delay_s", 10.0))
            clear_after_success = bool(self._pending_params.get("clear_after_success", True))
            try:
                if mode == "alive":
                    payload, alive_details = self._build_alive_payload()
                    ok, transmit_details = self.ll.send_sbd_binary(
                        payload,
                        clear_after_success=clear_after_success,
                        max_attempts=max_attempts,
                        retry_delay_s=retry_delay_s,
                    )
                    details = {"alive": alive_details, "transmit": transmit_details}
                elif mode == "binary":
                    payload = self._pending_params.get("payload")
                    if not isinstance(payload, (bytes, bytearray)):
                        raise ValueError("binary transmit requires bytes payload")
                    ok, details = self.ll.send_sbd_binary(
                        bytes(payload),
                        clear_after_success=clear_after_success,
                        max_attempts=max_attempts,
                        retry_delay_s=retry_delay_s,
                    )
                else:
                    text = self._pending_params.get("text")
                    if text is None:
                        payload = self._pending_params.get("payload")
                        text = payload.decode("utf-8") if isinstance(payload, bytes) else str(payload or "")
                    ok, details = self.ll.send_sbd_text(
                        text,
                        clear_after_success=clear_after_success,
                        max_attempts=max_attempts,
                        retry_delay_s=retry_delay_s,
                    )

                result = ResultCode.OK if ok else ResultCode.ERROR
                self._emit_action_result("transmit", result, details=details)
                self.set_state(State.IDLE if ok else State.ERROR, self.status_queue)
            except Exception as exc:
                self.logger.exception("Transmit failed: %s", exc)
                self._emit_action_result("transmit", ResultCode.ERROR, error=str(exc))
                self.set_state(State.ERROR, self.status_queue)
            self._on_entry_flag = False

        elif self.state == State.DISABLE and self._on_entry_flag:
            self.logger.info("Entering DISABLE")
            self.ll.deinit()
            self._on_entry_flag = False

        elif self.state == State.ERROR and self._on_entry_flag:
            self.logger.error("Entering ERROR")
            self.ll.deinit()
            self._on_entry_flag = False
