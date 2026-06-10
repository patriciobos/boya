from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from modules.support.system_config import get_data_path


def compact_utc_timestamp() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def data_source_for(low_level: Any) -> str:
    cls = low_level.__class__
    if cls.__name__.endswith("Mock") or "ll_mocks" in getattr(cls, "__module__", ""):
        return "mock"
    return "hardware"


class SensorDataLogger:
    def __init__(self, module_name: str):
        self.module_name = module_name
        self.file_path = get_data_path() / f"{module_name.lower()}_readings.jsonl"
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, data: dict[str, Any], source: str | None = None) -> None:
        entry = {
            "timestamp": compact_utc_timestamp(),
            "module": self.module_name,
            "source": source or "unknown",
            "data": data,
        }
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
