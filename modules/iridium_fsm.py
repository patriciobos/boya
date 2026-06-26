import json
import os
import time
from pathlib import Path

from modules.support.base_fsm import BaseHandlerFSM, State, Message, MessageID, ResultCode
from modules.support.iridium_protocol import (
    AUDIOPROC_CRC_SIZE,
    AUDIOPROC_HEADER_SIZE,
    MSG_BOOT,
    build_audio_proc_payload,
    BOOT_PAYLOAD_SIZE,
    SYSTEM_STATUS_PAYLOAD_SIZE,
    build_status_flags,
    build_status_ok_bitmaps,
    pack_boot_payload,
    pack_system_status,
    decode_audio_proc_payload,
    expected_audio_band_count,
    status_details,
)
from modules.support.ll_factory import get_low_level_class
from modules.support.storage_guard import (
    GIB_BYTES,
    RECORDING_INTERRUPTED,
    RECORDING_SKIPPED_INSUFFICIENT_SPACE_FOR_EXPECTED_FILE,
    RECORDING_SKIPPED_INVALID_AUDIO_CONFIG,
    RECORDING_SKIPPED_LOW_STORAGE,
    RECORDING_SKIPPED_RECORDINGS_QUOTA_EXCEEDED,
    RECORDING_STOPPED_AUDIO_ERROR,
    RECORDING_STOPPED_MAX_DURATION,
    RECORDING_STOPPED_MAX_FILE_SIZE,
    STORAGE_CRITICAL_LOW_FREE_SPACE,
    STORAGE_ERROR_RECORDINGS_DIR_NOT_ON_STORAGE,
    STORAGE_ERROR_RECORDINGS_DIR_NOT_WRITABLE,
    STORAGE_ERROR_RECORDINGS_DIR_UNAVAILABLE,
    STORAGE_WARNING_LOW_FREE_SPACE,
    disk_free_bytes,
    get_directory_size_bytes,
    validate_recordings_dir,
)
from modules.support.system_config import PROJECT_ROOT, get_config_value, get_data_path, get_logs_path, now_utc_minus_3, utc_minus_3_timestamp


class AudioProcPayloadUnavailable(ValueError):
    pass


