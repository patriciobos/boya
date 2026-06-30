from datetime import datetime, timezone
from pathlib import Path

import json
import math
import subprocess
import struct
import sys

from modules.support.iridium_protocol import (
    AUDIOPROC_ABS_INT16_SENTINEL,
    AUDIOPROC_CRC_SIZE,
    AUDIOPROC_HEADER_SIZE,
    AUDIO_PACKING_ABS_INT16,
    AUDIO_PACKING_DELTA_PREVIOUS_INT8,
    BOOT_PAYLOAD_SIZE,
    MESSAGE_TYPE_AUDIO_MONO_ABS_INT16,
    MESSAGE_TYPE_AUDIO_MONO_DELTA_PREVIOUS_INT8,
    MESSAGE_TYPE_AUDIO_STEREO_ABS_INT16,
    MESSAGE_TYPE_AUDIO_STEREO_DELTA_PREVIOUS_INT8,
    MSG_BOOT,
    MSG_SYSTEM_STATUS,
    SYSTEM_STATUS_FLAG_BATTERY_CRITICAL,
    SYSTEM_STATUS_FLAG_BATTERY_WARNING,
    SYSTEM_STATUS_FLAG_LAST_ACQUISITION_INCOMPLETE,
    SYSTEM_STATUS_FLAG_STORAGE_CRITICAL,
    SYSTEM_STATUS_FLAG_STORAGE_NOT_WRITABLE,
    SYSTEM_STATUS_FLAG_STORAGE_QUOTA_EXCEEDED,
    SYSTEM_STATUS_FLAG_STORAGE_UNAVAILABLE,
    SYSTEM_STATUS_FLAG_STORAGE_WARNING,
    SYSTEM_STATUS_PAYLOAD_SIZE,
    build_status_flags,
    build_audio_proc_payload,
    build_status_bitmaps,
    build_status_ok_bitmaps,
    can_pack_delta_previous_int8,
    decode_message,
    decode_audio_proc_payload,
    encode_battery_soc_percent,
    encode_battery_voltage_mv,
    encode_audio_db_value,
    encode_storage_free_gib_x10,
    encode_uptime_minutes,
    expected_audio_band_count,
    module_bit,
    pack_abs_int16_channel,
    pack_boot_payload,
    pack_delta_previous_int8_channel,
    pack_system_status,
    quantize_db_tenths,
    status_details,
    unpack_system_status,
    unpack_abs_int16_channel,
    unpack_delta_previous_int8_channel,
)


def test_pack_boot_payload_encodes_fixed_3_byte_binary_contract():
    payload = pack_boot_payload(0)

    assert len(payload) == BOOT_PAYLOAD_SIZE == 3
    assert payload == bytes([MSG_BOOT, 0x00, 0x00])
    decoded = decode_message(payload)
    assert decoded == {
        "message_type": "MSG_BOOT",
        "message_type_byte": MSG_BOOT,
        "uptime_minutes": 0,
    }


def test_pack_boot_payload_encodes_big_endian_uptime():
    payload = pack_boot_payload(65535)

    assert payload[0] == MSG_BOOT
    assert payload[1:] == struct.pack(">H", 65535)
    decoded = decode_message(payload)
    assert decoded["uptime_minutes"] == 65535


def test_pack_boot_payload_validates_uptime_range():
    try:
        pack_boot_payload(65536)
    except ValueError as exc:
        assert "uptime_minutes must be in range" in str(exc)
    else:
        raise AssertionError("expected ValueError for out of range uptime_minutes")

    try:
        pack_boot_payload(-1)
    except ValueError as exc:
        assert "uptime_minutes must be in range" in str(exc)
    else:
        raise AssertionError("expected ValueError for negative uptime_minutes")


def test_pack_system_status_encodes_fixed_11_byte_binary_contract():
    payload = pack_system_status(
        fsm_ok_bitmap=0b11111101,
        ll_ok_bitmap=0b11101111,
        storage_warning=True,
        storage_quota_exceeded=True,
        battery_warning=True,
        battery_voltage_mv=12480,
        battery_soc_percent=87,
        storage_free_gib=842.7,
        uptime_minutes=5321,
    )

    assert len(payload) == SYSTEM_STATUS_PAYLOAD_SIZE == 11
    assert payload == bytes(
        [
            MSG_SYSTEM_STATUS,
            0b11111101,
            0b11101111,
            SYSTEM_STATUS_FLAG_STORAGE_WARNING
            | SYSTEM_STATUS_FLAG_STORAGE_QUOTA_EXCEEDED
            | SYSTEM_STATUS_FLAG_BATTERY_WARNING,
        ]
    ) + struct.pack(">H", 12480) + bytes([87]) + struct.pack(">HH", 8427, 5321)
    decoded = unpack_system_status(payload)
    assert decoded == {
        "message_type": "MSG_SYSTEM_STATUS",
        "message_type_byte": MSG_SYSTEM_STATUS,
        "fsm_ok_bitmap": 0b11111101,
        "ll_ok_bitmap": 0b11101111,
        "status_flags_raw": 52,
        "status_flags": {
            "storage_unavailable": False,
            "storage_not_writable": False,
            "storage_warning": True,
            "storage_critical": False,
            "storage_quota_exceeded": True,
            "battery_warning": True,
            "battery_critical": False,
            "last_acquisition_incomplete": False,
        },
        "battery": {"voltage_mv": 12480, "voltage_v": 12.48, "soc_percent": 87},
        "storage": {"free_gib_x10": 8427, "free_gib": 842.7},
        "uptime_minutes": 5321,
    }


