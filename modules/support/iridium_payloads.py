from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from modules.support.iridium_protocol import (
    AUDIOPROC_CRC_SIZE,
    AUDIOPROC_HEADER_SIZE,
    SYSTEM_STATUS_PAYLOAD_SIZE,
    build_audio_proc_payload,
    build_status_flags,
    build_status_ok_bitmaps,
    decode_audio_proc_payload,
    expected_audio_band_count,
    pack_system_status,
    status_details,
)
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
    STORAGE_ERROR_RECORDINGS_DIR_NOT_ON_STORAGE,
    STORAGE_ERROR_RECORDINGS_DIR_NOT_WRITABLE,
    STORAGE_ERROR_RECORDINGS_DIR_UNAVAILABLE,
    disk_free_bytes,
    get_directory_size_bytes,
    validate_recordings_dir,
)
from modules.support.system_config import PROJECT_ROOT, get_config_value, get_data_path, get_logs_path


class AudioProcPayloadUnavailable(ValueError):
    pass


def load_system_status() -> dict[str, Any]:
    path = get_logs_path() / "system_status.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def load_latest_jsonl_data(path: Path) -> dict[str, Any]:
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


def battery_summary() -> dict[str, Any]:
    data = load_latest_jsonl_data(get_data_path() / "xtra2210_readings.jsonl")
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


def storage_summary() -> dict[str, Any]:
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


def uptime_minutes() -> int:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            return int(float(handle.read().split()[0]) // 60)
    except (OSError, ValueError, IndexError):
        return int(time.monotonic() // 60)


def details_have_errors(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("errors") or value.get("error"):
            return True
        return any(details_have_errors(item) for item in value.values())
    if isinstance(value, list):
        return any(details_have_errors(item) for item in value)
    return False


def details_contain_any(value: Any, needles: set[str]) -> bool:
    if isinstance(value, dict):
        return any(details_contain_any(item, needles) for item in value.values())
    if isinstance(value, list):
        return any(details_contain_any(item, needles) for item in value)
    return str(value) in needles


def last_acquisition_incomplete(system_status: dict[str, Any]) -> bool:
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
        if result == "error" or details_have_errors(details):
            return True
        if details_contain_any(details, incomplete_reasons):
            return True
    return False


def build_current_system_status_payload() -> tuple[bytes, dict[str, Any]]:
    system_status = load_system_status()
    fsm_ok_bitmap, ll_ok_bitmap = build_status_ok_bitmaps(system_status)
    storage = storage_summary()
    battery = battery_summary()
    status_flags = build_status_flags(
        storage_unavailable=storage["storage_unavailable"],
        storage_not_writable=storage["storage_not_writable"],
        storage_warning=storage["storage_warning"],
        storage_critical=storage["storage_critical"],
        storage_quota_exceeded=storage["storage_quota_exceeded"],
        battery_warning=battery["battery_warning"],
        battery_critical=battery["battery_critical"],
        last_acquisition_incomplete=last_acquisition_incomplete(system_status),
    )
    payload = pack_system_status(
        fsm_ok_bitmap=fsm_ok_bitmap,
        ll_ok_bitmap=ll_ok_bitmap,
        status_flags=status_flags,
        battery_voltage_mv=battery["battery_voltage_mv"],
        battery_soc_percent=battery["battery_soc_percent"],
        storage_free_gib=storage["storage_free_gib"],
        uptime_minutes=uptime_minutes(),
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


def resolve_audio_output_path(output_path: Any) -> Path:
    if not output_path:
        raise ValueError("audio transmit requires output path")
    path = Path(str(output_path))
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def load_latest_audioproc_output() -> dict[str, Any]:
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
        output_path = resolve_audio_output_path(output_file)
        if output_path.exists():
            return {"output": output_file, "source_reading": str(path)}
    raise AudioProcPayloadUnavailable("No valid AudioProc output is available")


def load_audio_proc_data(audio: dict[str, Any] | None = None) -> dict[str, Any]:
    audio = audio or load_latest_audioproc_output()
    if "relative_band_power_db" in audio:
        return audio
    if not audio.get("output"):
        audio = load_latest_audioproc_output()
    output_path = resolve_audio_output_path(audio.get("output"))
    try:
        data = json.loads(output_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"AudioProc output not found: {output_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid AudioProc output JSON: {output_path}: {exc}") from exc

    data.setdefault("output", audio.get("output"))
    data.setdefault("source_file", str(output_path))
    if audio.get("source_reading"):
        data.setdefault("source_reading", audio["source_reading"])
    return data


def build_latest_audio_proc_payload(audio: dict[str, Any] | None = None) -> tuple[bytes, dict[str, Any]]:
    audio_data = load_audio_proc_data(audio)
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
        "message_type_name": "MSG_AUDIO",
        "packing": decoded["packing"],
        "payload_size_bytes": len(payload),
        "header_size_bytes": AUDIOPROC_HEADER_SIZE,
        "crc_size_bytes": AUDIOPROC_CRC_SIZE,
        "frequency_band_count": expected_bands,
        "channel_count": channel_count,
        "audio_payload_size_bytes": audio_payload_bytes,
        "audio_value_count": expected_bands * channel_count,
        "bytes_per_channel": audio_payload_bytes // channel_count if channel_count else 0,
        "audio_output": audio_data.get("output"),
        "audio_source_file": audio_data.get("source_file"),
        "audio_source_reading": audio_data.get("source_reading"),
        "audio_timestamp": audio_data["timestamp"],
        "encoding": "relative_band_power_db_q0.1_crc16_ccitt_false",
    }
    return payload, details