class IridiumHandlerFSM(BaseHandlerFSM):
    def __init__(self):
        super().__init__("Iridium")
        self.ll = get_low_level_class("Iridium")()
        self.status_queue = None
        self._pending_params = {}
        self._boot_message_sent = False

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

    def _load_latest_jsonl_data(self, path: Path):
        if not path.exists():
            return {}
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            return entry.get("data") or {}
        return {}

    def _battery_summary(self):
        data = self._load_latest_jsonl_data(get_data_path() / "xtra2210_readings.jsonl")
        voltage_v = data.get("battery_voltage_v")
        soc_percent = data.get("battery_soc_pct")
        voltage_mv = None
        try:
            voltage_mv = None if voltage_v is None else int(round(float(voltage_v) * 1000.0))
        except (TypeError, ValueError):
            voltage_mv = None

        warning_voltage_mv = get_config_value("battery_warning_voltage_mv", 11800)
        critical_voltage_mv = get_config_value("battery_critical_voltage_mv", 11100)
        warning_soc = get_config_value("battery_warning_soc_percent", 20)
        critical_soc = get_config_value("battery_critical_soc_percent", 10)

        battery_warning = False
        battery_critical = False
        try:
            if voltage_mv is not None and voltage_mv <= int(critical_voltage_mv):
                battery_critical = True
            elif voltage_mv is not None and voltage_mv <= int(warning_voltage_mv):
                battery_warning = True
        except (TypeError, ValueError):
            pass
        try:
            soc = None if soc_percent is None else float(soc_percent)
            if soc is not None and soc <= float(critical_soc):
                battery_critical = True
            elif soc is not None and soc <= float(warning_soc):
                battery_warning = True
        except (TypeError, ValueError):
            pass
        if battery_critical:
            battery_warning = True

        return {
            "battery_voltage_mv": voltage_mv,
            "battery_soc_percent": soc_percent,
            "battery_warning": battery_warning,
            "battery_critical": battery_critical,
        }

    def _storage_summary(self):
        recordings_dir = str(get_config_value("recordings_dir", "/storage/boya/recordings"))
        warning_bytes = int(get_config_value("storage_guard_min_free_warning_bytes", 100 * GIB_BYTES))
        critical_bytes = int(get_config_value("storage_guard_min_free_critical_bytes", 50 * GIB_BYTES))
        max_recordings_dir_bytes = int(get_config_value("storage_guard_max_recordings_dir_bytes", 860 * GIB_BYTES))

        validation = validate_recordings_dir(recordings_dir, create=False)
        errors = set(validation.errors)
        storage_unavailable = bool(
            STORAGE_ERROR_RECORDINGS_DIR_UNAVAILABLE in errors
            or STORAGE_ERROR_RECORDINGS_DIR_NOT_ON_STORAGE in errors
            or not Path("/storage").exists()
            or not os.path.ismount("/storage")
        )
        storage_not_writable = STORAGE_ERROR_RECORDINGS_DIR_NOT_WRITABLE in errors

        free_bytes = None
        used_bytes = None
        storage_warning = False
        storage_critical = False
        storage_quota_exceeded = False
        if not storage_unavailable:
            try:
                free_bytes = disk_free_bytes(recordings_dir)
                used_bytes = get_directory_size_bytes(recordings_dir)
                if free_bytes < critical_bytes:
                    storage_critical = True
                    storage_warning = True
                elif free_bytes < warning_bytes:
                    storage_warning = True
                storage_quota_exceeded = used_bytes > max_recordings_dir_bytes
            except OSError:
                storage_unavailable = True
                free_bytes = None

        return {
            "recordings_dir": recordings_dir,
            "storage_unavailable": storage_unavailable,
            "storage_not_writable": storage_not_writable,
            "storage_warning": storage_warning,
            "storage_critical": storage_critical,
            "storage_quota_exceeded": storage_quota_exceeded,
            "storage_free_gib": None if free_bytes is None else free_bytes / GIB_BYTES,
            "recordings_dir_used_bytes": used_bytes,
            "validation_errors": validation.errors,
        }

    def _uptime_minutes(self):
        try:
            with open("/proc/uptime", "r", encoding="utf-8") as handle:
                return int(float(handle.read().split()[0]) // 60)
        except (OSError, ValueError, IndexError):
            return int(time.monotonic() // 60)

    def _boot_message_enabled(self):
        return bool(get_config_value("iridium_boot_message_enabled", True))

    def _send_boot_message(self):
        if self._boot_message_sent:
            return True, {"mode": "boot", "skipped": True, "reason": "boot_message_already_sent"}

        self._boot_message_sent = True
        if not self._boot_message_enabled():
            details = {"mode": "boot", "skipped": True, "reason": "iridium_boot_message_disabled"}
            self.logger.info("Iridium boot message disabled by configuration")
            return True, details

        try:
            uptime_minutes = self._uptime_minutes()
            payload = pack_boot_payload(uptime_minutes)
        except ValueError as exc:
            self.logger.warning("Boot message payload validation failed: %s", exc)
            return False, {"mode": "boot", "skipped": True, "reason": "invalid_uptime_minutes", "error": str(exc)}

        details = {
            "message_type": "boot",
            "message_type_byte": MSG_BOOT,
            "payload_size_bytes": len(payload),
            "uptime_minutes": uptime_minutes,
        }
        ok, transmit_details = self._send_binary_or_log(
            payload,
            details,
            clear_after_success=True,
            max_attempts=1,
            retry_delay_s=0.0,
        )
        return ok, transmit_details

    def _last_acquisition_incomplete(self, system_status):
        incomplete_reasons = {
            RECORDING_INTERRUPTED,
            RECORDING_SKIPPED_INSUFFICIENT_SPACE_FOR_EXPECTED_FILE,
            RECORDING_SKIPPED_INVALID_AUDIO_CONFIG,
            RECORDING_SKIPPED_LOW_STORAGE,
            RECORDING_SKIPPED_RECORDINGS_QUOTA_EXCEEDED,
            RECORDING_STOPPED_AUDIO_ERROR,
            RECORDING_STOPPED_MAX_DURATION,
            RECORDING_STOPPED_MAX_FILE_SIZE,
        }
        for module_status in (system_status.get("modules") or {}).values():
            result = str(module_status.get("last_result") or "").lower()
            details = module_status.get("last_details") or {}
            if result == "error" or self._details_have_errors(details):
                return True
            if self._details_contain_any(details, incomplete_reasons):
                return True
        return False

    def _details_have_errors(self, value):
        if isinstance(value, dict):
            if value.get("errors") or value.get("error"):
                return True
            return any(self._details_have_errors(item) for item in value.values())
        if isinstance(value, list):
            return any(self._details_have_errors(item) for item in value)
        return False

    def _details_contain_any(self, value, needles):
        if isinstance(value, dict):
            return any(self._details_contain_any(item, needles) for item in value.values())
        if isinstance(value, list):
            return any(self._details_contain_any(item, needles) for item in value)
        return str(value) in needles

    def _build_system_status_payload(self):
        system_status = self._load_system_status()
        fsm_ok_bitmap, ll_ok_bitmap = build_status_ok_bitmaps(system_status)
        storage = self._storage_summary()
        battery = self._battery_summary()
        status_flags = build_status_flags(
            storage_unavailable=storage["storage_unavailable"],
            storage_not_writable=storage["storage_not_writable"],
            storage_warning=storage["storage_warning"],
            storage_critical=storage["storage_critical"],
            storage_quota_exceeded=storage["storage_quota_exceeded"],
            battery_warning=battery["battery_warning"],
            battery_critical=battery["battery_critical"],
            last_acquisition_incomplete=self._last_acquisition_incomplete(system_status),
        )
        payload = pack_system_status(
            fsm_ok_bitmap=fsm_ok_bitmap,
            ll_ok_bitmap=ll_ok_bitmap,
            status_flags=status_flags,
            battery_voltage_mv=battery["battery_voltage_mv"],
            battery_soc_percent=battery["battery_soc_percent"],
            storage_free_gib=storage["storage_free_gib"],
            uptime_minutes=self._uptime_minutes(),
            storage_unavailable=storage["storage_unavailable"],
        )
        details = {
            "message_type": "system_status",
            "message_type_name": "MSG_SYSTEM_STATUS",
            "payload_size_bytes": SYSTEM_STATUS_PAYLOAD_SIZE,
            **status_details(fsm_ok_bitmap, ll_ok_bitmap),
            "status_flags_raw": status_flags,
            "battery": battery,
            "storage": storage,
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
        decoded = decode_audio_proc_payload(payload, expected_band_count=expected_bands)
        audio_payload_bytes = max(0, len(payload) - AUDIOPROC_HEADER_SIZE - AUDIOPROC_CRC_SIZE)
        channel_count = decoded["channel_count"]
        details = {
            "message_type": "audioProc",
            "message_type_byte": message_type,
            "packing": decoded["packing"],
            "payload_size_bytes": len(payload),
            "header_size_bytes": AUDIOPROC_HEADER_SIZE,
            "crc_size_bytes": AUDIOPROC_CRC_SIZE,
            "frequency_band_count": expected_bands,
            "channel_count": channel_count,
            "audio_payload_size_bytes": audio_payload_bytes,
            "audio_value_count": expected_bands * channel_count,
            "bytes_per_channel": audio_payload_bytes // channel_count if channel_count else 0,
            "audio_output": (audio or {}).get("output") or audio_data.get("output"),
            "audio_timestamp": audio_data["timestamp"],
            "encoding": "relative_band_power_db_q0.1_crc16_ccitt_false",
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
        elif message.id in (MessageID.SIG_ACQUIRE, MessageID.SIG_TIMEOUT):
            self._pending_params = {}
            self.set_state(State.ACQUIRE, self.status_queue)
        elif message.id == MessageID.SIG_TRANSMIT:
            self._pending_params = {
                "mode": params.get("mode", "system_status"),
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
            if ok:
                boot_ok, boot_details = self._send_boot_message()
                if not boot_ok:
                    self.logger.warning("Iridium boot message failed after successful test: %s", boot_details)
                self.set_state(State.IDLE, self.status_queue)
            else:
                self.set_state(State.ERROR, self.status_queue)
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
                if mode in ("alive", "system_status"):
                    payload, system_status_details = self._build_system_status_payload()
                    ok, transmit_details = self._send_binary_or_log(
                        payload,
                        system_status_details,
                        clear_after_success,
                        max_attempts,
                        retry_delay_s,
                    )
                    details = {"system_status": system_status_details, "transmit": transmit_details}
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
                            "allowed_modes": ["system_status", "alive", "audio"],
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
