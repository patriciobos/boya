from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIGS_PATH = PROJECT_ROOT / "configs"
CONFIG_PATH = CONFIGS_PATH / "config.json"
SCHEDULER_PATH = PROJECT_ROOT / "scheduler.json"
UTC_MINUS_3_LABEL = "UTC-3"
UTC_MINUS_3 = timezone(timedelta(hours=-3), UTC_MINUS_3_LABEL)

MODULE_NAMES = (
    "AHT10",
    "AIS",
    "AudioProc",
    "Behringer",
    "Iridium",
    "MPU6050",
    "Windsonic",
    "XTRA2210",
)

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


def now_utc_minus_3() -> datetime:
    return datetime.now(UTC_MINUS_3)


def utc_minus_3_timestamp() -> str:
    return now_utc_minus_3().replace(microsecond=0).isoformat()


def compact_utc_minus_3_timestamp() -> str:
    return now_utc_minus_3().strftime("%Y%m%d_%H%M%S")


def normalize_module_names(value: Any, field_name: str = "modules") -> list[str]:
    if value in (None, "", False):
        return []
    if isinstance(value, str):
        raw_names = [part.strip() for part in value.split(",")]
    elif isinstance(value, list):
        raw_names = [str(part).strip() for part in value]
    else:
        raise RuntimeError(f"Invalid {field_name}: expected list or comma-separated string")

    canonical = {name.lower(): name for name in MODULE_NAMES}
    normalized: list[str] = []
    unknown: list[str] = []
    for raw_name in raw_names:
        if not raw_name:
            continue
        name = canonical.get(raw_name.lower())
        if name is None:
            unknown.append(raw_name)
            continue
        if name not in normalized:
            normalized.append(name)

    if unknown:
        raise RuntimeError(
            f"Invalid {field_name}: unknown module(s) {unknown}. Allowed modules: {list(MODULE_NAMES)}"
        )

    return normalized


def get_configured_mock_modules() -> list[str]:
    return normalize_module_names(get_config_value("mock_modules", []), "mock_modules")


def get_schedule(module_name: str, default: Any = None) -> Any:
    # Prefer scheduler.json if present (allows separate schedule management)
    try:
        if SCHEDULER_PATH.exists():
            with open(SCHEDULER_PATH, "r", encoding="utf-8") as handle:
                sched_conf = json.load(handle)
            schedules = sched_conf.get("schedules", {})
            if module_name in schedules:
                return schedules.get(module_name)
    except Exception:
        pass

    schedules = get_config_value("schedules", {})
    return schedules.get(module_name, default)


def get_configs_path() -> Path:
    return CONFIGS_PATH


def get_data_path() -> Path:
    data_dir = get_config_value("data_dir", "data")
    path = Path(data_dir)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_logs_path() -> Path:
    logs_dir = get_config_value("logs_dir", "logs")
    path = Path(logs_dir)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path
