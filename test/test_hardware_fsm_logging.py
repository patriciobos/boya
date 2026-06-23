import importlib
import json
import os
import re
import time
from pathlib import Path

import pytest

from modules.support.base_fsm import Message, MessageID, State
from modules.support.system_config import get_data_path

TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}-03:00")

MODULES = {
    "AHT10": (
        "modules.aht10_fsm",
        "AHT10HandlerFSM",
        Message(MessageID.SIG_ACQUIRE, {"timeout": 3.0}),
    ),
    "AIS": (
        "modules.ais_fsm",
        "AISHandlerFSM",
        Message(MessageID.SIG_ACQUIRE, {"seconds": 1.0}),
    ),
    "MPU6050": (
        "modules.mpu6050_fsm",
        "MPU6050HandlerFSM",
        Message(MessageID.SIG_ACQUIRE),
    ),
    "XTRA2210": (
        "modules.xtra2210_fsm",
        "XTRA2210HandlerFSM",
        Message(MessageID.SIG_ACQUIRE),
    ),
    "Windsonic": (
        "modules.windsonic_fsm",
        "WindsonicHandlerFSM",
        Message(MessageID.SIG_ACQUIRE, {"num": 5}),
    ),
    "Behringer": (
        "modules.behringer_fsm",
        "BehringerHandlerFSM",
        Message(MessageID.SIG_ACQUIRE, {"duration": 1}),
    ),
}


def _enabled_modules():
    selected = os.getenv("HARDWARE_LOG_MODULES")
    if selected:
        names = [name.strip() for name in selected.split(",") if name.strip()]
    else:
        names = list(MODULES)

    default_skip = "" if selected else "Windsonic"
    skipped = {
        name.strip()
        for name in os.getenv("HARDWARE_LOG_SKIP", default_skip).split(",")
        if name.strip()
    }
    return [name for name in names if name not in skipped]


def _drive_until(fsm, states: set[State], timeout_s: float) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        fsm.update()
        if fsm.state in states:
            return
        time.sleep(0.05)
    raise AssertionError(
        f"FSM {fsm.name} did not reach {states}; current state={fsm.state}"
    )


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(
        1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    )


def _has_meaningful_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, dict):
        return any(_has_meaningful_value(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_has_meaningful_value(item) for item in value)
    return True


@pytest.mark.hardware
@pytest.mark.timeout(180)
@pytest.mark.parametrize("module_name", _enabled_modules())
def test_hardware_fsm_acquire_logs_real_reading(module_name, monkeypatch):
    if os.getenv("RUN_HARDWARE_TESTS", "0").strip().lower() not in (
        "1",
        "true",
        "yes",
        "on",
    ):
        pytest.skip("hardware test disabled; set RUN_HARDWARE_TESTS=1 to run")

    monkeypatch.delenv("USE_LL_MOCKS", raising=False)
    for name in MODULES:
        monkeypatch.delenv(f"USE_MOCK_{name.upper()}", raising=False)

    import modules.support.ll_factory as ll_factory

    importlib.reload(ll_factory)

    module_path, class_name, acquire_message = MODULES[module_name]
    module = importlib.import_module(module_path)
    importlib.reload(module)
    fsm = getattr(module, class_name)()

    readings_file = get_data_path() / f"{module_name.lower()}_readings.jsonl"
    before = _line_count(readings_file)

    fsm.handle_message(Message(MessageID.SIG_INIT))
    _drive_until(fsm, {State.IDLE, State.ERROR}, timeout_s=45.0)
    assert fsm.state == State.IDLE

    fsm.handle_message(acquire_message)
    _drive_until(fsm, {State.IDLE, State.ERROR}, timeout_s=90.0)
    assert fsm.state == State.IDLE

    assert readings_file.exists(), f"{readings_file} was not created"
    assert _line_count(readings_file) == before + 1

    last_line = [
        line
        for line in readings_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ][-1]
    entry = json.loads(last_line)
    if module_name in ("AHT10", "AIS", "Behringer", "MPU6050", "Windsonic", "XTRA2210"):
        assert "module" not in entry
    else:
        assert entry["module"] == module_name
    assert "source" not in entry
    assert TIMESTAMP_RE.fullmatch(entry["timestamp"]), entry["timestamp"]
    assert "." not in entry["timestamp"]
    assert entry["data"]
    assert _has_meaningful_value(entry["data"]), entry["data"]
