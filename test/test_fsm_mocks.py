import importlib
import os
import time
from multiprocessing import Queue
from pathlib import Path
from queue import Empty

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


def test_mocks_can_be_enabled_per_module(monkeypatch):
    monkeypatch.delenv("USE_LL_MOCKS", raising=False)
    monkeypatch.setenv("USE_MOCK_AUDIOPROC", "1")
    monkeypatch.delenv("USE_MOCK_BEHRINGER", raising=False)

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
    assert fsm.data_logger.entries[-1]["duration_s"] == 1
    assert fsm.data_logger.entries[-1]["status"] == "recording_completed"
    assert "duration" not in fsm.data_logger.entries[-1]
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
    assert fsm.data_logger.sources[-1] == "hardware mock"

    fsm.ll.deinit()


def test_iridium_fsm_transmit_with_mock(monkeypatch):
    modules = _reload_modules_with_mocks(monkeypatch)

    fsm = modules["Iridium"].IridiumHandlerFSM()
    status_queue = Queue()
    fsm.status_queue = status_queue

    fsm.handle_message(Message(MessageID.SIG_INIT))
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE

    fsm.handle_message(
        Message(
            MessageID.SIG_TRANSMIT,
            {
                "mode": "text",
                "text": "hello world",
                "clear_after_success": True,
                "max_attempts": 1,
                "retry_delay_s": 0.1,
            },
        )
    )
    assert _wait_for_state(fsm, {State.IDLE, State.ERROR})
    assert fsm.state == State.IDLE

    messages = _drain_status_queue(status_queue)
    assert any(msg[1].id == MessageID.ACTION_RESULT for msg in messages)
    assert any(
        msg[1].params.get("details", {}).get("mode") == "text"
        or msg[1].params.get("details", {}).get("mock") is True
        for msg in messages
    )

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
    assert fsm.data_logger.sources[-1] == "hardware mock"

    fsm.ll.deinit()


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
    assert logged["temperature_c"] == 25.0
    assert "temp_c" not in logged
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
