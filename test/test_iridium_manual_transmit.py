from __future__ import annotations

import json
from datetime import datetime, timezone

from modules.support.iridium_protocol import (
    SYSTEM_STATUS_PAYLOAD_SIZE,
    build_audio_proc_payload,
    decode_message,
    expected_audio_band_count,
    pack_system_status,
)
from modules.support.iridium_payloads import AudioProcPayloadUnavailable
from scripts import iridium_manual_transmit as manual


class FailingIridiumLowLevel:
    def __init__(self, *args, **kwargs):
        raise AssertionError("dry-run must not instantiate IridiumLowLevel")


class CapturingIridiumLowLevel:
    instances = []

    def __init__(self, *args, **kwargs):
        self.init_called = False
        self.deinit_called = False
        self.sent = []
        CapturingIridiumLowLevel.instances.append(self)

    def init(self):
        self.init_called = True
        return True

    def deinit(self):
        self.deinit_called = True
        return True

    def send_sbd_binary(self, payload, *, clear_after_success=True, max_attempts=3, retry_delay_s=10.0):
        self.sent.append({
            "payload": payload,
            "clear_after_success": clear_after_success,
            "max_attempts": max_attempts,
            "retry_delay_s": retry_delay_s,
        })
        return True, {"mode": "binary", "size": len(payload)}


def test_system_status_dry_run_builds_decodes_and_does_not_call_modem(monkeypatch, tmp_path, capsys):
    payload = pack_system_status(fsm_ok_bitmap=0xFF, ll_ok_bitmap=0xFE)

    monkeypatch.setattr(manual, "IridiumLowLevel", FailingIridiumLowLevel)
    monkeypatch.setattr(manual, "get_logs_path", lambda: tmp_path)
    monkeypatch.setattr(manual, "build_current_system_status_payload", lambda: (payload, {"message_type_name": "MSG_SYSTEM_STATUS"}))

    result = manual.main(["system-status", "--dry-run", "--json"])

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["dry_run"] is True
    assert output["payload_size"] == SYSTEM_STATUS_PAYLOAD_SIZE == 11
    assert output["decoded"]["message_type"] == "MSG_SYSTEM_STATUS"
    assert decode_message(bytes.fromhex(output["payload_hex"]))["message_type"] == "MSG_SYSTEM_STATUS"


def test_latest_audio_dry_run_uses_shared_helper_and_does_not_call_modem(monkeypatch, tmp_path, capsys):
    expected_bands = expected_audio_band_count()
    bands = [[float(index)] for index in range(expected_bands)]
    payload = build_audio_proc_payload(
        timestamp=datetime(2026, 6, 19, 16, 0, tzinfo=timezone.utc),
        relative_band_power_db=bands,
        expected_band_count=expected_bands,
    )
    calls = {"helper": 0}

    def fake_build_latest_audio_proc_payload():
        calls["helper"] += 1
        return payload, {
            "audio_timestamp": "2026-06-19T16:00:00-03:00",
            "audio_output": "data/audio_proc/audioProc_test.json",
            "packing": "DELTA_PREVIOUS_INT8",
        }

    monkeypatch.setattr(manual, "IridiumLowLevel", FailingIridiumLowLevel)
    monkeypatch.setattr(manual, "get_logs_path", lambda: tmp_path)
    monkeypatch.setattr(manual, "build_latest_audio_proc_payload", fake_build_latest_audio_proc_payload)

    result = manual.main(["latest-audio", "--dry-run", "--json"])

    assert result == 0
    assert calls["helper"] == 1
    output = json.loads(capsys.readouterr().out)
    assert output["dry_run"] is True
    assert output["message_type_name"] == "MSG_AUDIO"
    assert output["packing"] == "DELTA_PREVIOUS_INT8"
    assert output["bands"] == expected_bands
    assert decode_message(bytes.fromhex(output["payload_hex"]), expected_audio_band_count=expected_bands)


def test_send_with_mock_calls_send_sbd_binary_once(monkeypatch):
    CapturingIridiumLowLevel.instances = []
    payload = b"\x01\x02\x03"

    monkeypatch.setattr(manual, "IridiumLowLevel", CapturingIridiumLowLevel)

    report = manual.send_payload_via_iridium(
        payload,
        max_attempts=1,
        retry_delay_s=0.0,
        clear_after_success=True,
    )

    modem = CapturingIridiumLowLevel.instances[0]
    assert report["ok"] is True
    assert modem.init_called is True
    assert modem.deinit_called is True
    assert len(modem.sent) == 1
    assert modem.sent[0]["payload"] == payload
    assert modem.sent[0]["max_attempts"] == 1


def test_latest_audio_without_data_errors_clearly_and_does_not_send(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(manual, "IridiumLowLevel", FailingIridiumLowLevel)
    monkeypatch.setattr(manual, "get_logs_path", lambda: tmp_path)
    monkeypatch.setattr(
        manual,
        "build_latest_audio_proc_payload",
        lambda: (_ for _ in ()).throw(AudioProcPayloadUnavailable("No valid AudioProc output is available")),
    )

    result = manual.main(["latest-audio", "--send", "--json"])

    assert result == 2
    stderr = capsys.readouterr().err
    assert "No valid AudioProc output is available" in stderr


def test_payload_over_340_bytes_errors_before_modem(monkeypatch):
    monkeypatch.setattr(manual, "IridiumLowLevel", FailingIridiumLowLevel)

    try:
        manual.send_payload_via_iridium(
            b"x" * 341,
            max_attempts=1,
            retry_delay_s=0.0,
            clear_after_success=False,
        )
    except ValueError as exc:
        assert "exceeds 340 bytes" in str(exc)
    else:
        raise AssertionError("expected ValueError for oversized Iridium payload")
