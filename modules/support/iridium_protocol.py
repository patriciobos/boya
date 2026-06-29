from __future__ import annotations

import csv
import math
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from modules.support.system_config import PROJECT_ROOT, get_config_value

MSG_SYSTEM_STATUS_V1 = 0x01
MSG_SYSTEM_STATUS = MSG_SYSTEM_STATUS_V1
MSG_BOOT_V1 = 0x02
MSG_BOOT = MSG_BOOT_V1
MESSAGE_TYPE_AUDIO_MONO_DELTA_PREVIOUS_INT8 = 0x03
MESSAGE_TYPE_AUDIO_STEREO_DELTA_PREVIOUS_INT8 = 0x04
MESSAGE_TYPE_AUDIO_MONO_ABS_INT16 = 0x05
MESSAGE_TYPE_AUDIO_STEREO_ABS_INT16 = 0x06
SYSTEM_STATUS_PAYLOAD_FORMAT = ">BBBBHBHH"
BOOT_PAYLOAD_FORMAT = ">BH"
BOOT_PAYLOAD_SIZE = struct.calcsize(BOOT_PAYLOAD_FORMAT)
SYSTEM_STATUS_PAYLOAD_SIZE = struct.calcsize(SYSTEM_STATUS_PAYLOAD_FORMAT)
SYSTEM_STATUS_UNKNOWN_U16 = 0xFFFF
SYSTEM_STATUS_UNKNOWN_U8 = 0xFF
SYSTEM_STATUS_MAX_U16_VALUE = 0xFFFE
SYSTEM_STATUS_FLAG_STORAGE_UNAVAILABLE = 0x01
SYSTEM_STATUS_FLAG_STORAGE_NOT_WRITABLE = 0x02
SYSTEM_STATUS_FLAG_STORAGE_WARNING = 0x04
SYSTEM_STATUS_FLAG_STORAGE_CRITICAL = 0x08
SYSTEM_STATUS_FLAG_STORAGE_QUOTA_EXCEEDED = 0x10
SYSTEM_STATUS_FLAG_BATTERY_WARNING = 0x20
SYSTEM_STATUS_FLAG_BATTERY_CRITICAL = 0x40
SYSTEM_STATUS_FLAG_LAST_ACQUISITION_INCOMPLETE = 0x80
SYSTEM_STATUS_FLAG_FIELDS = {
    "storage_unavailable": SYSTEM_STATUS_FLAG_STORAGE_UNAVAILABLE,
    "storage_not_writable": SYSTEM_STATUS_FLAG_STORAGE_NOT_WRITABLE,
    "storage_warning": SYSTEM_STATUS_FLAG_STORAGE_WARNING,
    "storage_critical": SYSTEM_STATUS_FLAG_STORAGE_CRITICAL,
    "storage_quota_exceeded": SYSTEM_STATUS_FLAG_STORAGE_QUOTA_EXCEEDED,
    "battery_warning": SYSTEM_STATUS_FLAG_BATTERY_WARNING,
    "battery_critical": SYSTEM_STATUS_FLAG_BATTERY_CRITICAL,
    "last_acquisition_incomplete": SYSTEM_STATUS_FLAG_LAST_ACQUISITION_INCOMPLETE,
}
AUDIOPROC_HEADER_FORMAT = ">BI"
AUDIOPROC_HEADER_SIZE = struct.calcsize(AUDIOPROC_HEADER_FORMAT)
AUDIOPROC_CRC_SIZE = 2
AUDIOPROC_ABS_INT16_SENTINEL = -32768
AUDIOPROC_ABS_INT16_MIN = -32767
AUDIOPROC_ABS_INT16_MAX = 32767
AUDIOPROC_DELTA_INT8_SENTINEL = -128
AUDIOPROC_DELTA_INT8_MIN = -127
AUDIOPROC_DELTA_INT8_MAX = 127
AUDIOPROC_DB_SCALE = 10.0
AUDIO_PACKING_DELTA_PREVIOUS_INT8 = "DELTA_PREVIOUS_INT8"
AUDIO_PACKING_ABS_INT16 = "ABS_INT16"
COORD_SCALE = 10_000_000
NO_COORD = 0x7FFFFFFF

