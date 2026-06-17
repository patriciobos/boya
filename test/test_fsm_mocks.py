import importlib
import json
import os
import time
from multiprocessing import Queue
from pathlib import Path
from queue import Empty

import pytest

from modules.support.base_fsm import Message, MessageID, State, run_fsm_self_test


class CapturingDataLogger:
    def __init__(self):
        self.entries = []
        self.sources = []

    def log(self, data, source=None):
        self.entries.append(data)
        self.sources.append(source)


def _reload_modules_with_mocks(monkeypatch):
    monkeypatch.setenv("USE_LL_MOCKS", "1")

    import modules.support.ll_factory as ll_factory
    importlib.reload(ll_factory)

    import modules.audioProc_fsm as audio_proc_fsm
    import modules.behringer_fsm as behringer_fsm
    import modules.windsonic_fsm as windsonic_fsm
    import modules.iridium_fsm as iridium_fsm
    import modules.aht10_fsm as aht10_fsm
    import modules.ais_fsm as ais_fsm
    import modules.mpu6050_fsm as mpu6050_fsm
    import modules.xtra2210_fsm as xtra2210_fsm

    importlib.reload(audio_proc_fsm)
    importlib.reload(behringer_fsm)
    importlib.reload(windsonic_fsm)
    importlib.reload(iridium_fsm)
    importlib.reload(aht10_fsm)
    importlib.reload(ais_fsm)
    importlib.reload(mpu6050_fsm)
    importlib.reload(xtra2210_fsm)

    return {
        "AudioProc": audio_proc_fsm,
        "Behringer": behringer_fsm,
        "Windsonic": windsonic_fsm,
        "Iridium": iridium_fsm,
        "AHT10": aht10_fsm,
        "AIS": ais_fsm,
        "MPU6050": mpu6050_fsm,
        "XTRA2210": xtra2210_fsm,
    }


def _wait_for_state(fsm, target_states, max_iters=200):
    for _ in range(max_iters):
        fsm.update()
        if fsm.state in target_states:
            return True
        time.sleep(0.01)
    return False


def _drain_status_queue(status_queue):
    messages = []
    deadline = time.time() + 0.3
    while True:
        try:
            messages.append(status_queue.get_nowait())
            deadline = time.time() + 0.03
        except Empty:
            if time.time() >= deadline:
                return messages
            time.sleep(0.01)
    return messages


def test_audio_proc_fsm_uses_mock_and_self_tests_with_mock(monkeypatch):
    modules = _reload_modules_with_mocks(monkeypatch)

    fsm = modules["AudioProc"].AudioProcHandlerFSM()
    assert fsm.ll.__class__.__name__.endswith("Mock")

    ok, report = run_fsm_self_test(fsm)
    assert ok, report
    assert report["final_state"] == "IDLE"


def test_audio_proc_fsm_process_with_mock_logs_output(monkeypatch, tmp_path):
    modules = _reload_modules_with_mocks(monkeypatch)

    fsm = modules["AudioProc"].AudioProcHandlerFSM()
    fsm.data_logger = CapturingDataLogger()
    status_queue = Queue()
    fsm.status_queue = status_queue

    input_path = tmp_path / "recording.wav"
    input_path.write_bytes(b"mock wav placeholder")

    fsm.handle_message(Message(MessageID.SIG_INIT))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE

    fsm.handle_message(Message(MessageID.SIG_PROCESS, {"file": str(input_path)}))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR}, max_iters=400)
    assert fsm.state == State.IDLE

    logged = fsm.data_logger.entries[-1]
    assert set(logged) == {"input_file", "output_file"}
    assert logged["input_file"].endswith("recording.wav")
    output_path = Path(logged["output_file"])
    assert output_path.exists()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert set(payload) == {"timestamp", "relative_band_power_db"}
    assert payload["relative_band_power_db"] == [[1.0], [2.0]]
    assert fsm.data_logger.sources[-1] == "hardware mock"


def _use_temp_config(monkeypatch, tmp_path, config):
    import modules.support.system_config as system_config

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(system_config, "CONFIG_PATH", config_path)
    monkeypatch.setattr(system_config, "_default_config", None)
    return system_config


def _use_temp_mocks_config(monkeypatch, tmp_path, config):
    import modules.support.system_config as system_config

    mocks_config_path = tmp_path / "mocks.json"
    mocks_config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(system_config, "MOCKS_CONFIG_PATH", mocks_config_path)
    monkeypatch.setattr(system_config, "_default_mocks_config", None)
    return system_config


