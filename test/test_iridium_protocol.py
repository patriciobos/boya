from datetime import datetime, timezone

from modules.support.iridium_protocol import (
    ALIVE_PAYLOAD_SIZE,
    MESSAGE_TYPE_ALIVE,
    NO_COORD,
    build_alive_payload,
    build_status_bitmaps,
    decode_alive_payload,
    module_bit,
)


def test_build_alive_payload_encodes_fixed_binary_contract():
    timestamp = datetime(2026, 6, 11, 17, 20, 58, tzinfo=timezone.utc)

    payload = build_alive_payload(
        timestamp=timestamp,
        fsm_status_bits=module_bit("AIS"),
        ll_status_bits=module_bit("Iridium"),
        gps_fix=True,
        lat=-34.1234567,
        lon=-58.1234567,
    )

    assert len(payload) == ALIVE_PAYLOAD_SIZE == 16
    decoded = decode_alive_payload(payload)
    assert decoded["message_type"] == MESSAGE_TYPE_ALIVE
    assert decoded["timestamp"] == timestamp
    assert decoded["fsm_status_bits"] == module_bit("AIS")
    assert decoded["ll_status_bits"] == module_bit("Iridium")
    assert decoded["gps_fix"] is True
    assert decoded["lat"] == -34.1234567
    assert decoded["lon"] == -58.1234567


def test_build_alive_payload_uses_coord_sentinel_without_fix():
    payload = build_alive_payload(timestamp=0, gps_fix=False, lat=-34.0, lon=-58.0)

    assert payload[-8:-4] == NO_COORD.to_bytes(4, "big", signed=True)
    assert payload[-4:] == NO_COORD.to_bytes(4, "big", signed=True)
    decoded = decode_alive_payload(payload)
    assert decoded["gps_fix"] is False
    assert decoded["lat"] is None
    assert decoded["lon"] is None


def test_build_status_bitmaps_maps_module_errors_to_bits():
    status = {
        "modules": {
            "AHT10": {"state": "IDLE", "last_result": "ok"},
            "AIS": {"state": "ERROR", "last_result": "ok"},
            "AudioProc": {"state": "IDLE", "last_result": "error"},
            "Behringer": {"state": "IDLE", "last_result": "ok"},
            "Iridium": {"state": "IDLE", "last_result": "ok"},
            "MPU6050": {"state": "IDLE", "last_result": "ok"},
            "Windsonic": {"state": "IDLE", "last_result": "ok"},
            "XTRA2210": {"state": "IDLE", "last_result": "ok"},
        }
    }

    fsm_bits, ll_bits = build_status_bitmaps(status)

    assert fsm_bits == module_bit("AIS")
    assert ll_bits == module_bit("AudioProc")
