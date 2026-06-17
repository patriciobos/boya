import json
from pathlib import Path

from modules.support.base_fsm import BaseHandlerFSM, State, Message, MessageID, ResultCode
from modules.support.iridium_protocol import (
    ALIVE_PAYLOAD_SIZE,
    AUDIOPROC_CRC_SIZE,
    AUDIOPROC_HEADER_SIZE,
    MESSAGE_TYPE_AUDIO_MONO,
    MESSAGE_TYPE_AUDIO_STEREO,
    build_alive_payload,
    build_audio_proc_payload,
    build_status_bitmaps,
    expected_audio_band_count,
    status_details,
)
from modules.support.ll_factory import get_low_level_class
from modules.support.system_config import PROJECT_ROOT, get_config_value, get_data_path, get_logs_path, now_utc_minus_3, utc_minus_3_timestamp


class AudioProcPayloadUnavailable(ValueError):
    pass


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
            timestamp=now_utc_minus_3(),
            fsm_status_bits=fsm_bits,
            ll_status_bits=ll_bits,
            gps_fix=ais["gps_fix"],
            lat=ais["lat"],
            lon=ais["lon"],
        )
        details = {
            "message_type": "alive",
            "payload_size_bytes": ALIVE_PAYLOAD_SIZE,
            **status_details(fsm_bits, ll_bits),
            "gps_fix": ais["gps_fix"],
            "lat_present": ais["lat"] is not None,
            "lon_present": ais["lon"] is not None,
        }
        return payload, details

    def _resolve_audio_output_path(self, output_path):
        if not output_path:
            raise ValueError("audio transmit requires output path")
        path = Path(str(output_path))
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path

    def _load_latest_audio_output(self):
        path = get_data_path() / "audioProc_readings.jsonl"
        if not path.exists():
            raise AudioProcPayloadUnavailable("No AudioProc readings are available")
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            data = entry.get("data") or {}
            output_file = data.get("output_file") or data.get("output")
            if not output_file:
                continue
            output_path = self._resolve_audio_output_path(output_file)
            if output_path.exists():
                return {"output": output_file}
        raise AudioProcPayloadUnavailable("No valid AudioProc output is available")

    def _load_audio_proc_data(self, audio):
        audio = audio or self._load_latest_audio_output()
        if "relative_band_power_db" in audio:
            return audio
        if not audio.get("output"):
            audio = self._load_latest_audio_output()
        output_path = self._resolve_audio_output_path(audio.get("output"))
        try:
            data = json.loads(output_path.read_text(encoding="utf-8"))
            data.setdefault("output", audio.get("output"))
            return data
        except FileNotFoundError as exc:
            raise ValueError(f"AudioProc output not found: {output_path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid AudioProc output JSON: {output_path}: {exc}") from exc

    def _build_audio_proc_payload(self, audio):
        audio_data = self._load_audio_proc_data(audio)
        bands = audio_data.get("relative_band_power_db")
        if bands is None:
            raise AudioProcPayloadUnavailable("AudioProc relative_band_power_db is not available")
        if not audio_data.get("timestamp"):
            raise AudioProcPayloadUnavailable("AudioProc timestamp is not available")

        expected_bands = expected_audio_band_count()
        payload = build_audio_proc_payload(
            timestamp=audio_data["timestamp"],
            relative_band_power_db=bands,
            expected_band_count=expected_bands,
        )
        message_type = payload[0]
        channel_count = 1 if message_type == MESSAGE_TYPE_AUDIO_MONO else 2
        audio_value_count = max(0, len(payload) - AUDIOPROC_HEADER_SIZE - AUDIOPROC_CRC_SIZE)
        details = {
            "message_type": "audioProc",
            "message_type_byte": message_type,
            "payload_size_bytes": len(payload),
            "header_size_bytes": AUDIOPROC_HEADER_SIZE,
            "crc_size_bytes": AUDIOPROC_CRC_SIZE,
            "frequency_band_count": expected_bands,
            "channel_count": channel_count,
            "audio_value_count": audio_value_count,
            "bytes_per_channel": expected_bands,
            "audio_output": (audio or {}).get("output") or audio_data.get("output"),
            "audio_timestamp": audio_data["timestamp"],
            "encoding": "int8_db_null_-128_crc16_ccitt_false",
        }
        return payload, details

    def _transmit_enabled(self):
        return bool(get_config_value("iridium_transmit_enabled", True))

    def _log_transmit_request(self, mode, payload, details, skipped_reason=None):
        entry = {
            "timestamp": utc_minus_3_timestamp(),
            "mode": mode,
            "payload_size_bytes": len(payload) if isinstance(payload, (bytes, bytearray)) else None,
            "payload_hex": bytes(payload).hex() if isinstance(payload, (bytes, bytearray)) else None,
            "details": details,
        }
        if skipped_reason:
            entry["skipped_reason"] = skipped_reason
        path = get_logs_path() / "iridium_transmit_requests.jsonl"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n")
        self.logger.info(
            "Iridium transmit request logged: mode=%s size=%s skipped_reason=%s",
            entry["mode"],
            entry["payload_size_bytes"],
            skipped_reason,
        )

    def _send_binary_or_log(self, payload, details, clear_after_success, max_attempts, retry_delay_s):
        if not self._transmit_enabled():
            skipped = {
                "mode": "binary",
                "skipped": True,
                "reason": "iridium_transmit_disabled",
            }
            self._log_transmit_request("binary", payload, details, skipped_reason=skipped["reason"])
            return True, skipped

        self._log_transmit_request("binary", payload, details)
        return self.ll.send_sbd_binary(
            payload,
            clear_after_success=clear_after_success,
            max_attempts=max_attempts,
            retry_delay_s=retry_delay_s,
        )

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
                "mode": params.get("mode", "alive"),
                "payload": params.get("payload"),
                "text": params.get("text"),
                "audio": params.get("audio"),
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
                    ok, transmit_details = self._send_binary_or_log(
                        payload,
                        alive_details,
                        clear_after_success,
                        max_attempts,
                        retry_delay_s,
                    )
                    details = {"alive": alive_details, "transmit": transmit_details}
                elif mode == "audio":
                    try:
                        payload, audio_details = self._build_audio_proc_payload(self._pending_params.get("audio"))
                    except (AudioProcPayloadUnavailable, ValueError) as exc:
                        ok = True
                        details = {
                            "audio": {
                                "message_type": "audioProc",
                                "skipped": True,
                                "reason": "audioProc_payload_unavailable",
                                "error": str(exc),
                            },
                            "transmit": {
                                "mode": "binary",
                                "skipped": True,
                                "reason": "audioProc_payload_unavailable",
                            },
                        }
                    else:
                        ok, transmit_details = self._send_binary_or_log(
                            payload,
                            audio_details,
                            clear_after_success,
                            max_attempts,
                            retry_delay_s,
                        )
                        details = {"audio": audio_details, "transmit": transmit_details}
                else:
                    ok = True
                    details = {
                        "transmit": {
                            "mode": mode,
                            "skipped": True,
                            "reason": "unsupported_transmit_mode",
                            "allowed_modes": ["alive", "audio"],
                        }
                    }
                    self.logger.warning("Skipping unsupported Iridium transmit mode: %s", mode)

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
