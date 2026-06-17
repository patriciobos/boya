from __future__ import annotations

import csv
import math
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modules.support.system_config import PROJECT_ROOT, get_config_value

MESSAGE_TYPE_ALIVE = 0x01
MESSAGE_TYPE_AUDIO_MONO = 0x03
MESSAGE_TYPE_AUDIO_STEREO = 0x04
ALIVE_PAYLOAD_FORMAT = ">BIBBBii"
ALIVE_PAYLOAD_SIZE = struct.calcsize(ALIVE_PAYLOAD_FORMAT)
AUDIOPROC_HEADER_FORMAT = ">BI"
AUDIOPROC_HEADER_SIZE = struct.calcsize(AUDIOPROC_HEADER_FORMAT)
AUDIOPROC_CRC_SIZE = 2
AUDIOPROC_NULL_DB = -128
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


def build_alive_payload(
    *,
    timestamp: datetime | int | float | str | None = None,
    fsm_status_bits: int = 0,
    ll_status_bits: int = 0,
    gps_fix: bool | None = False,
    lat: float | None = None,
    lon: float | None = None,
) -> bytes:
    epoch = utc_epoch_seconds(timestamp)
    fsm_status_bits = int(fsm_status_bits) & 0xFF
    ll_status_bits = int(ll_status_bits) & 0xFF
    has_fix = bool(gps_fix and lat is not None and lon is not None)
    lat_i = encode_coordinate(lat, -90.0, 90.0) if has_fix else NO_COORD
    lon_i = encode_coordinate(lon, -180.0, 180.0) if has_fix else NO_COORD
    return struct.pack(
        ALIVE_PAYLOAD_FORMAT,
        MESSAGE_TYPE_ALIVE,
        epoch,
        fsm_status_bits,
        ll_status_bits,
        1 if has_fix else 0,
        lat_i,
        lon_i,
    )


def decode_alive_payload(payload: bytes) -> dict[str, Any]:
    if len(payload) != ALIVE_PAYLOAD_SIZE:
        raise ValueError(f"alive payload must be {ALIVE_PAYLOAD_SIZE} bytes")
    message_type, epoch, fsm_bits, ll_bits, gps_fix, lat_i, lon_i = struct.unpack(
        ALIVE_PAYLOAD_FORMAT, payload
    )
    if message_type != MESSAGE_TYPE_ALIVE:
        raise ValueError(f"invalid alive message type: {message_type}")
    return {
        "message_type": message_type,
        "timestamp": datetime.fromtimestamp(epoch, timezone.utc),
        "fsm_status_bits": fsm_bits,
        "ll_status_bits": ll_bits,
        "gps_fix": bool(gps_fix),
        "lat": None if lat_i == NO_COORD else lat_i / COORD_SCALE,
        "lon": None if lon_i == NO_COORD else lon_i / COORD_SCALE,
    }


def status_bits_binary(value: int) -> str:
    return format(int(value) & 0xFF, "08b")


def status_details(fsm_status_bits: int, ll_status_bits: int) -> dict[str, Any]:
    fsm_status_bits = int(fsm_status_bits) & 0xFF
    ll_status_bits = int(ll_status_bits) & 0xFF
    return {
        "fsm_status_bits": fsm_status_bits,
        "ll_status_bits": ll_status_bits,
        "fsm_status_bits_binary": status_bits_binary(fsm_status_bits),
        "ll_status_bits_binary": status_bits_binary(ll_status_bits),
        "status_bytes_binary": f"{status_bits_binary(fsm_status_bits)} {status_bits_binary(ll_status_bits)}",
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


def encode_audio_db_value(value: Any) -> int:
    if value is None:
        return AUDIOPROC_NULL_DB
    numeric = float(value)
    if not math.isfinite(numeric):
        return AUDIOPROC_NULL_DB
    rounded = int(round(numeric))
    return max(-127, min(127, rounded))


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
    channels, band_count, channel_count = _audio_channel_values(relative_band_power_db)
    if expected_band_count is not None and band_count != int(expected_band_count):
        raise ValueError(
            f"AudioProc band count mismatch: expected {expected_band_count} bands, got {band_count}"
        )
    message_type = MESSAGE_TYPE_AUDIO_MONO if channel_count == 1 else MESSAGE_TYPE_AUDIO_STEREO
    epoch = utc_epoch_seconds(timestamp)
    encoded_values = bytes(
        encode_audio_db_value(value) & 0xFF
        for channel_values in channels
        for value in channel_values
    )
    body = struct.pack(AUDIOPROC_HEADER_FORMAT, message_type, epoch) + encoded_values
    return body + struct.pack(">H", crc16_ccitt_false(body))


def decode_audio_proc_payload(payload: bytes) -> dict[str, Any]:
    minimum_size = AUDIOPROC_HEADER_SIZE + AUDIOPROC_CRC_SIZE
    if len(payload) < minimum_size:
        raise ValueError(f"audio payload must be at least {minimum_size} bytes")

    body = payload[:-AUDIOPROC_CRC_SIZE]
    expected_crc = struct.unpack(">H", payload[-AUDIOPROC_CRC_SIZE:])[0]
    actual_crc = crc16_ccitt_false(body)
    if actual_crc != expected_crc:
        raise ValueError(f"audio payload CRC mismatch: expected={expected_crc:#06x} actual={actual_crc:#06x}")

    message_type, epoch = struct.unpack(AUDIOPROC_HEADER_FORMAT, body[:AUDIOPROC_HEADER_SIZE])
    if message_type == MESSAGE_TYPE_AUDIO_MONO:
        channel_count = 1
    elif message_type == MESSAGE_TYPE_AUDIO_STEREO:
        channel_count = 2
    else:
        raise ValueError(f"invalid audio message type: {message_type}")

    raw_values = body[AUDIOPROC_HEADER_SIZE:]
    if len(raw_values) % channel_count != 0:
        raise ValueError("audio payload value count is not divisible by channel count")
    band_count = len(raw_values) // channel_count
    values = [
        None if value == 0x80 else struct.unpack(">b", bytes([value]))[0]
        for value in raw_values
    ]
    rows = []
    for band_index in range(band_count):
        rows.append([values[channel * band_count + band_index] for channel in range(channel_count)])

    return {
        "message_type": message_type,
        "timestamp": datetime.fromtimestamp(epoch, timezone.utc),
        "channel_count": channel_count,
        "band_count": band_count,
        "relative_band_power_db": rows,
        "crc16_ccitt_false": expected_crc,
    }


def module_bit(module_name: str) -> int:
    return 1 << MODULE_ORDER.index(module_name)


def build_status_bitmaps(system_status: dict[str, Any]) -> tuple[int, int]:
    modules = system_status.get("modules") or {}
    fsm_bits = 0
    ll_bits = 0

    for index, module_name in enumerate(MODULE_ORDER):
        module_status = modules.get(module_name)
        if not module_status:
            fsm_bits |= 1 << index
            ll_bits |= 1 << index
            continue

        state = str(module_status.get("state") or "").upper()
        result = str(module_status.get("last_result") or "").lower()
        details = module_status.get("last_details") or {}

        if state == "ERROR":
            fsm_bits |= 1 << index
        if result == "error" or _details_have_errors(details):
            ll_bits |= 1 << index

    return fsm_bits, ll_bits


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
