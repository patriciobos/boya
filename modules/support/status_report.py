from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from modules.support.system_config import get_logs_path, utc_minus_3_timestamp
from modules.support.ll_factory import get_mocked_module_names, mock_source_for


class StatusReport:
    def __init__(self):
        self.report: dict[str, Any] = {
            "last_updated": None,
            "modules": {},
            "summary": {},
        }
        self.path = get_logs_path() / "system_status.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.mock_modules = set(get_mocked_module_names())

    def update(self, origin: str, state: str, action: str | None, result: str | None, details: Any = None) -> None:
        self.report["last_updated"] = utc_minus_3_timestamp()
        module_report = self.report["modules"].setdefault(origin, {})
        mode = "mock" if origin in self.mock_modules else "hardware"
        module_update = {
            "state": state,
            "mode": mode,
            "last_action": action,
            "last_result": result,
            "last_details": details,
            "updated_at": self.report["last_updated"],
        }
        if mode == "mock":
            module_update["source"] = "hardware mock"
            module_update["mock_source"] = mock_source_for(origin)
        else:
            module_update.pop("source", None)
            module_update.pop("mock_source", None)
        module_report.update(module_update)

    def write(self) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(self.report, handle, indent=2, ensure_ascii=False)
