from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modules.support.system_config import get_logs_path


class StatusReport:
    def __init__(self):
        self.report: dict[str, Any] = {
            "last_updated": None,
            "modules": {},
            "summary": {},
        }
        self.path = get_logs_path() / "system_status.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def update(self, origin: str, state: str, action: str | None, result: str | None, details: Any = None) -> None:
        self.report["last_updated"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        module_report = self.report["modules"].setdefault(origin, {})
        module_report.update({
            "state": state,
            "last_action": action,
            "last_result": result,
            "last_details": details,
            "updated_at": self.report["last_updated"],
        })

    def write(self) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(self.report, handle, indent=2, ensure_ascii=False)
