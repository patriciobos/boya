from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

_default_config: dict[str, Any] | None = None


def _load_config() -> dict[str, Any]:
    global _default_config
    if _default_config is not None:
        return _default_config

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            _default_config = json.load(handle)
    except FileNotFoundError:
        _default_config = {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in config file {CONFIG_PATH}: {exc}") from exc
    return _default_config


def get_config_value(key: str, default: Any = None) -> Any:
    config = _load_config()
    return config.get(key, default)


def get_schedule(module_name: str, default: Any = None) -> Any:
    schedules = get_config_value("schedules", {})
    return schedules.get(module_name, default)


def get_data_path() -> Path:
    data_dir = get_config_value("data_dir", "data")
    path = Path(data_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_logs_path() -> Path:
    logs_dir = get_config_value("logs_dir", "logs")
    path = Path(logs_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path
