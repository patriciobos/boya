from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import pytest

from modules.mpu6050_LL import MPU6050LowLevel


class DummySMBus:
    def __init__(self, busnum: int) -> None:
        self.busnum = busnum
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def read_byte_data(self, address: int, reg: int) -> int:
        if address == 0x68 and reg == 0x75:
            return 0x68
        raise OSError("no device")

    def read_i2c_block_data(self, address: int, reg: int, n: int) -> list[int]:
        raise OSError("no block data")


@pytest.fixture(autouse=True)
def patch_smbus2(monkeypatch):
    import modules.mpu6050_LL as mpu6050_LL

    class FakeSMBus:
        def __init__(self, busnum: int) -> None:
            self.inner = DummySMBus(busnum)

        def close(self) -> None:
            self.inner.close()

        def read_byte_data(self, address: int, reg: int) -> int:
            return self.inner.read_byte_data(address, reg)

        def read_i2c_block_data(self, address: int, reg: int, n: int) -> list[int]:
            return self.inner.read_i2c_block_data(address, reg, n)

    monkeypatch.setattr(mpu6050_LL, "SMBus", FakeSMBus)
    yield


def test_mpu6050_probe_opens_forced_bus(monkeypatch):
    ll = MPU6050LowLevel()
    assert ll.init(bus=3, address=0x68) is True
    assert ll.probe() is True
    assert ll.bus_num == 3
    assert ll.is_open is False


def test_mpu6050_probe_falls_back_when_no_bus_open(monkeypatch):
    ll = MPU6050LowLevel()
    assert ll.init(bus=3, address=0x68) is True
    ll.bus_candidates = [3]
    assert ll.probe() is True
    assert ll.bus_num == 3
    assert ll.is_open is False


def test_mpu6050_probe_current_bus_requires_whoami(monkeypatch):
    import modules.mpu6050_LL as mpu6050_LL

    class FakeSMBus2:
        def __init__(self, busnum: int) -> None:
            self.busnum = busnum

        def close(self) -> None:
            pass

        def read_byte_data(self, address: int, reg: int) -> int:
            if reg == mpu6050_LL.MPU6050LowLevel.REG_WHO_AM_I:
                raise OSError("no device")
            raise OSError("unexpected")

        def read_i2c_block_data(self, address: int, reg: int, n: int) -> list[int]:
            return [0] * n

    monkeypatch.setattr(mpu6050_LL, "SMBus", FakeSMBus2)
    ll = MPU6050LowLevel()
    assert ll.init(bus=12, address=0x68) is True
    ll.bus = FakeSMBus2(12)
    ll.bus_num = 12
    ll.is_open = True

    present, errors, details = ll._probe_current_bus()

    assert present is False
    assert any(
        check["name"] == "whoami" and check["ok"] is False
        for check in details["checks"]
    )
    assert any(check["name"] == "read_all_raw" for check in details["checks"])


def test_mpu6050_full_test_scans_all_buses_when_default_fails(monkeypatch):
    import modules.mpu6050_LL as mpu6050_LL

    class FakeSMBus3:
        def __init__(self, busnum: int) -> None:
            self.busnum = busnum

        def close(self) -> None:
            pass

        def read_byte_data(self, address: int, reg: int) -> int:
            if reg == mpu6050_LL.MPU6050LowLevel.REG_WHO_AM_I:
                if self.busnum == 1 and address == 0x68:
                    return 0x68
                raise OSError("no device")
            return 0

        def read_i2c_block_data(self, address: int, reg: int, n: int) -> list[int]:
            if self.busnum == 1 and address == 0x68 and reg == mpu6050_LL.MPU6050LowLevel.REG_ACCEL_XOUT_H:
                return [0] * 14
            raise OSError("no block data")

    monkeypatch.setattr(mpu6050_LL, "SMBus", FakeSMBus3)
    ll = MPU6050LowLevel()
    assert ll.init(bus=None, address=0x68) is True
    ll.bus_candidates = [12, 1]

    found, _, details = ll._scan_for_device()

    assert found is True
    assert details["selected_bus"] == 1
    assert details["selected_address"] == "0x68"


