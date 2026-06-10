import json
import re

from modules.support import data_logger


def test_sensor_data_logger_uses_compact_utc_timestamp(monkeypatch, tmp_path):
    monkeypatch.setattr(data_logger, "get_data_path", lambda: tmp_path)

    logger = data_logger.SensorDataLogger("AHT10")
    logger.log({"temperature_c": 25.0}, source="mock")

    entry = json.loads((tmp_path / "aht10_readings.jsonl").read_text(encoding="utf-8"))
    assert entry["module"] == "AHT10"
    assert entry["source"] == "mock"
    assert entry["data"] == {"temperature_c": 25.0}
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", entry["timestamp"])


def test_data_source_for_distinguishes_mocks():
    class RealLowLevel:
        pass

    class SomeLowLevelMock:
        pass

    assert data_logger.data_source_for(RealLowLevel()) == "hardware"
    assert data_logger.data_source_for(SomeLowLevelMock()) == "mock"