MODULE_ORDER = (
    "AHT10",
    "AIS",
    "AudioProc",
    "Behringer",
    "Iridium",
    "MPU6050",
    "Windsonic",
    "XTRA2210",
)


def utc_epoch_seconds(timestamp: datetime | int | float | str | None = None) -> int:
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if isinstance(timestamp, datetime):
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        timestamp = timestamp.astimezone(timezone.utc).timestamp()
    epoch = int(timestamp)
    if not 0 <= epoch <= 0xFFFFFFFF:
        raise ValueError("timestamp is outside uint32 range")
    return epoch


def encode_coordinate(value: float | int | None, minimum: float, maximum: float) -> int:
    if value is None:
        return NO_COORD
    numeric = float(value)
    if not minimum <= numeric <= maximum:
        raise ValueError(f"coordinate {numeric} outside range {minimum}..{maximum}")
    return int(round(numeric * COORD_SCALE))


def _finite_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def encode_battery_voltage_mv(value: Any) -> int:
    numeric = _finite_number(value)
    if numeric is None:
        return SYSTEM_STATUS_UNKNOWN_U16
    return max(0, min(SYSTEM_STATUS_MAX_U16_VALUE, int(round(numeric))))


def encode_battery_soc_percent(value: Any) -> int:
    numeric = _finite_number(value)
    if numeric is None:
        return SYSTEM_STATUS_UNKNOWN_U8
    return max(0, min(100, int(round(numeric))))


def encode_storage_free_gib_x10(value: Any) -> int:
    numeric = _finite_number(value)
    if numeric is None:
        return SYSTEM_STATUS_UNKNOWN_U16
    encoded = int(round(numeric * 10.0))
    return max(0, min(SYSTEM_STATUS_MAX_U16_VALUE, encoded))


def encode_uptime_minutes(value: Any) -> int:
    numeric = _finite_number(value)
    if numeric is None:
        return SYSTEM_STATUS_UNKNOWN_U16
    encoded = int(round(numeric))
    if encoded < 0:
        return 0
    if encoded > SYSTEM_STATUS_MAX_U16_VALUE:
        return SYSTEM_STATUS_UNKNOWN_U16
    return encoded


def encode_boot_uptime_minutes(value: Any) -> int:
    numeric = _finite_number(value)
    if numeric is None:
        raise ValueError("uptime_minutes must be a finite numeric value")
    uptime = int(round(numeric))
    if not 0 <= uptime <= 0xFFFF:
        raise ValueError("uptime_minutes must be in range 0..65535")
    return uptime


def pack_boot_payload(uptime_minutes: Any) -> bytes:
    uptime_encoded = encode_boot_uptime_minutes(uptime_minutes)
    return struct.pack(BOOT_PAYLOAD_FORMAT, MSG_BOOT_V1, uptime_encoded)


def unpack_boot_message(payload: bytes) -> dict[str, Any]:
    if len(payload) != BOOT_PAYLOAD_SIZE:
        raise ValueError(f"boot message payload must be {BOOT_PAYLOAD_SIZE} bytes")
    message_type, uptime_minutes = struct.unpack(BOOT_PAYLOAD_FORMAT, payload)
    if message_type != MSG_BOOT_V1:
        raise ValueError(f"invalid boot message type: {message_type}")
    return {
        "message_type": "MSG_BOOT",
        "message_type_byte": message_type,
        "uptime_minutes": uptime_minutes,
    }