def test_mpu6050_full_test_falls_back_to_bus_candidates(monkeypatch):
    import modules.mpu6050_LL as mpu6050_LL

    class FakeSMBus3:
        def __init__(self, busnum: int) -> None:
            self.busnum = busnum

        def close(self) -> None:
            pass

        def read_byte_data(self, address: int, reg: int) -> int:
            if reg == mpu6050_LL.MPU6050LowLevel.REG_WHO_AM_I:
                if self.busnum == 1 and address == 0x68:
                    return 0x68
                raise OSError("no device")
            return 0

        def read_i2c_block_data(self, address: int, reg: int, n: int) -> list[int]:
            if self.busnum == 1 and address == 0x68 and reg == mpu6050_LL.MPU6050LowLevel.REG_ACCEL_XOUT_H:
                return [0] * 14
            raise OSError("no block data")

        def write_byte_data(self, address: int, reg: int, value: int) -> None:
            return None

    monkeypatch.setattr(mpu6050_LL, "SMBus", FakeSMBus3)

    def fake_whoami(self) -> int:
        if self.bus_num == 1 and self.address == 0x68:
            return 0x68
        raise OSError("no device")

    monkeypatch.setattr(mpu6050_LL.MPU6050LowLevel, "whoami", fake_whoami)
    monkeypatch.setattr(
        mpu6050_LL.MPU6050LowLevel,
        "read_all_raw",
        lambda self: {"ax": 0, "ay": 0, "az": 0, "temp_raw": 0, "gx": 0, "gy": 0, "gz": 0},
    )
    monkeypatch.setattr(mpu6050_LL.MPU6050LowLevel, "reset", lambda self: None)
    monkeypatch.setattr(mpu6050_LL.MPU6050LowLevel, "configure_default", lambda self: None)
    monkeypatch.setattr(mpu6050_LL.MPU6050LowLevel, "read_int_status", lambda self: 0x01)
    monkeypatch.setattr(
        mpu6050_LL.MPU6050LowLevel,
        "read_all",
        lambda self: {
            "ax_g": 0.0,
            "ay_g": 0.0,
            "az_g": 0.0,
            "temp_c": 0.0,
            "gx_dps": 0.0,
            "gy_dps": 0.0,
            "gz_dps": 0.0,
        },
    )
    monkeypatch.setattr(
        mpu6050_LL.MPU6050LowLevel,
        "read_all_corrected",
        lambda self: {
            "ax_g": 0.0,
            "ay_g": 0.0,
            "az_g": 0.0,
            "temp_c": 0.0,
            "gx_dps": 0.0,
            "gy_dps": 0.0,
            "gz_dps": 0.0,
        },
    )
    monkeypatch.setattr(
        mpu6050_LL.MPU6050LowLevel,
        "read_tilt_deg_corrected",
        lambda self: (0.0, 0.0),
    )
    monkeypatch.setattr(
        mpu6050_LL.MPU6050LowLevel,
        "get_accel_offsets",
        lambda self: (0.0, 0.0, 0.0),
    )
    monkeypatch.setattr(
        mpu6050_LL.MPU6050LowLevel,
        "get_gyro_offsets",
        lambda self: (0.0, 0.0, 0.0),
    )

    ll = MPU6050LowLevel()
    assert ll.init(bus=None, address=0x68) is True
    ll.bus_candidates = [12, 1]

    ok, details = ll.full_test()

    assert ok is True
    assert details["device_present"] is True
    assert details["details"]["scan"]["fallback_scan"]["selected_bus"] == 1
    assert details["details"]["scan"]["fallback_scan"]["selected_address"] == "0x68"
    assert ll.bus_num == 1
