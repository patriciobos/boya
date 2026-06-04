from __future__ import annotations

import os
from typing import Any, Type

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

USE_LL_MOCKS = os.getenv("USE_LL_MOCKS", "0").strip().lower() in ("1", "true", "yes", "on")

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


def is_mock_enabled_for(module_name: str) -> bool:
    module_name = str(module_name).strip()
    if module_name not in _MOCK_ENV_VARS:
        raise ValueError(f"Unknown low-level module name: {module_name}")

    env_name = _MOCK_ENV_VARS[module_name]
    return os.getenv(env_name, "0").strip().lower() in ("1", "true", "yes", "on")


def get_low_level_class(module_name: str) -> Type[Any]:
    module_name = str(module_name).strip()
    if module_name not in _ACTUAL_CLASSES:
        raise ValueError(f"Unknown low-level module name: {module_name}")

    if USE_LL_MOCKS or is_mock_enabled_for(module_name):
        return _MOCK_CLASSES[module_name]

    actual_module_name, actual_class_name = _ACTUAL_CLASSES[module_name]
    module = __import__(f"modules.{actual_module_name}", fromlist=[actual_class_name])
    return getattr(module, actual_class_name)


def is_mock_enabled() -> bool:
    return USE_LL_MOCKS