def test_system_status_flags_bits_and_critical_implications():
    flags = build_status_flags(
        storage_unavailable=True,
        storage_not_writable=True,
        storage_critical=True,
        storage_quota_exceeded=True,
        battery_critical=True,
        last_acquisition_incomplete=True,
    )

    assert flags == (
        SYSTEM_STATUS_FLAG_STORAGE_UNAVAILABLE
        | SYSTEM_STATUS_FLAG_STORAGE_NOT_WRITABLE
        | SYSTEM_STATUS_FLAG_STORAGE_WARNING
        | SYSTEM_STATUS_FLAG_STORAGE_CRITICAL
        | SYSTEM_STATUS_FLAG_STORAGE_QUOTA_EXCEEDED
        | SYSTEM_STATUS_FLAG_BATTERY_WARNING
        | SYSTEM_STATUS_FLAG_BATTERY_CRITICAL
        | SYSTEM_STATUS_FLAG_LAST_ACQUISITION_INCOMPLETE
    )
    decoded = unpack_system_status(
        pack_system_status(fsm_ok_bitmap=0, ll_ok_bitmap=0, status_flags=flags)
    )
    assert all(decoded["status_flags"].values())
    assert "any_fsm_not_ok" not in decoded
    assert "any_ll_not_ok" not in decoded


def test_system_status_sentinels_and_saturation():
    payload = pack_system_status(
        fsm_ok_bitmap=0xFF,
        ll_ok_bitmap=0x00,
        storage_unavailable=True,
        battery_voltage_mv=None,
        battery_soc_percent=None,
        storage_free_gib=123.4,
        uptime_minutes=70000,
    )

    assert payload[4:6] == b"\xff\xff"
    assert payload[6] == 0xFF
    assert payload[7:9] == b"\xff\xff"
    assert payload[9:11] == b"\xff\xff"
    decoded = unpack_system_status(payload)
    assert decoded["battery"] == {
        "voltage_mv": None,
        "voltage_v": None,
        "soc_percent": None,
    }
    assert decoded["storage"] == {"free_gib_x10": None, "free_gib": None}
    assert decoded["uptime_minutes"] is None
    assert encode_battery_voltage_mv(-1) == 0
    assert encode_battery_voltage_mv(70000) == 65534
    assert encode_battery_soc_percent(-10) == 0
    assert encode_battery_soc_percent(120) == 100
    assert encode_storage_free_gib_x10(7000) == 65534
    assert encode_uptime_minutes(-3) == 0
    assert encode_uptime_minutes(65535) == 65535


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
    fsm_ok_bitmap, ll_ok_bitmap = build_status_ok_bitmaps(status)
    assert fsm_ok_bitmap == (0xFF & ~module_bit("AIS"))
    assert ll_ok_bitmap == (0xFF & ~module_bit("AudioProc"))


def test_status_details_includes_binary_status_bytes():
    details = status_details(0b10100000, 0b00000101)

    assert details["fsm_ok_bitmap"] == 0b10100000
    assert details["ll_ok_bitmap"] == 0b00000101
    assert details["fsm_ok_bitmap_binary"] == "10100000"
    assert details["ll_ok_bitmap_binary"] == "00000101"
    assert details["ok_bytes_binary"] == "10100000 00000101"


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
    assert unpack_delta_previous_int8_channel(packed, len(q_values)) == [
        1.2,
        1.5,
        1.8,
        1.7,
    ]


def test_abs_int16_channel_round_trip_with_sentinel_and_big_endian():
    q_values = quantize_db_tenths([None, -3.7, 1.2, math.inf, 4000.0])
    packed = pack_abs_int16_channel(q_values)

    assert len(packed) == 2 * len(q_values)
    assert packed[:2] == struct.pack(">h", AUDIOPROC_ABS_INT16_SENTINEL)
    assert packed[2:4] == struct.pack(">h", -37)
    assert packed[-2:] == struct.pack(">h", 32767)
    assert unpack_abs_int16_channel(packed, len(q_values)) == [
        None,
        -3.7,
        1.2,
        None,
        3276.7,
    ]


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