def test_mocks_can_be_enabled_from_mocks_config(monkeypatch, tmp_path):
    for name in (
        "USE_LL_MOCKS",
        "USE_MOCK_AHT10",
        "USE_MOCK_AIS",
        "USE_MOCK_AUDIOPROC",
        "USE_MOCK_BEHRINGER",
        "USE_MOCK_IRIDIUM",
        "USE_MOCK_MPU6050",
        "USE_MOCK_WINDSONIC",
        "USE_MOCK_XTRA2210",
    ):
        monkeypatch.delenv(name, raising=False)
    _use_temp_mocks_config(monkeypatch, tmp_path, {"mock_modules": ["ais", "XTRA2210"]})

    import modules.support.ll_factory as ll_factory
    importlib.reload(ll_factory)

    assert ll_factory.is_mock_enabled_for("AIS") is True
    assert ll_factory.is_mock_enabled_for("XTRA2210") is True
    assert ll_factory.is_mock_enabled_for("Windsonic") is False
    assert ll_factory.mock_source_for("AIS") == "config"
    assert ll_factory.get_low_level_class("AIS").__name__ == "AISLowLevelMock"
    assert ll_factory.validate_mock_configuration()["mock_modules"] == ["AIS", "XTRA2210"]


def test_global_mock_env_rejects_partial_config(monkeypatch, tmp_path):
    monkeypatch.setenv("USE_LL_MOCKS", "1")
    _use_temp_mocks_config(monkeypatch, tmp_path, {"mock_modules": ["AIS"]})

    import modules.support.ll_factory as ll_factory
    importlib.reload(ll_factory)

    with pytest.raises(RuntimeError, match="Ambiguous mock configuration"):
        ll_factory.validate_mock_configuration()


def test_mock_modules_fallback_to_main_config_when_mocks_config_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("USE_LL_MOCKS", raising=False)
    _use_temp_config(monkeypatch, tmp_path, {"mock_modules": ["AIS"]})

    import modules.support.system_config as system_config
    monkeypatch.setattr(system_config, "MOCKS_CONFIG_PATH", tmp_path / "missing_mocks.json")
    monkeypatch.setattr(system_config, "_default_mocks_config", None)

    assert system_config.get_configured_mock_modules() == ["AIS"]


def test_status_report_tags_mock_modules(monkeypatch, tmp_path):
    monkeypatch.delenv("USE_LL_MOCKS", raising=False)
    _use_temp_config(monkeypatch, tmp_path, {"logs_dir": str(tmp_path / "logs")})
    _use_temp_mocks_config(monkeypatch, tmp_path, {"mock_modules": ["AIS"]})

    import modules.support.ll_factory as ll_factory
    import modules.support.status_report as status_report
    importlib.reload(ll_factory)
    importlib.reload(status_report)

    report = status_report.StatusReport()
    report.update("AIS", "IDLE", "acquire", "ok", {})
    report.update("Windsonic", "IDLE", "acquire", "ok", {})

    assert report.report["modules"]["AIS"]["mode"] == "mock"
    assert report.report["modules"]["AIS"]["source"] == "hardware mock"
    assert report.report["modules"]["AIS"]["mock_source"] == "config"
    assert report.report["modules"]["Windsonic"]["mode"] == "hardware"
    assert "source" not in report.report["modules"]["Windsonic"]


def test_mocks_can_be_enabled_per_module(monkeypatch):
    monkeypatch.delenv("USE_LL_MOCKS", raising=False)
    monkeypatch.setenv("USE_MOCK_AUDIOPROC", "1")
    monkeypatch.delenv("USE_MOCK_BEHRINGER", raising=False)

    import modules.support.system_config as system_config
    monkeypatch.setattr(system_config, "_default_config", {"mock_modules": []})

    import modules.support.ll_factory as ll_factory
    importlib.reload(ll_factory)

    assert ll_factory.is_mock_enabled_for("AudioProc") is True
    assert ll_factory.is_mock_enabled_for("Behringer") is False
    assert ll_factory.is_mock_enabled_for("Windsonic") is False

    audio_proc_cls = ll_factory.get_low_level_class("AudioProc")
    assert audio_proc_cls.__name__ == "AudioProcLowLevelMock"

    # Ensure only the targeted module uses a mock when individual env vars are set.
    assert ll_factory.get_low_level_class("AudioProc").__name__ == "AudioProcLowLevelMock"


