from datetime import datetime, timezone

import math
import struct

from modules.support.iridium_protocol import (
    ALIVE_PAYLOAD_SIZE,
    AUDIOPROC_ABS_INT16_SENTINEL,
    AUDIOPROC_CRC_SIZE,
    AUDIOPROC_HEADER_SIZE,
    AUDIO_PACKING_ABS_INT16,
    AUDIO_PACKING_DELTA_PREVIOUS_INT8,
    MESSAGE_TYPE_ALIVE,
    MESSAGE_TYPE_AUDIO_MONO_ABS_INT16,
    MESSAGE_TYPE_AUDIO_MONO_DELTA_PREVIOUS_INT8,
    MESSAGE_TYPE_AUDIO_STEREO_ABS_INT16,
    MESSAGE_TYPE_AUDIO_STEREO_DELTA_PREVIOUS_INT8,
    NO_COORD,
    build_alive_payload,
    build_audio_proc_payload,
    build_status_bitmaps,
    can_pack_delta_previous_int8,
    decode_alive_payload,
    decode_audio_proc_payload,
    encode_audio_db_value,
    expected_audio_band_count,
    module_bit,
    pack_abs_int16_channel,
    pack_delta_previous_int8_channel,
    quantize_db_tenths,
    status_details,
    unpack_abs_int16_channel,
    unpack_delta_previous_int8_channel,
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


def assert_rows_close(actual, expected):
    assert len(actual) == len(expected)
    for actual_row, expected_row in zip(actual, expected):
        assert len(actual_row) == len(expected_row)
        for actual_value, expected_value in zip(actual_row, expected_row):
            if expected_value is None:
                assert actual_value is None
            else:
                assert actual_value is not None
                assert abs(actual_value - expected_value) <= 0.05


def test_quantize_db_tenths_uses_numpy_rint_and_marks_invalid():
    values = [1.23, 1.25, -3.7, None, float("nan"), float("inf")]

    assert quantize_db_tenths(values) == [12, 12, -37, None, None, None]
    assert encode_audio_db_value(4000.0) == 32767
    assert encode_audio_db_value(-4000.0) == -32767
    assert encode_audio_db_value(None) == AUDIOPROC_ABS_INT16_SENTINEL


def test_delta_previous_int8_channel_round_trip():
    q_values = quantize_db_tenths([1.2, 1.5, 1.8, 1.7])

    assert can_pack_delta_previous_int8(q_values) is True
    packed = pack_delta_previous_int8_channel(q_values)

    assert len(packed) == 2 + (len(q_values) - 1)
    assert packed[:2] == struct.pack(">h", 12)
    assert unpack_delta_previous_int8_channel(packed, len(q_values)) == [1.2, 1.5, 1.8, 1.7]


def test_abs_int16_channel_round_trip_with_sentinel_and_big_endian():
    q_values = quantize_db_tenths([None, -3.7, 1.2, math.inf, 4000.0])
    packed = pack_abs_int16_channel(q_values)

    assert len(packed) == 2 * len(q_values)
    assert packed[:2] == struct.pack(">h", AUDIOPROC_ABS_INT16_SENTINEL)
    assert packed[2:4] == struct.pack(">h", -37)
    assert packed[-2:] == struct.pack(">h", 32767)
    assert unpack_abs_int16_channel(packed, len(q_values)) == [None, -3.7, 1.2, None, 3276.7]


def test_build_audio_proc_payload_prefers_mono_delta_with_timestamp_and_crc():
    timestamp = datetime(2026, 6, 13, 11, 0, 8, tzinfo=timezone.utc)
    bands = [[1.2], [1.5], [1.8], [1.7]]

    payload = build_audio_proc_payload(
        timestamp=timestamp,
        relative_band_power_db=bands,
        expected_band_count=4,
    )

    assert len(payload) == AUDIOPROC_HEADER_SIZE + 2 + 3 + AUDIOPROC_CRC_SIZE
    assert payload[0] == MESSAGE_TYPE_AUDIO_MONO_DELTA_PREVIOUS_INT8
    decoded = decode_audio_proc_payload(payload, expected_band_count=4)
    assert decoded["message_type"] == MESSAGE_TYPE_AUDIO_MONO_DELTA_PREVIOUS_INT8
    assert decoded["packing"] == AUDIO_PACKING_DELTA_PREVIOUS_INT8
    assert decoded["timestamp"] == timestamp
    assert decoded["channel_count"] == 1
    assert decoded["band_count"] == 4
    assert_rows_close(decoded["relative_band_power_db"], bands)


def test_audio_proc_payload_validates_frequency_bands_per_channel():
    expected_bands = expected_audio_band_count(sample_rate_hz=192000)
    assert expected_bands == 49

    mono_bands = [[float(index)] for index in range(expected_bands)]
    stereo_bands = [[float(index), float(index + 100)] for index in range(expected_bands)]

    mono_payload = build_audio_proc_payload(
        timestamp=0, relative_band_power_db=mono_bands, expected_band_count=expected_bands
    )
    stereo_payload = build_audio_proc_payload(
        timestamp=0, relative_band_power_db=stereo_bands, expected_band_count=expected_bands
    )

    assert len(mono_payload) == AUDIOPROC_HEADER_SIZE + 2 + (expected_bands - 1) + AUDIOPROC_CRC_SIZE
    assert len(stereo_payload) == AUDIOPROC_HEADER_SIZE + 2 * (2 + (expected_bands - 1)) + AUDIOPROC_CRC_SIZE
    assert mono_payload[0] == MESSAGE_TYPE_AUDIO_MONO_DELTA_PREVIOUS_INT8
    assert stereo_payload[0] == MESSAGE_TYPE_AUDIO_STEREO_DELTA_PREVIOUS_INT8
    assert decode_audio_proc_payload(mono_payload, expected_band_count=expected_bands)["band_count"] == expected_bands
    decoded_stereo = decode_audio_proc_payload(stereo_payload, expected_band_count=expected_bands)
    assert decoded_stereo["band_count"] == expected_bands
    assert decoded_stereo["channel_count"] == 2
    assert decoded_stereo["packing"] == AUDIO_PACKING_DELTA_PREVIOUS_INT8
    assert decoded_stereo["relative_band_power_db"][0] == [0.0, 100.0]
    first_channel = stereo_payload[AUDIOPROC_HEADER_SIZE:AUDIOPROC_HEADER_SIZE + 2 + (expected_bands - 1)]
    assert first_channel[:2] == struct.pack(">h", 0)
    assert first_channel[2:] == bytes([10] * (expected_bands - 1))


def test_audio_proc_payload_falls_back_globally_to_abs_when_any_delta_is_too_large():
    bands = [[0.0, 10.0], [1.0, 40.0], [2.0, 41.0]]

    payload = build_audio_proc_payload(timestamp=0, relative_band_power_db=bands, expected_band_count=3)

    assert payload[0] == MESSAGE_TYPE_AUDIO_STEREO_ABS_INT16
    assert len(payload) == AUDIOPROC_HEADER_SIZE + 4 * 3 + AUDIOPROC_CRC_SIZE
    decoded = decode_audio_proc_payload(payload, expected_band_count=3)
    assert decoded["packing"] == AUDIO_PACKING_ABS_INT16
    assert_rows_close(decoded["relative_band_power_db"], bands)


def test_audio_proc_payload_abs_mono_encodes_invalid_sentinel_and_saturates():
    bands = [[None], [float("nan")], [float("inf")], [4000.0], [-4000.0]]

    payload = build_audio_proc_payload(timestamp=0, relative_band_power_db=bands, expected_band_count=5)

    assert payload[0] == MESSAGE_TYPE_AUDIO_MONO_ABS_INT16
    assert len(payload) == AUDIOPROC_HEADER_SIZE + 2 * 5 + AUDIOPROC_CRC_SIZE
    body = payload[AUDIOPROC_HEADER_SIZE:-AUDIOPROC_CRC_SIZE]
    assert body[:2] == struct.pack(">h", AUDIOPROC_ABS_INT16_SENTINEL)
    decoded = decode_audio_proc_payload(payload, expected_band_count=5)
    assert decoded["relative_band_power_db"] == [[None], [None], [None], [3276.7], [-3276.7]]


def test_audio_proc_payload_rejects_wrong_band_count():
    expected_bands = 4

    try:
        build_audio_proc_payload(timestamp=0, relative_band_power_db=[[1.0], [2.0], [3.0]], expected_band_count=expected_bands)
    except ValueError as exc:
        assert "band count mismatch" in str(exc)
    else:
        raise AssertionError("expected ValueError for wrong AudioProc band count")


def test_audio_proc_payload_rejects_bad_crc():
    payload = bytearray(build_audio_proc_payload(timestamp=0, relative_band_power_db=[[1.0]], expected_band_count=1))
    payload[-1] ^= 0x01

    try:
        decode_audio_proc_payload(bytes(payload))
    except ValueError as exc:
        assert "CRC mismatch" in str(exc)
    else:
        raise AssertionError("expected ValueError for bad CRC")