def test_decode_message_routes_system_status_and_audio():
    status_payload = pack_system_status(
        fsm_ok_bitmap=0xFE,
        ll_ok_bitmap=0xFD,
        battery_voltage_mv=12500,
        battery_soc_percent=88,
        storage_free_gib=100.0,
        uptime_minutes=10,
    )
    status_decoded = decode_message(status_payload)

    assert status_decoded["message_type"] == "MSG_SYSTEM_STATUS"
    assert status_decoded["fsm_ok_bitmap"] == 0xFE
    assert status_decoded["ll_ok_bitmap"] == 0xFD

    audio_payload = build_audio_proc_payload(
        timestamp=0,
        relative_band_power_db=[[1.0], [1.2]],
        expected_band_count=2,
    )
    audio_decoded = decode_message(audio_payload, expected_audio_band_count=2)

    assert audio_decoded["message_type_name"] == "MSG_AUDIO"
    assert audio_decoded["packing"] == AUDIO_PACKING_DELTA_PREVIOUS_INT8
    assert audio_decoded["relative_band_power_db"] == [[1.0], [1.2]]


def test_decode_message_rejects_unknown_message_type():
    try:
        decode_message(b"\x7f")
    except ValueError as exc:
        assert "unknown Iridium message type" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown message type")


def test_decode_iridium_message_script_decodes_hex_payload():
    payload = pack_system_status(
        fsm_ok_bitmap=0xAA,
        ll_ok_bitmap=0x55,
        battery_voltage_mv=12340,
        battery_soc_percent=76,
        storage_free_gib=12.3,
        uptime_minutes=45,
    )

    result = subprocess.run(
        [
            sys.executable,
            "scripts/decode_iridium_message.py",
            "--hex",
            payload.hex(),
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    decoded = json.loads(result.stdout)
    assert decoded["message_type"] == "MSG_SYSTEM_STATUS"
    assert decoded["fsm_ok_bitmap"] == 0xAA
    assert decoded["ll_ok_bitmap"] == 0x55
    assert decoded["battery"]["voltage_mv"] == 12340
    assert decoded["storage"]["free_gib_x10"] == 123


def test_audio_proc_payload_validates_frequency_bands_per_channel():
    expected_bands = expected_audio_band_count(sample_rate_hz=192000)
    assert expected_bands == 49

    mono_bands = [[float(index)] for index in range(expected_bands)]
    stereo_bands = [
        [float(index), float(index + 100)] for index in range(expected_bands)
    ]

    mono_payload = build_audio_proc_payload(
        timestamp=0,
        relative_band_power_db=mono_bands,
        expected_band_count=expected_bands,
    )
    stereo_payload = build_audio_proc_payload(
        timestamp=0,
        relative_band_power_db=stereo_bands,
        expected_band_count=expected_bands,
    )

    assert (
        len(mono_payload)
        == AUDIOPROC_HEADER_SIZE + 2 + (expected_bands - 1) + AUDIOPROC_CRC_SIZE
    )
    assert (
        len(stereo_payload)
        == AUDIOPROC_HEADER_SIZE + 2 * (2 + (expected_bands - 1)) + AUDIOPROC_CRC_SIZE
    )
    assert mono_payload[0] == MESSAGE_TYPE_AUDIO_MONO_DELTA_PREVIOUS_INT8
    assert stereo_payload[0] == MESSAGE_TYPE_AUDIO_STEREO_DELTA_PREVIOUS_INT8
    assert (
        decode_audio_proc_payload(mono_payload, expected_band_count=expected_bands)[
            "band_count"
        ]
        == expected_bands
    )
    decoded_stereo = decode_audio_proc_payload(
        stereo_payload, expected_band_count=expected_bands
    )
    assert decoded_stereo["band_count"] == expected_bands
    assert decoded_stereo["channel_count"] == 2
    assert decoded_stereo["packing"] == AUDIO_PACKING_DELTA_PREVIOUS_INT8
    assert decoded_stereo["relative_band_power_db"][0] == [0.0, 100.0]
    first_channel = stereo_payload[
        AUDIOPROC_HEADER_SIZE : AUDIOPROC_HEADER_SIZE + 2 + (expected_bands - 1)
    ]
    assert first_channel[:2] == struct.pack(">h", 0)
    assert first_channel[2:] == bytes([10] * (expected_bands - 1))


def test_audio_proc_payload_falls_back_globally_to_abs_when_any_delta_is_too_large():
    bands = [[0.0, 10.0], [1.0, 40.0], [2.0, 41.0]]

    payload = build_audio_proc_payload(
        timestamp=0, relative_band_power_db=bands, expected_band_count=3
    )

    assert payload[0] == MESSAGE_TYPE_AUDIO_STEREO_ABS_INT16
    assert len(payload) == AUDIOPROC_HEADER_SIZE + 4 * 3 + AUDIOPROC_CRC_SIZE
    decoded = decode_audio_proc_payload(payload, expected_band_count=3)
    assert decoded["packing"] == AUDIO_PACKING_ABS_INT16
    assert_rows_close(decoded["relative_band_power_db"], bands)


def test_audio_proc_payload_abs_mono_encodes_invalid_sentinel_and_saturates():
    bands = [[None], [float("nan")], [float("inf")], [4000.0], [-4000.0]]

    payload = build_audio_proc_payload(
        timestamp=0, relative_band_power_db=bands, expected_band_count=5
    )

    assert payload[0] == MESSAGE_TYPE_AUDIO_MONO_ABS_INT16
    assert len(payload) == AUDIOPROC_HEADER_SIZE + 2 * 5 + AUDIOPROC_CRC_SIZE
    body = payload[AUDIOPROC_HEADER_SIZE:-AUDIOPROC_CRC_SIZE]
    assert body[:2] == struct.pack(">h", AUDIOPROC_ABS_INT16_SENTINEL)
    decoded = decode_audio_proc_payload(payload, expected_band_count=5)
    assert decoded["relative_band_power_db"] == [
        [None],
        [None],
        [None],
        [3276.7],
        [-3276.7],
    ]


def test_audio_proc_payload_rejects_wrong_band_count():
    expected_bands = 4

    try:
        build_audio_proc_payload(
            timestamp=0,
            relative_band_power_db=[[1.0], [2.0], [3.0]],
            expected_band_count=expected_bands,
        )
    except ValueError as exc:
        assert "band count mismatch" in str(exc)
    else:
        raise AssertionError("expected ValueError for wrong AudioProc band count")


def test_audio_proc_payload_rejects_bad_crc():
    payload = bytearray(
        build_audio_proc_payload(
            timestamp=0, relative_band_power_db=[[1.0]], expected_band_count=1
        )
    )
    payload[-1] ^= 0x01

    try:
        decode_audio_proc_payload(bytes(payload))
    except ValueError as exc:
        assert "CRC mismatch" in str(exc)
    else:
        raise AssertionError("expected ValueError for bad CRC")


def test_decode_iridium_message_batch_generates_executive_reports(tmp_path):
    payload_dir = tmp_path / "payloads"
    output_dir = tmp_path / "reports"
    payload_dir.mkdir()

    status_payload = pack_system_status(
        fsm_ok_bitmap=0xAA,
        ll_ok_bitmap=0x55,
        battery_voltage_mv=12340,
        battery_soc_percent=76,
        storage_free_gib=12.3,
        uptime_minutes=45,
    )
    audio_payload = build_audio_proc_payload(
        timestamp=0,
        relative_band_power_db=[[1.0], [1.2]],
        expected_band_count=2,
    )

    status_name = "1_19f0d7530d64a567_300534063350070_000001.sbd"
    audio_name = "2_19f0dac206b58df1_300534063350070_000002.sbd"
    (payload_dir / status_name).write_bytes(status_payload)
    (payload_dir / audio_name).write_bytes(audio_payload)

    result = subprocess.run(
        [
            sys.executable,
            "scripts/decode_iridium_message.py",
            "--input-dir",
            str(payload_dir),
            "--output-dir",
            str(output_dir),
            "--expected-audio-band-count",
            "2",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    assert "Analizados: 2" in result.stdout
    assert "Reportes individuales: 2" in result.stdout

    decoded = json.loads((output_dir / "payloads_decoded.json").read_text())
    assert [row["momsn"] for row in decoded] == [1, 2]
    assert all(row["ok"] for row in decoded)

    pdfs = [
        output_dir / Path(status_name).with_suffix(".pdf"),
        output_dir / Path(audio_name).with_suffix(".pdf"),
        output_dir / "payloads_report.pdf",
    ]
    for pdf_path in pdfs:
        pdf_bytes = pdf_path.read_bytes()
        assert pdf_bytes.startswith(b"%PDF-1.4")
        assert b"/MediaBox [0 0 595.28 841.89]" in pdf_bytes

    status_pdf = (output_dir / Path(status_name).with_suffix(".pdf")).read_bytes()
    assert b"MSG_SYSTEM_STATUS" in status_pdf
    assert b"fsm_ok_bitmap" in status_pdf
    assert b"ll_ok_bitmap" in status_pdf
    assert b"status_flags" in status_pdf
    assert b"storage_unavailable" in status_pdf

    audio_pdf = (output_dir / Path(audio_name).with_suffix(".pdf")).read_bytes()
    assert b"MSG_AUDIO" in audio_pdf
    assert b"Bandas AudioProc" in audio_pdf
    assert b"Banda" in audio_pdf
    assert b"Ch1" in audio_pdf