def build_status_flags(
    *,
    storage_unavailable: bool = False,
    storage_not_writable: bool = False,
    storage_warning: bool = False,
    storage_critical: bool = False,
    storage_quota_exceeded: bool = False,
    battery_warning: bool = False,
    battery_critical: bool = False,
    last_acquisition_incomplete: bool = False,
) -> int:
    if storage_critical:
        storage_warning = True
    if battery_critical:
        battery_warning = True

    flags = 0
    values = {
        "storage_unavailable": storage_unavailable,
        "storage_not_writable": storage_not_writable,
        "storage_warning": storage_warning,
        "storage_critical": storage_critical,
        "storage_quota_exceeded": storage_quota_exceeded,
        "battery_warning": battery_warning,
        "battery_critical": battery_critical,
        "last_acquisition_incomplete": last_acquisition_incomplete,
    }
    for name, enabled in values.items():
        if enabled:
            flags |= SYSTEM_STATUS_FLAG_FIELDS[name]
    return flags & 0xFF


def decode_status_flags(status_flags: int) -> dict[str, bool]:
    raw = int(status_flags) & 0xFF
    return {name: bool(raw & bit) for name, bit in SYSTEM_STATUS_FLAG_FIELDS.items()}


def pack_system_status(
    *,
    fsm_ok_bitmap: int,
    ll_ok_bitmap: int,
    battery_voltage_mv: Any = None,
    battery_soc_percent: Any = None,
    storage_free_gib: Any = None,
    uptime_minutes: Any = None,
    status_flags: int | None = None,
    storage_unavailable: bool = False,
    storage_not_writable: bool = False,
    storage_warning: bool = False,
    storage_critical: bool = False,
    storage_quota_exceeded: bool = False,
    battery_warning: bool = False,
    battery_critical: bool = False,
    last_acquisition_incomplete: bool = False,
) -> bytes:
    if status_flags is None:
        status_flags = build_status_flags(
            storage_unavailable=storage_unavailable,
            storage_not_writable=storage_not_writable,
            storage_warning=storage_warning,
            storage_critical=storage_critical,
            storage_quota_exceeded=storage_quota_exceeded,
            battery_warning=battery_warning,
            battery_critical=battery_critical,
            last_acquisition_incomplete=last_acquisition_incomplete,
        )

    storage_encoded = (
        SYSTEM_STATUS_UNKNOWN_U16
        if storage_unavailable
        else encode_storage_free_gib_x10(storage_free_gib)
    )
    return struct.pack(
        SYSTEM_STATUS_PAYLOAD_FORMAT,
        MSG_SYSTEM_STATUS_V1,
        int(fsm_ok_bitmap) & 0xFF,
        int(ll_ok_bitmap) & 0xFF,
        int(status_flags) & 0xFF,
        encode_battery_voltage_mv(battery_voltage_mv),
        encode_battery_soc_percent(battery_soc_percent),
        storage_encoded,
        encode_uptime_minutes(uptime_minutes),
    )


def unpack_system_status(payload: bytes) -> dict[str, Any]:
    if len(payload) != SYSTEM_STATUS_PAYLOAD_SIZE:
        raise ValueError(f"system status payload must be {SYSTEM_STATUS_PAYLOAD_SIZE} bytes")
    (
        message_type,
        fsm_ok_bitmap,
        ll_ok_bitmap,
        status_flags_raw,
        battery_voltage_mv,
        battery_soc_percent,
        storage_free_gib_x10,
        uptime_minutes,
    ) = struct.unpack(SYSTEM_STATUS_PAYLOAD_FORMAT, payload)
    if message_type != MSG_SYSTEM_STATUS_V1:
        raise ValueError(f"invalid system status message type: {message_type}")

    voltage_mv = None if battery_voltage_mv == SYSTEM_STATUS_UNKNOWN_U16 else battery_voltage_mv
    soc_percent = None if battery_soc_percent == SYSTEM_STATUS_UNKNOWN_U8 else battery_soc_percent
    free_gib_x10 = None if storage_free_gib_x10 == SYSTEM_STATUS_UNKNOWN_U16 else storage_free_gib_x10
    uptime = None if uptime_minutes == SYSTEM_STATUS_UNKNOWN_U16 else uptime_minutes
    return {
        "message_type": "MSG_SYSTEM_STATUS",
        "message_type_byte": message_type,
        "fsm_ok_bitmap": fsm_ok_bitmap,
        "ll_ok_bitmap": ll_ok_bitmap,
        "status_flags_raw": status_flags_raw,
        "status_flags": decode_status_flags(status_flags_raw),
        "battery": {
            "voltage_mv": voltage_mv,
            "voltage_v": None if voltage_mv is None else voltage_mv / 1000.0,
            "soc_percent": soc_percent,
        },
        "storage": {
            "free_gib_x10": free_gib_x10,
            "free_gib": None if free_gib_x10 is None else free_gib_x10 / 10.0,
        },
        "uptime_minutes": uptime,
    }


