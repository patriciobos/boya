from __future__ import annotations

import os
from typing import Any, Type

from modules.support.log_utils import get_logger
from modules.support.system_config import get_configured_mock_modules

from modules.support.ll_mocks import (
    AHT10LowLevelMock,
    AISLowLevelMock,
    AudioProcLowLevelMock,
    BehringerLowLevelMock,
    IridiumLowLevelMock,
    MPU6050LowLevelMock,
    WindsonicLowLevelMock,
    XTRA2210LowLevelMock,
)

def _env_enabled(name: str) -> bool:
    return os.getenv(name, "0").strip().lower() in ("1", "true", "yes", "on")


USE_LL_MOCKS = _env_enabled("USE_LL_MOCKS")

_ACTUAL_CLASSES: dict[str, tuple[str, str]] = {
    "AHT10": ("aht10_LL", "AHT10LowLevel"),
    "AIS": ("ais_LL", "AISLowLevel"),
    "AudioProc": ("audioProc_LL", "AudioProcLowLevel"),
    "Behringer": ("behringer_LL", "BehringerLowLevel"),
    "Iridium": ("iridium_LL", "IridiumLowLevel"),
    "MPU6050": ("mpu6050_LL", "MPU6050LowLevel"),
    "Windsonic": ("windsonic_LL", "WindsonicLowLevel"),
    "XTRA2210": ("xtra2210_LL", "XTRA2210LowLevel"),
}

_MOCK_ENV_VARS: dict[str, str] = {
    module_name: f"USE_MOCK_{module_name.upper()}"
    for module_name in _ACTUAL_CLASSES
}

_MOCK_CLASSES: dict[str, Type[Any]] = {
    "AHT10": AHT10LowLevelMock,
    "AIS": AISLowLevelMock,
    "AudioProc": AudioProcLowLevelMock,
    "Behringer": BehringerLowLevelMock,
    "Iridium": IridiumLowLevelMock,
    "MPU6050": MPU6050LowLevelMock,
    "Windsonic": WindsonicLowLevelMock,
    "XTRA2210": XTRA2210LowLevelMock,
}


def _configured_mock_set() -> set[str]:
    return set(get_configured_mock_modules())


def _env_mock_modules() -> set[str]:
    return {
        module_name
        for module_name, env_name in _MOCK_ENV_VARS.items()
        if _env_enabled(env_name)
    }


def validate_mock_configuration() -> dict[str, Any]:
    config_modules = _configured_mock_set()
    env_modules = _env_mock_modules()
    all_modules = set(_ACTUAL_CLASSES)

    if USE_LL_MOCKS and config_modules and config_modules != all_modules:
        raise RuntimeError(
            "Ambiguous mock configuration: USE_LL_MOCKS enables every module, "
            f"but config mock_modules only lists {sorted(config_modules)}"
        )

    return {
        "all_mock": USE_LL_MOCKS,
        "config_modules": sorted(config_modules),
        "env_modules": sorted(env_modules),
        "mock_modules": sorted(all_modules if USE_LL_MOCKS else config_modules | env_modules),
    }


def mock_source_for(module_name: str) -> str | None:
    module_name = str(module_name).strip()
    if module_name not in _MOCK_ENV_VARS:
        raise ValueError(f"Unknown low-level module name: {module_name}")

    config_enabled = module_name in _configured_mock_set()
    env_enabled = _env_enabled(_MOCK_ENV_VARS[module_name])
    if USE_LL_MOCKS:
        return "env:USE_LL_MOCKS"
    if config_enabled and env_enabled:
        return "config+env"
    if config_enabled:
        return "config"
    if env_enabled:
        return f"env:{_MOCK_ENV_VARS[module_name]}"
    return None


def get_mocked_module_names() -> list[str]:
    return validate_mock_configuration()["mock_modules"]


def is_mock_enabled_for(module_name: str) -> bool:
    return mock_source_for(module_name) is not None


def get_low_level_class(module_name: str) -> Type[Any]:
    module_name = str(module_name).strip()
    if module_name not in _ACTUAL_CLASSES:
        raise ValueError(f"Unknown low-level module name: {module_name}")

    mock_source = mock_source_for(module_name)
    if mock_source is not None:
        get_logger("ll_factory").warning(
            "%s low-level is running in mock mode (source=%s)",
            module_name,
            mock_source,
        )
        return _MOCK_CLASSES[module_name]

    actual_module_name, actual_class_name = _ACTUAL_CLASSES[module_name]
    module = __import__(f"modules.{actual_module_name}", fromlist=[actual_class_name])
    return getattr(module, actual_class_name)


def is_mock_enabled() -> bool:
    return bool(get_mocked_module_names())
