import json
import re

from modules.support import data_logger


def test_sensor_data_logger_uses_compact_utc_timestamp(monkeypatch, tmp_path):
    monkeypatch.setattr(data_logger, "get_data_path", lambda: tmp_path)

    logger = data_logger.SensorDataLogger("AHT10")
    logger.log({"temperature_c": 25.0}, source="hardware mock")

    entry = json.loads((tmp_path / "aht10_readings.jsonl").read_text(encoding="utf-8"))
    assert entry["module"] == "AHT10"
    assert entry["source"] == "hardware mock"
    assert entry["data"] == {"temperature_c": 25.0}
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", entry["timestamp"])


def test_data_source_for_distinguishes_mocks():
    class RealLowLevel:
        pass

    class SomeLowLevelMock:
        pass

    assert data_logger.data_source_for(RealLowLevel()) is None
    assert data_logger.data_source_for(SomeLowLevelMock()) == "hardware mock"


def test_sensor_data_logger_omits_source_for_real_hardware(monkeypatch, tmp_path):
    monkeypatch.setattr(data_logger, "get_data_path", lambda: tmp_path)

    logger = data_logger.SensorDataLogger("AHT10")
    logger.log({"temperature_c": 25.0})

    entry = json.loads((tmp_path / "aht10_readings.jsonl").read_text(encoding="utf-8"))
    assert "source" not in entry


def test_sensor_data_logger_marks_firmware_mock(monkeypatch, tmp_path):
    monkeypatch.setattr(data_logger, "get_data_path", lambda: tmp_path)

    logger = data_logger.SensorDataLogger("XTRA2210")
    logger.log({"identity": {"firmware": "mock"}})

    entry = json.loads((tmp_path / "xtra2210_readings.jsonl").read_text(encoding="utf-8"))
    assert entry["source"] == "firmware mock"


def test_sensor_data_logger_can_omit_module(monkeypatch, tmp_path):
    monkeypatch.setattr(data_logger, "get_data_path", lambda: tmp_path)

    logger = data_logger.SensorDataLogger("XTRA2210", include_module=False)
    logger.log({"battery_voltage_v": 12.0})

    entry = json.loads((tmp_path / "xtra2210_readings.jsonl").read_text(encoding="utf-8"))
    assert "module" not in entry
    assert entry["data"] == {"battery_voltage_v": 12.0}
