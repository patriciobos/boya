from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from modules.support.system_config import get_data_path, utc_minus_3_timestamp


def compact_utc_timestamp() -> str:
    return utc_minus_3_timestamp()


def data_source_for(low_level: Any) -> str | None:
    cls = low_level.__class__
    if cls.__name__.endswith("Mock") or "ll_mocks" in getattr(cls, "__module__", ""):
        return "hardware mock"
    return None


def _contains_firmware_mock(value: Any) -> bool:
    if isinstance(value, dict):
        if str(value.get("firmware", "")).strip().lower() == "mock":
            return True
        return any(_contains_firmware_mock(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_firmware_mock(item) for item in value)
    return False


def _normalize_source(data: dict[str, Any], source: str | None) -> str | None:
    if _contains_firmware_mock(data):
        return "firmware mock"
    if source in (None, "", "hardware", "unknown"):
        return None
    if source == "mock":
        return "hardware mock"
    return source


class SensorDataLogger:
    def __init__(self, module_name: str, include_module: bool = True, file_stem: str | None = None):
        self.module_name = module_name
        self.include_module = include_module
        stem = file_stem or module_name.lower()
        self.file_path = get_data_path() / f"{stem}_readings.jsonl"
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, data: dict[str, Any], source: str | None = None) -> None:
        entry = {
            "timestamp": compact_utc_timestamp(),
            "data": data,
        }
        if self.include_module:
            entry["module"] = self.module_name
        normalized_source = _normalize_source(data, source)
        if normalized_source is not None:
            entry["source"] = normalized_source
        with open(self.file_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, separators=(",", ":")))
            handle.write("\n")


class SystemStatusLogger:
    def __init__(self):
        self.file_path = get_data_path() / "system_status.json"
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, report: dict[str, Any]) -> None:
        with open(self.file_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
