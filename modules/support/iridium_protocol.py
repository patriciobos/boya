from __future__ import annotations

import struct
from datetime import datetime, timezone
from typing import Any

MESSAGE_TYPE_ALIVE = 0x01
ALIVE_PAYLOAD_FORMAT = ">BIBBBii"
ALIVE_PAYLOAD_SIZE = struct.calcsize(ALIVE_PAYLOAD_FORMAT)
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


def utc_epoch_seconds(timestamp: datetime | int | float | None = None) -> int:
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
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
    timestamp: datetime | int | float | None = None,
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