def status_bits_binary(value: int) -> str:
    return format(int(value) & 0xFF, "08b")


def status_details(fsm_ok_bitmap: int, ll_ok_bitmap: int) -> dict[str, Any]:
    fsm_ok_bitmap = int(fsm_ok_bitmap) & 0xFF
    ll_ok_bitmap = int(ll_ok_bitmap) & 0xFF
    return {
        "fsm_ok_bitmap": fsm_ok_bitmap,
        "ll_ok_bitmap": ll_ok_bitmap,
        "fsm_ok_bitmap_binary": status_bits_binary(fsm_ok_bitmap),
        "ll_ok_bitmap_binary": status_bits_binary(ll_ok_bitmap),
        "ok_bytes_binary": f"{status_bits_binary(fsm_ok_bitmap)} {status_bits_binary(ll_ok_bitmap)}",
    }


def expected_audio_band_count(sample_rate_hz: float | int | None = None, bands_path: Path | None = None) -> int:
    if sample_rate_hz is None:
        sample_rate_hz = get_config_value("fs[Hz]", 192000)
    sample_rate_hz = float(sample_rate_hz)
    nyquist = sample_rate_hz / 2.0
    bands_path = bands_path or (PROJECT_ROOT / "support" / "third_octave_bands.csv")

    count = 0
    with open(bands_path, "r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            fl = float(row["fl"])
            fh = float(row["fh"])
            if fl >= 1.0 and fh <= 100000.0 and fh <= nyquist * 0.99:
                count += 1
    return count


def _audio_channel_values(relative_band_power_db: Any) -> tuple[list[list[Any]], int, int]:
    if not isinstance(relative_band_power_db, list):
        raise ValueError("relative_band_power_db must be a list")
    if not relative_band_power_db:
        raise ValueError("relative_band_power_db must not be empty")

    channel_count: int | None = None
    channels: list[list[Any]] = []
    for row in relative_band_power_db:
        row_values = row if isinstance(row, list) else [row]
        if channel_count is None:
            channel_count = len(row_values)
            if channel_count not in (1, 2):
                raise ValueError(f"Unsupported AudioProc channel count: {channel_count}")
            channels = [[] for _ in range(channel_count)]
        elif len(row_values) != channel_count:
            raise ValueError("AudioProc channel count is not consistent across frequency bands")
        for channel_index, value in enumerate(row_values):
            channels[channel_index].append(value)

    return channels, len(relative_band_power_db), int(channel_count or 0)


def _is_invalid_audio_value(value: Any) -> bool:
    if value is None:
        return True
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return True
    return not math.isfinite(numeric)


def quantize_db_tenths(values: Any) -> list[int | None]:
    q_values: list[int | None] = []
    for value in values:
        if _is_invalid_audio_value(value):
            q_values.append(None)
            continue
        q_values.append(int(np.rint(float(value) * AUDIOPROC_DB_SCALE)))
    return q_values


def encode_audio_db_value(value: Any) -> int:
    q_value = quantize_db_tenths([value])[0]
    if q_value is None:
        return AUDIOPROC_ABS_INT16_SENTINEL
    return max(AUDIOPROC_ABS_INT16_MIN, min(AUDIOPROC_ABS_INT16_MAX, q_value))


def can_pack_delta_previous_int8(q_values: list[int | None]) -> bool:
    previous: int | None = None
    for q_value in q_values:
        if q_value is None:
            return False
        if q_value < AUDIOPROC_ABS_INT16_MIN or q_value > AUDIOPROC_ABS_INT16_MAX:
            return False
        if previous is not None:
            delta = q_value - previous
            if delta < AUDIOPROC_DELTA_INT8_MIN or delta > AUDIOPROC_DELTA_INT8_MAX:
                return False
        previous = q_value
    return bool(q_values)


def pack_abs_int16_channel(q_values: list[int | None]) -> bytes:
    payload = bytearray()
    for q_value in q_values:
        if q_value is None:
            encoded = AUDIOPROC_ABS_INT16_SENTINEL
        else:
            encoded = max(AUDIOPROC_ABS_INT16_MIN, min(AUDIOPROC_ABS_INT16_MAX, int(q_value)))
        payload.extend(struct.pack(">h", encoded))
    return bytes(payload)


def unpack_abs_int16_channel(payload: bytes, n_bands: int) -> list[float | None]:
    expected_size = int(n_bands) * 2
    if len(payload) != expected_size:
        raise ValueError(f"ABS_INT16 channel payload must be {expected_size} bytes")
    values: list[float | None] = []
    for index in range(0, len(payload), 2):
        q_value = struct.unpack(">h", payload[index:index + 2])[0]
        values.append(None if q_value == AUDIOPROC_ABS_INT16_SENTINEL else q_value / AUDIOPROC_DB_SCALE)
    return values


def pack_delta_previous_int8_channel(q_values: list[int | None]) -> bytes:
    if not can_pack_delta_previous_int8(q_values):
        raise ValueError("q_values cannot be packed as DELTA_PREVIOUS_INT8")
    payload = bytearray(struct.pack(">h", int(q_values[0])))
    previous = int(q_values[0])
    for q_value in q_values[1:]:
        current = int(q_value)
        payload.extend(struct.pack(">b", current - previous))
        previous = current
    return bytes(payload)


def unpack_delta_previous_int8_channel(payload: bytes, n_bands: int) -> list[float]:
    expected_size = 2 + max(0, int(n_bands) - 1)
    if len(payload) != expected_size:
        raise ValueError(f"DELTA_PREVIOUS_INT8 channel payload must be {expected_size} bytes")
    if n_bands <= 0:
        raise ValueError("n_bands must be positive")
    q_values = [struct.unpack(">h", payload[:2])[0]]
    previous = q_values[0]
    for raw_delta in payload[2:]:
        delta = struct.unpack(">b", bytes([raw_delta]))[0]
        if delta == AUDIOPROC_DELTA_INT8_SENTINEL:
            raise ValueError("DELTA_PREVIOUS_INT8 payload contains reserved sentinel")
        previous = previous + delta
        q_values.append(previous)
    return [q_value / AUDIOPROC_DB_SCALE for q_value in q_values]


def _audio_message_type(channel_count: int, packing: str) -> int:
    if channel_count == 1 and packing == AUDIO_PACKING_DELTA_PREVIOUS_INT8:
        return MESSAGE_TYPE_AUDIO_MONO_DELTA_PREVIOUS_INT8
    if channel_count == 2 and packing == AUDIO_PACKING_DELTA_PREVIOUS_INT8:
        return MESSAGE_TYPE_AUDIO_STEREO_DELTA_PREVIOUS_INT8
    if channel_count == 1 and packing == AUDIO_PACKING_ABS_INT16:
        return MESSAGE_TYPE_AUDIO_MONO_ABS_INT16
    if channel_count == 2 and packing == AUDIO_PACKING_ABS_INT16:
        return MESSAGE_TYPE_AUDIO_STEREO_ABS_INT16
    raise ValueError(f"unsupported audio channel_count/packing: {channel_count}/{packing}")


def _audio_type_details(message_type: int) -> tuple[int, str]:
    if message_type == MESSAGE_TYPE_AUDIO_MONO_DELTA_PREVIOUS_INT8:
        return 1, AUDIO_PACKING_DELTA_PREVIOUS_INT8
    if message_type == MESSAGE_TYPE_AUDIO_STEREO_DELTA_PREVIOUS_INT8:
        return 2, AUDIO_PACKING_DELTA_PREVIOUS_INT8
    if message_type == MESSAGE_TYPE_AUDIO_MONO_ABS_INT16:
        return 1, AUDIO_PACKING_ABS_INT16
    if message_type == MESSAGE_TYPE_AUDIO_STEREO_ABS_INT16:
        return 2, AUDIO_PACKING_ABS_INT16
    raise ValueError(f"invalid audio message type: {message_type}")


def choose_audio_packing(relative_band_power_db: Any) -> dict[str, Any]:
    channels, band_count, channel_count = _audio_channel_values(relative_band_power_db)
    q_channels = [quantize_db_tenths(channel_values) for channel_values in channels]
    can_delta = all(can_pack_delta_previous_int8(q_values) for q_values in q_channels)
    packing = AUDIO_PACKING_DELTA_PREVIOUS_INT8 if can_delta else AUDIO_PACKING_ABS_INT16
    message_type = _audio_message_type(channel_count, packing)
    if packing == AUDIO_PACKING_DELTA_PREVIOUS_INT8:
        audio_payload = b"".join(pack_delta_previous_int8_channel(q_values) for q_values in q_channels)
    else:
        audio_payload = b"".join(pack_abs_int16_channel(q_values) for q_values in q_channels)
    return {
        "channels": channels,
        "q_channels": q_channels,
        "band_count": band_count,
        "channel_count": channel_count,
        "packing": packing,
        "message_type": message_type,
        "audio_payload": audio_payload,
    }


def crc16_ccitt_false(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def build_audio_proc_payload(
    *,
    timestamp: datetime | int | float | str | None,
    relative_band_power_db: Any,
    expected_band_count: int | None = None,
) -> bytes:
    packing_details = choose_audio_packing(relative_band_power_db)
    band_count = packing_details["band_count"]
    if expected_band_count is not None and band_count != int(expected_band_count):
        raise ValueError(
            f"AudioProc band count mismatch: expected {expected_band_count} bands, got {band_count}"
        )
    message_type = packing_details["message_type"]
    epoch = utc_epoch_seconds(timestamp)
    body = struct.pack(AUDIOPROC_HEADER_FORMAT, message_type, epoch) + packing_details["audio_payload"]
    return body + struct.pack(">H", crc16_ccitt_false(body))


def decode_audio_proc_payload(payload: bytes, expected_band_count: int | None = None) -> dict[str, Any]:
    minimum_size = AUDIOPROC_HEADER_SIZE + AUDIOPROC_CRC_SIZE
    if len(payload) < minimum_size:
        raise ValueError(f"audio payload must be at least {minimum_size} bytes")

    body = payload[:-AUDIOPROC_CRC_SIZE]
    expected_crc = struct.unpack(">H", payload[-AUDIOPROC_CRC_SIZE:])[0]
    actual_crc = crc16_ccitt_false(body)
    if actual_crc != expected_crc:
        raise ValueError(f"audio payload CRC mismatch: expected={expected_crc:#06x} actual={actual_crc:#06x}")

    message_type, epoch = struct.unpack(AUDIOPROC_HEADER_FORMAT, body[:AUDIOPROC_HEADER_SIZE])
    channel_count, packing = _audio_type_details(message_type)
    audio_payload = body[AUDIOPROC_HEADER_SIZE:]
    if packing == AUDIO_PACKING_DELTA_PREVIOUS_INT8:
        if expected_band_count is None:
            if len(audio_payload) % channel_count != 0:
                raise ValueError("delta audio payload length is not divisible by channel count")
            per_channel_size = len(audio_payload) // channel_count
            band_count = per_channel_size - 1
        else:
            band_count = int(expected_band_count)
        per_channel_size = 2 + max(0, band_count - 1)
        expected_size = channel_count * per_channel_size
        if len(audio_payload) != expected_size:
            raise ValueError(f"delta audio payload must be {expected_size} bytes for {band_count} bands")
        channels = []
        for channel_index in range(channel_count):
            start = channel_index * per_channel_size
            channels.append(
                unpack_delta_previous_int8_channel(audio_payload[start:start + per_channel_size], band_count)
            )
    else:
        if expected_band_count is None:
            if len(audio_payload) % (channel_count * 2) != 0:
                raise ValueError("ABS audio payload length is not divisible by channel count")
            band_count = len(audio_payload) // (channel_count * 2)
        else:
            band_count = int(expected_band_count)
        per_channel_size = band_count * 2
        expected_size = channel_count * per_channel_size
        if len(audio_payload) != expected_size:
            raise ValueError(f"ABS audio payload must be {expected_size} bytes for {band_count} bands")
        channels = []
        for channel_index in range(channel_count):
            start = channel_index * per_channel_size
            channels.append(unpack_abs_int16_channel(audio_payload[start:start + per_channel_size], band_count))

    rows = []
    for band_index in range(band_count):
        rows.append([channels[channel][band_index] for channel in range(channel_count)])

    return {
        "message_type": message_type,
        "timestamp": datetime.fromtimestamp(epoch, timezone.utc),
        "channel_count": channel_count,
        "band_count": band_count,
        "packing": packing,
        "relative_band_power_db": rows,
        "crc16_ccitt_false": expected_crc,
    }


def decode_message(payload: bytes, expected_audio_band_count: int | None = None) -> dict[str, Any]:
    if not payload:
        raise ValueError("Iridium payload is empty")
    message_type = payload[0]
    if message_type == MSG_SYSTEM_STATUS_V1:
        return unpack_system_status(payload)
    if message_type == MSG_BOOT_V1:
        return unpack_boot_message(payload)
    if message_type in {
        MESSAGE_TYPE_AUDIO_MONO_DELTA_PREVIOUS_INT8,
        MESSAGE_TYPE_AUDIO_STEREO_DELTA_PREVIOUS_INT8,
        MESSAGE_TYPE_AUDIO_MONO_ABS_INT16,
        MESSAGE_TYPE_AUDIO_STEREO_ABS_INT16,
    }:
        decoded = decode_audio_proc_payload(payload, expected_band_count=expected_audio_band_count)
        decoded["message_type_name"] = "MSG_AUDIO"
        return decoded
    raise ValueError(f"unknown Iridium message type: {message_type:#04x}")


def module_bit(module_name: str) -> int:
    return 1 << MODULE_ORDER.index(module_name)


def build_status_ok_bitmaps(system_status: dict[str, Any]) -> tuple[int, int]:
    modules = system_status.get("modules") or {}
    fsm_ok_bitmap = 0
    ll_ok_bitmap = 0

    for index, module_name in enumerate(MODULE_ORDER):
        module_status = modules.get(module_name)
        if not module_status:
            continue

        state = str(module_status.get("state") or "").upper()
        result = str(module_status.get("last_result") or "").lower()
        details = module_status.get("last_details") or {}

        if state != "ERROR":
            fsm_ok_bitmap |= 1 << index
        if result != "error" and not _details_have_errors(details):
            ll_ok_bitmap |= 1 << index

    return fsm_ok_bitmap, ll_ok_bitmap


def build_status_bitmaps(system_status: dict[str, Any]) -> tuple[int, int]:
    fsm_ok_bitmap, ll_ok_bitmap = build_status_ok_bitmaps(system_status)
    return (~fsm_ok_bitmap) & 0xFF, (~ll_ok_bitmap) & 0xFF


def _details_have_errors(value: Any) -> bool:
    if isinstance(value, dict):
        errors = value.get("errors")
        if errors:
            return True
        error = value.get("error")
        if error:
            return True
        return any(_details_have_errors(item) for item in value.values())
    if isinstance(value, list):
        return any(_details_have_errors(item) for item in value)
    return False
