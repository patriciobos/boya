from datetime import datetime, timezone

from modules.support.iridium_protocol import (
    ALIVE_PAYLOAD_SIZE,
    AUDIOPROC_HEADER_SIZE,
    AUDIOPROC_NULL_DB,
    MESSAGE_TYPE_ALIVE,
    MESSAGE_TYPE_AUDIOPROC,
    NO_COORD,
    build_alive_payload,
    build_audio_proc_payload,
    build_status_bitmaps,
    decode_alive_payload,
    decode_audio_proc_payload,
    expected_audio_band_count,
    module_bit,
    status_details,
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


def test_status_details_includes_binary_status_bytes():
    details = status_details(0b10100000, 0b00000101)

    assert details["fsm_status_bits"] == 0b10100000
    assert details["ll_status_bits"] == 0b00000101
    assert details["fsm_status_bits_binary"] == "10100000"
    assert details["ll_status_bits_binary"] == "00000101"
    assert details["status_bytes_binary"] == "10100000 00000101"


def test_build_audio_proc_payload_encodes_status_and_audio_values():
    payload = build_audio_proc_payload(
        fsm_status_bits=module_bit("AIS"),
        ll_status_bits=module_bit("AudioProc"),
        relative_band_power_db=[[None], [62.7], [-200], [200]],
        expected_band_count=4,
    )

    assert len(payload) == AUDIOPROC_HEADER_SIZE + 4
    decoded = decode_audio_proc_payload(payload)
    assert decoded["message_type"] == MESSAGE_TYPE_AUDIOPROC
    assert decoded["fsm_status_bits"] == module_bit("AIS")
    assert decoded["ll_status_bits"] == module_bit("AudioProc")
    assert decoded["band_count"] == 4
    assert decoded["relative_band_power_db"] == [None, 63, -127, 127]
    assert payload[AUDIOPROC_HEADER_SIZE] == (AUDIOPROC_NULL_DB & 0xFF)


def test_audio_proc_payload_validates_frequency_bands_per_channel():
    expected_bands = expected_audio_band_count(sample_rate_hz=192000)
    assert expected_bands == 49

    mono_bands = [[float(index)] for index in range(expected_bands)]
    stereo_bands = [[float(index), float(index + 1)] for index in range(expected_bands)]

    mono_payload = build_audio_proc_payload(relative_band_power_db=mono_bands, expected_band_count=expected_bands)
    stereo_payload = build_audio_proc_payload(relative_band_power_db=stereo_bands, expected_band_count=expected_bands)

    assert len(mono_payload) == AUDIOPROC_HEADER_SIZE + expected_bands
    assert len(stereo_payload) == AUDIOPROC_HEADER_SIZE + 2 * expected_bands
    assert decode_audio_proc_payload(mono_payload)["band_count"] == expected_bands
    assert decode_audio_proc_payload(stereo_payload)["band_count"] == 2 * expected_bands


def test_audio_proc_payload_rejects_wrong_band_count():
    expected_bands = 4

    try:
        build_audio_proc_payload(relative_band_power_db=[[1.0], [2.0], [3.0]], expected_band_count=expected_bands)
    except ValueError as exc:
        assert "band count mismatch" in str(exc)
    else:
        raise AssertionError("expected ValueError for wrong AudioProc band count")