def test_behringer_fsm_acquire_with_mock(monkeypatch):
    modules = _reload_modules_with_mocks(monkeypatch)

    fsm = modules["Behringer"].BehringerHandlerFSM()
    status_queue = Queue()
    fsm.status_queue = status_queue
    fsm.data_logger = CapturingDataLogger()

    fsm.handle_message(Message(MessageID.SIG_INIT))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE

    fsm.handle_message(Message(MessageID.SIG_ACQUIRE, {"duration": 1}))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE
    assert fsm.ll.output_path is not None
    assert Path(fsm.ll.output_path).exists()
    assert not Path(fsm.data_logger.entries[-1]["file"]).is_absolute()
    logged = fsm.data_logger.entries[-1]
    assert logged["duration_s"] == 1
    assert logged["sample_rate_hz"] == 192000
    assert logged["channels"] == 1
    assert logged["size_bytes"] > 0
    assert "status" not in logged
    assert "duration" not in logged
    assert fsm.data_logger.sources[-1] == "hardware mock"

    fsm.ll.deinit()


def test_windsonic_fsm_acquire_with_mock(monkeypatch):
    modules = _reload_modules_with_mocks(monkeypatch)

    fsm = modules["Windsonic"].WindsonicHandlerFSM()
    status_queue = Queue()
    fsm.status_queue = status_queue
    fsm.data_logger = CapturingDataLogger()

    fsm.handle_message(Message(MessageID.SIG_INIT))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE

    fsm.handle_message(Message(MessageID.SIG_ACQUIRE, {"num": 3}))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE
    messages = _drain_status_queue(status_queue)
    assert any(msg[1].id == MessageID.ACTION_RESULT for msg in messages)
    logged = fsm.data_logger.entries[-1]
    assert logged["samples"] == 3
    assert logged["valid_samples"] == 3
    assert logged["wind_speed_mps_avg"] == 4.0
    assert logged["wind_speed_mps_min"] == 3.0
    assert logged["wind_speed_mps_max"] == 5.0
    assert logged["wind_direction_deg_avg"] == 180.0
    assert logged["direction_valid"] is True
    assert "status" not in logged
    assert fsm.data_logger.sources[-1] == "hardware mock"

    fsm.ll.deinit()


def test_iridium_fsm_skips_unsupported_text_transmit(monkeypatch):
    modules = _reload_modules_with_mocks(monkeypatch)

    fsm = modules["Iridium"].IridiumHandlerFSM()
    status_queue = Queue()
    fsm.status_queue = status_queue

    fsm.handle_message(Message(MessageID.SIG_INIT))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE
    _drain_status_queue(status_queue)

    fsm.handle_message(
        Message(
            MessageID.SIG_TRANSMIT,
            {
                "mode": "text",
                "text": "unsupported",
                "clear_after_success": True,
                "max_attempts": 1,
                "retry_delay_s": 0.1,
            },
        )
    )
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE

    messages = _drain_status_queue(status_queue)
    action_results = [msg[1] for msg in messages if msg[1].id == MessageID.ACTION_RESULT]
    assert action_results
    assert action_results[-1].params["result"] == "ok"
    details = action_results[-1].params["details"]["transmit"]
    assert details["mode"] == "text"
    assert details["skipped"] is True
    assert details["reason"] == "unsupported_transmit_mode"

    fsm.ll.deinit()


def test_iridium_fsm_transmits_alive_binary_with_mock(monkeypatch, tmp_path):
    modules = _reload_modules_with_mocks(monkeypatch)
    iridium_module = modules["Iridium"]

    logs_path = tmp_path / "logs"
    data_path = tmp_path / "data"
    logs_path.mkdir()
    data_path.mkdir()
    (logs_path / "system_status.json").write_text(
        json.dumps({
            "modules": {
                "AHT10": {"state": "IDLE", "last_result": "ok"},
                "AIS": {"state": "IDLE", "last_result": "ok"},
                "AudioProc": {"state": "IDLE", "last_result": "ok"},
                "Behringer": {"state": "IDLE", "last_result": "ok"},
                "Iridium": {"state": "IDLE", "last_result": "ok"},
                "MPU6050": {"state": "IDLE", "last_result": "ok"},
                "Windsonic": {"state": "IDLE", "last_result": "ok"},
                "XTRA2210": {"state": "IDLE", "last_result": "ok"},
            }
        }),
        encoding="utf-8",
    )
    (data_path / "ais_readings.jsonl").write_text(
        json.dumps({"data": {"gps_fix": True, "lat": -34.1, "lon": -58.2}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(iridium_module, "get_logs_path", lambda: logs_path)
    monkeypatch.setattr(iridium_module, "get_data_path", lambda: data_path)
    monkeypatch.setattr(iridium_module, "get_config_value", lambda key, default=None: True)

    fsm = iridium_module.IridiumHandlerFSM()
    status_queue = Queue()
    fsm.status_queue = status_queue

    fsm.handle_message(Message(MessageID.SIG_INIT))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE
    _drain_status_queue(status_queue)

    fsm.handle_message(
        Message(
            MessageID.SIG_TRANSMIT,
            {"mode": "alive", "clear_after_success": True, "max_attempts": 1, "retry_delay_s": 0.1},
        )
    )
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE

    messages = _drain_status_queue(status_queue)
    action_results = [msg[1] for msg in messages if msg[1].id == MessageID.ACTION_RESULT]
    assert action_results
    details = action_results[-1].params["details"]
    assert details["alive"]["payload_size_bytes"] == 16
    assert details["alive"]["fsm_status_bits"] == 0
    assert details["alive"]["ll_status_bits"] == 0
    assert details["alive"]["fsm_status_bits_binary"] == "00000000"
    assert details["alive"]["ll_status_bits_binary"] == "00000000"
    assert details["alive"]["status_bytes_binary"] == "00000000 00000000"
    assert details["alive"]["gps_fix"] is True
    assert details["transmit"]["mode"] == "binary"
    assert details["transmit"]["size"] == 16

    fsm.ll.deinit()


def test_iridium_fsm_logs_audio_binary_when_transmit_disabled(monkeypatch, tmp_path):
    modules = _reload_modules_with_mocks(monkeypatch)
    iridium_module = modules["Iridium"]

    logs_path = tmp_path / "logs"
    data_path = tmp_path / "data"
    audio_path = data_path / "audio_proc" / "audioProc_test.json"
    logs_path.mkdir()
    audio_path.parent.mkdir(parents=True)
    (logs_path / "system_status.json").write_text(
        json.dumps({
            "modules": {
                "AHT10": {"state": "IDLE", "last_result": "ok"},
                "AIS": {"state": "ERROR", "last_result": "ok"},
                "AudioProc": {"state": "IDLE", "last_result": "ok"},
                "Behringer": {"state": "IDLE", "last_result": "ok"},
                "Iridium": {"state": "IDLE", "last_result": "ok"},
                "MPU6050": {"state": "IDLE", "last_result": "ok"},
                "Windsonic": {"state": "IDLE", "last_result": "ok"},
                "XTRA2210": {"state": "IDLE", "last_result": "ok"},
            }
        }),
        encoding="utf-8",
    )
    audio_path.write_text(
        json.dumps({
            "timestamp": "2026-06-13T08:00:08-03:00",
            "relative_band_power_db": [[None if index == 0 else float(index)] for index in range(49)],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(iridium_module, "get_logs_path", lambda: logs_path)
    monkeypatch.setattr(iridium_module, "get_data_path", lambda: data_path)
    monkeypatch.setattr(iridium_module, "get_config_value", lambda key, default=None: False)
    monkeypatch.setattr(iridium_module, "PROJECT_ROOT", tmp_path)

    fsm = iridium_module.IridiumHandlerFSM()
    status_queue = Queue()
    fsm.status_queue = status_queue

    fsm.handle_message(Message(MessageID.SIG_INIT))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE
    _drain_status_queue(status_queue)

    fsm.handle_message(Message(MessageID.SIG_TRANSMIT, {"mode": "audio", "audio": {"output": "data/audio_proc/audioProc_test.json"}}))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE

    messages = _drain_status_queue(status_queue)
    action_results = [msg[1] for msg in messages if msg[1].id == MessageID.ACTION_RESULT]
    assert action_results[-1].params["result"] == "ok"
    details = action_results[-1].params["details"]
    assert details["audio"]["message_type"] == "audioProc"
    assert details["audio"]["message_type_byte"] == 0x03
    assert details["audio"]["frequency_band_count"] == 49
    assert details["audio"]["channel_count"] == 1
    assert details["audio"]["audio_value_count"] == 49
    assert details["audio"]["bytes_per_channel"] == 49
    assert details["audio"]["crc_size_bytes"] == 2
    assert details["transmit"]["reason"] == "iridium_transmit_disabled"

    entry = json.loads((logs_path / "iridium_transmit_requests.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["mode"] == "binary"
    assert entry["payload_size_bytes"] == 56
    assert entry["payload_hex"].startswith("03")
    assert entry["details"]["message_type_byte"] == 0x03
    assert entry["skipped_reason"] == "iridium_transmit_disabled"

    fsm.ll.deinit()


def test_iridium_fsm_audio_uses_latest_audioproc_output_when_not_provided(monkeypatch, tmp_path):
    modules = _reload_modules_with_mocks(monkeypatch)
    iridium_module = modules["Iridium"]

    logs_path = tmp_path / "logs"
    data_path = tmp_path / "data"
    audio_path = data_path / "audio_proc" / "audioProc_latest.json"
    logs_path.mkdir()
    audio_path.parent.mkdir(parents=True)
    (logs_path / "system_status.json").write_text(
        json.dumps({"modules": {"AudioProc": {"state": "IDLE", "last_result": "ok"}}}),
        encoding="utf-8",
    )
    audio_path.write_text(
        json.dumps({
            "timestamp": "2026-06-13T08:00:08-03:00",
            "relative_band_power_db": [[float(index)] for index in range(49)],
        }),
        encoding="utf-8",
    )
    (data_path / "audioProc_readings.jsonl").write_text(
        json.dumps({
            "timestamp": "2026-06-13T08:01:00-03:00",
            "data": {
                "input_file": "data/recordings/latest.wav",
                "output_file": "data/audio_proc/audioProc_latest.json",
            },
        }) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(iridium_module, "get_logs_path", lambda: logs_path)
    monkeypatch.setattr(iridium_module, "get_data_path", lambda: data_path)
    monkeypatch.setattr(iridium_module, "get_config_value", lambda key, default=None: False)
    monkeypatch.setattr(iridium_module, "PROJECT_ROOT", tmp_path)

    fsm = iridium_module.IridiumHandlerFSM()
    status_queue = Queue()
    fsm.status_queue = status_queue

    fsm.handle_message(Message(MessageID.SIG_INIT))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE
    _drain_status_queue(status_queue)

    fsm.handle_message(Message(MessageID.SIG_TRANSMIT, {"mode": "audio"}))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE

    messages = _drain_status_queue(status_queue)
    action_results = [msg[1] for msg in messages if msg[1].id == MessageID.ACTION_RESULT]
    details = action_results[-1].params["details"]
    assert action_results[-1].params["result"] == "ok"
    assert details["audio"]["message_type"] == "audioProc"
    assert details["audio"]["message_type_byte"] == 0x03
    assert details["audio"]["audio_output"] == "data/audio_proc/audioProc_latest.json"
    assert details["transmit"]["reason"] == "iridium_transmit_disabled"

    entry = json.loads((logs_path / "iridium_transmit_requests.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["payload_size_bytes"] == 56
    assert entry["details"]["audio_output"] == "data/audio_proc/audioProc_latest.json"

    fsm.ll.deinit()


def test_iridium_fsm_skips_audio_when_payload_is_unavailable(monkeypatch, tmp_path):
    modules = _reload_modules_with_mocks(monkeypatch)
    iridium_module = modules["Iridium"]

    logs_path = tmp_path / "logs"
    data_path = tmp_path / "data"
    logs_path.mkdir()
    data_path.mkdir()
    (logs_path / "system_status.json").write_text(
        json.dumps({"modules": {"AudioProc": {"state": "ERROR", "last_result": "error"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(iridium_module, "get_logs_path", lambda: logs_path)
    monkeypatch.setattr(iridium_module, "get_data_path", lambda: data_path)
    monkeypatch.setattr(iridium_module, "get_config_value", lambda key, default=None: False)

    fsm = iridium_module.IridiumHandlerFSM()
    status_queue = Queue()
    fsm.status_queue = status_queue

    fsm.handle_message(Message(MessageID.SIG_INIT))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE
    _drain_status_queue(status_queue)

    fsm.handle_message(Message(MessageID.SIG_TRANSMIT, {"mode": "audio", "audio": {"relative_band_power_db": None}}))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE

    messages = _drain_status_queue(status_queue)
    action_results = [msg[1] for msg in messages if msg[1].id == MessageID.ACTION_RESULT]
    assert action_results[-1].params["result"] == "ok"
    details = action_results[-1].params["details"]
    assert details["audio"]["skipped"] is True
    assert details["audio"]["reason"] == "audioProc_payload_unavailable"
    assert details["transmit"]["skipped"] is True
    assert not (logs_path / "iridium_transmit_requests.jsonl").exists()

    fsm.ll.deinit()


def test_iridium_fsm_logs_alive_when_transmit_disabled(monkeypatch, tmp_path):
    modules = _reload_modules_with_mocks(monkeypatch)
    iridium_module = modules["Iridium"]

    logs_path = tmp_path / "logs"
    data_path = tmp_path / "data"
    logs_path.mkdir()
    data_path.mkdir()
    (logs_path / "system_status.json").write_text(
        json.dumps({"modules": {}}),
        encoding="utf-8",
    )
    (data_path / "ais_readings.jsonl").write_text(
        json.dumps({"data": {"gps_fix": False, "lat": None, "lon": None}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(iridium_module, "get_logs_path", lambda: logs_path)
    monkeypatch.setattr(iridium_module, "get_data_path", lambda: data_path)
    monkeypatch.setattr(iridium_module, "get_config_value", lambda key, default=None: False)

    fsm = iridium_module.IridiumHandlerFSM()
    status_queue = Queue()
    fsm.status_queue = status_queue

    fsm.handle_message(Message(MessageID.SIG_INIT))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE
    _drain_status_queue(status_queue)

    fsm.handle_message(Message(MessageID.SIG_TRANSMIT, {"mode": "alive"}))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE

    messages = _drain_status_queue(status_queue)
    action_results = [msg[1] for msg in messages if msg[1].id == MessageID.ACTION_RESULT]
    assert action_results[-1].params["result"] == "ok"
    details = action_results[-1].params["details"]
    assert details["transmit"]["skipped"] is True
    assert details["transmit"]["reason"] == "iridium_transmit_disabled"

    log_path = logs_path / "iridium_transmit_requests.jsonl"
    entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["mode"] == "binary"
    assert entry["payload_size_bytes"] == 16
    assert entry["skipped_reason"] == "iridium_transmit_disabled"
    assert entry["payload_hex"]

    fsm.ll.deinit()


def test_aht10_fsm_acquire_with_mock(monkeypatch):
    modules = _reload_modules_with_mocks(monkeypatch)

    fsm = modules["AHT10"].AHT10HandlerFSM()
    status_queue = Queue()
    fsm.status_queue = status_queue
    fsm.data_logger = CapturingDataLogger()

    fsm.handle_message(Message(MessageID.SIG_INIT))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE

    fsm.handle_message(Message(MessageID.SIG_ACQUIRE))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE
    messages = _drain_status_queue(status_queue)
    assert any(msg[1].id == MessageID.ACTION_RESULT for msg in messages)
    assert any(msg[1].params.get("action") == "acquire" for msg in messages)
    logged = fsm.data_logger.entries[-1]
    assert logged == {"temperature_c": 25.0, "humidity_rh": 50.0}
    assert "raw" not in logged
    assert fsm.data_logger.sources[-1] == "hardware mock"

    fsm.ll.deinit()


def test_ais_fsm_acquire_with_mock(monkeypatch):
    modules = _reload_modules_with_mocks(monkeypatch)

    fsm = modules["AIS"].AISHandlerFSM()
    status_queue = Queue()
    fsm.status_queue = status_queue
    fsm.data_logger = CapturingDataLogger()

    fsm.handle_message(Message(MessageID.SIG_INIT))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE

    fsm.handle_message(Message(MessageID.SIG_ACQUIRE, {"seconds": 0.1}))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE
    messages = _drain_status_queue(status_queue)
    assert any(msg[1].id == MessageID.ACTION_RESULT for msg in messages)
    assert any(msg[1].params.get("action") == "acquire" for msg in messages)
    logged = fsm.data_logger.entries[-1]
    assert logged == {
        "gps_fix": True,
        "lat": 0.0,
        "lon": 0.0,
        "satellites": 4,
        "hdop": 0.9,
        "own_transmit_messages": 1,
    }
    assert "navigation" not in logged
    assert "lines" not in logged
    assert fsm.data_logger.sources[-1] == "hardware mock"

    fsm.ll.deinit()


def test_ais_fsm_acquire_without_fresh_traffic_goes_error(monkeypatch):
    modules = _reload_modules_with_mocks(monkeypatch)

    class NoTrafficAISLowLevel:
        is_open = False

        def init(self):
            return True

        def full_test(self):
            return True, {}

        def open(self):
            self.is_open = True
            return True

        def read_lines(self, seconds=1.0):
            return ["noise", "$GPRMC,invalid*00"]

        def get_navigation(self):
            return {
                "lat": None,
                "lon": None,
                "fix": False,
                "num_sats": 0,
                "hdop": None,
            }

        def deinit(self):
            self.is_open = False
            return True

    fsm = modules["AIS"].AISHandlerFSM()
    fsm.ll = NoTrafficAISLowLevel()
    status_queue = Queue()
    fsm.status_queue = status_queue
    fsm.data_logger = CapturingDataLogger()

    fsm.handle_message(Message(MessageID.SIG_INIT))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE

    fsm.handle_message(Message(MessageID.SIG_ACQUIRE, {"seconds": 0.1}))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.ERROR
    assert fsm.data_logger.entries == []

    messages = _drain_status_queue(status_queue)
    acquire_results = [
        msg[1].params
        for msg in messages
        if msg[1].id == MessageID.ACTION_RESULT and msg[1].params.get("action") == "acquire"
    ]
    assert acquire_results
    assert acquire_results[-1]["result"] == "error"
    assert "No fresh AIS/GPS traffic detected" in acquire_results[-1]["error"]


def test_mpu6050_fsm_acquire_with_mock(monkeypatch):
    modules = _reload_modules_with_mocks(monkeypatch)

    fsm = modules["MPU6050"].MPU6050HandlerFSM()
    status_queue = Queue()
    fsm.status_queue = status_queue
    fsm.data_logger = CapturingDataLogger()

    fsm.handle_message(Message(MessageID.SIG_INIT))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE

    fsm.handle_message(Message(MessageID.SIG_ACQUIRE))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE
    messages = _drain_status_queue(status_queue)
    assert any(msg[1].id == MessageID.ACTION_RESULT for msg in messages)
    assert any(msg[1].params.get("action") == "acquire" for msg in messages)
    logged = fsm.data_logger.entries[-1]
    assert logged == {
        "ax_g": 0.0,
        "ay_g": 0.0,
        "az_g": 0.0,
        "gx_dps": 0.0,
        "gy_dps": 0.0,
        "gz_dps": 0.0,
    }
    assert "temperature_c" not in logged
    assert "temp_c" not in logged
    assert "accel_raw" not in logged
    assert "gyro_raw" not in logged
    assert fsm.data_logger.sources[-1] == "hardware mock"

    fsm.ll.deinit()


def test_xtra2210_fsm_acquire_with_mock(monkeypatch):
    modules = _reload_modules_with_mocks(monkeypatch)

    fsm = modules["XTRA2210"].XTRA2210HandlerFSM()
    status_queue = Queue()
    fsm.status_queue = status_queue
    fsm.data_logger = CapturingDataLogger()

    fsm.handle_message(Message(MessageID.SIG_INIT))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE

    fsm.handle_message(Message(MessageID.SIG_ACQUIRE))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE
    messages = _drain_status_queue(status_queue)
    assert any(msg[1].id == MessageID.ACTION_RESULT for msg in messages)
    assert any(msg[1].params.get("action") == "acquire" for msg in messages)
    logged = fsm.data_logger.entries[-1]
    assert logged["pv_voltage_v"] == 12.0
    assert logged["pv_current_a"] == 1.2
    assert logged["load_current_a"] == 0.5
    assert logged["battery_voltage_v"] == 12.6
    assert logged["battery_soc_pct"] == 85.0
    assert logged["battery_temperature_c"] == 25.0
    assert logged["device_temperature_c"] == 26.0
    assert "model" not in logged
    assert "firmware" not in logged
    assert "load_voltage_v" not in logged
    assert "load_power_w" not in logged
    assert "pv" not in logged
    assert fsm.data_logger.sources[-1] == "firmware mock"

    fsm.ll.deinit()
