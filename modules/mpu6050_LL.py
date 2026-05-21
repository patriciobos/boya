from __future__ import annotations

"""MPU-6050 / GY-521 low-level driver (lifecycle-uniformized).

This refactor keeps the device logic intact while aligning the public
low-level lifecycle with the agreed stage-1 contract:
- init() prepares configuration only
- open() opens the selected/current I2C bus only
- test() performs a quick presence/communication check and restores state
- full_test() performs a richer diagnosis and captures all exceptions
- close() is idempotent
- deinit() leaves the driver in a neutral state
"""

import math
import time
from typing import Any, Optional, Tuple, TYPE_CHECKING, cast

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.support.i2c_common import discover_i2c_buses
from modules.support.log_utils import get_logger


if TYPE_CHECKING:
    from smbus2 import SMBus as SMBusType
else:
    SMBusType = Any

try:
    from smbus2 import SMBus
except Exception:  # pragma: no cover
    SMBus = None


class MPU6050Error(Exception):
    pass


class NotFound(MPU6050Error):
    pass


class I2CError(MPU6050Error):
    pass


class ProtocolError(MPU6050Error):
    pass


class CalibrationError(MPU6050Error):
    pass


class MPU6050LowLevel:
    DEFAULT_ADDRESS = 0x68
    ALT_ADDRESS = 0x69
    DEFAULT_BUS = 1

    REG_SMPLRT_DIV = 0x19
    REG_CONFIG = 0x1A
    REG_GYRO_CONFIG = 0x1B
    REG_ACCEL_CONFIG = 0x1C
    REG_FIFO_EN = 0x23
    REG_INT_PIN_CFG = 0x37
    REG_INT_ENABLE = 0x38
    REG_INT_STATUS = 0x3A

    REG_ACCEL_XOUT_H = 0x3B
    REG_TEMP_OUT_H = 0x41
    REG_GYRO_XOUT_H = 0x43

    REG_USER_CTRL = 0x6A
    REG_PWR_MGMT_1 = 0x6B
    REG_PWR_MGMT_2 = 0x6C
    REG_FIFO_COUNTH = 0x72
    REG_FIFO_COUNTL = 0x73
    REG_FIFO_R_W = 0x74
    REG_WHO_AM_I = 0x75

    DEVICE_ID_68 = 0x68
    DEVICE_ID_69 = 0x69

    CLOCK_INTERNAL = 0
    CLOCK_PLL_XGYRO = 1
    CLOCK_PLL_YGYRO = 2
    CLOCK_PLL_ZGYRO = 3

    ACCEL_FS_2G = 0
    ACCEL_FS_4G = 1
    ACCEL_FS_8G = 2
    ACCEL_FS_16G = 3

    GYRO_FS_250 = 0
    GYRO_FS_500 = 1
    GYRO_FS_1000 = 2
    GYRO_FS_2000 = 3

    def __init__(self, logger_name: str = "mpu6050_LL") -> None:
        self.logger = get_logger(logger_name)

        self.is_initialized: bool = False
        self.is_open: bool = False
        self.last_error: Optional[str] = None

        self.bus: Optional[SMBusType] = None
        self.bus_num: Optional[int] = None
        self.address: int = self.DEFAULT_ADDRESS
        self.bus_candidates: list[int] = []
        self.bus_forced: bool = False

        self.accel_offsets_g: Tuple[float, float, float] = (0.0, 0.0, 0.0)
        self.gyro_offsets_dps: Tuple[float, float, float] = (0.0, 0.0, 0.0)

    def _set_error(self, message: str) -> None:
        self.last_error = message

    def _clear_error(self) -> None:
        self.last_error = None

    def _require_i2c(self) -> None:
        if SMBus is None:
            raise NotFound("smbus2 is required for MPU6050 I2C operations")

    def _require_initialized(self) -> None:
        if not self.is_initialized:
            raise I2CError("Module is not initialized. Call init() first.")

    def _require_bus(self) -> SMBusType:
        self._require_i2c()
        self._require_initialized()
        if self.bus is None or not self.is_open:
            raise I2CError("Bus is not open. Call open() first.")
        return self.bus

    def _resolve_bus_candidates(self, bus: Optional[int]) -> list[int]:
        preferred = int(bus) if bus is not None else self.DEFAULT_BUS
        return list(discover_i2c_buses(preferred))
    
    def _open_bus(self, busnum: int) -> SMBusType:
        self._require_i2c()
        SMBusCls = SMBus
        if SMBusCls is None:
            raise NotFound("smbus2 is required for MPU6050 I2C operations")
        return cast(SMBusType, SMBusCls(busnum))

    def _close_transport(self) -> None:
        if self.bus is not None:
            self.bus.close()
        self.bus = None

    def _candidate_addresses(self) -> list[int]:
        if self.address == self.ALT_ADDRESS:
            return [self.ALT_ADDRESS, self.DEFAULT_ADDRESS]
        return [self.DEFAULT_ADDRESS, self.ALT_ADDRESS]

    def _probe_current_bus(self) -> tuple[bool, list[str], dict[str, Any]]:
        errors: list[str] = []
        details: dict[str, Any] = {
            "bus": self.bus_num,
            "address": f"0x{self.address:02X}",
            "checks": [],
        }

        try:
            who = self.whoami()
            ok = who in (self.DEVICE_ID_68, self.DEVICE_ID_69)
            details["checks"].append(
                {"name": "whoami", "ok": ok, "value": f"0x{who:02X}"}
            )
            if ok:
                return True, errors, details
            errors.append(f"Unexpected WHO_AM_I value: 0x{who:02X}")
        except Exception as exc:
            errors.append(f"whoami failed: {exc}")
            details["checks"].append({"name": "whoami", "ok": False, "error": str(exc)})

        try:
            raw = self.read_all_raw()
            details["checks"].append({"name": "read_all_raw", "ok": True, "raw": raw})
            return True, errors, details
        except Exception as exc:
            errors.append(f"read_all_raw failed: {exc}")
            details["checks"].append({"name": "read_all_raw", "ok": False, "error": str(exc)})

        return False, errors, details

    def _scan_for_device(self) -> tuple[bool, list[str], dict[str, Any]]:
        self._require_initialized()

        scan_details: dict[str, Any] = {
            "requested_address": f"0x{self.address:02X}",
            "bus_forced": self.bus_forced,
            "scanned_buses": [],
            "selected_bus": None,
            "selected_address": None,
        }
        errors: list[str] = []

        original_bus = self.bus_num
        original_address = self.address
        original_was_open = self.is_open and self.bus is not None

        if original_was_open:
            self.close()

        candidates = [self.bus_num] if self.bus_forced and self.bus_num is not None else list(self.bus_candidates)
        if not candidates:
            candidates = self._resolve_bus_candidates(self.bus_num if self.bus_forced else None)

        for busnum in candidates:
            bus_entry: dict[str, Any] = {"bus": busnum, "addresses": []}
            scan_details["scanned_buses"].append(bus_entry)

            for address in self._candidate_addresses():
                address_entry: dict[str, Any] = {"address": f"0x{address:02X}"}
                bus_entry["addresses"].append(address_entry)

                try:
                    self.bus = self._open_bus(busnum)
                    self.bus_num = busnum
                    self.address = address
                    self.is_open = True
                    address_entry["open_ok"] = True

                    present, probe_errors, probe_details = self._probe_current_bus()
                    address_entry["probe"] = probe_details
                    errors.extend([f"bus {busnum} addr 0x{address:02X}: {e}" for e in probe_errors])

                    if present:
                        scan_details["selected_bus"] = busnum
                        scan_details["selected_address"] = f"0x{address:02X}"
                        return True, errors, scan_details
                except Exception as exc:
                    address_entry["open_ok"] = False
                    address_entry["error"] = str(exc)
                    errors.append(f"bus {busnum} addr 0x{address:02X}: open/probe failed: {exc}")
                finally:
                    if self.bus is not None:
                        try:
                            self.bus.close()
                        except Exception:
                            pass
                    self.bus = None
                    self.is_open = False

        self.bus_num = original_bus
        self.address = original_address
        if original_was_open and original_bus is not None:
            self.open()

        return False, errors, scan_details

    def init(self, bus: Optional[int] = None, address: int = DEFAULT_ADDRESS) -> bool:
        """Prepare configuration and internal state only."""
        self.logger.info("Initializing module")
        self._clear_error()

        try:
            self._require_i2c()
            self.close()

            self.address = int(address)
            self.bus_forced = bus is not None
            self.bus_num = int(bus) if bus is not None else None
            self.bus_candidates = self._resolve_bus_candidates(bus)
            self.is_initialized = True

            self.logger.info(
                "Module initialized: address=0x%02X bus_num=%s bus_forced=%s bus_candidates=%s",
                self.address,
                self.bus_num,
                self.bus_forced,
                self.bus_candidates,
            )
            return True
        except Exception as exc:
            self.is_initialized = False
            self._set_error(f"Initialization failed: {exc}")
            self.logger.exception("Initialization failed: %s", exc)
            return False

    def open(self) -> bool:
        """Open the I2C bus only. Does not fully validate the device."""
        self.logger.info("Opening I2C bus")
        self._clear_error()

        if not self.is_initialized:
            self._set_error("Module is not initialized")
            self.logger.error(self.last_error)
            return False

        if self.is_open and self.bus is not None:
            self.logger.info("I2C bus already open on bus %s", self.bus_num)
            return True

        candidates = [self.bus_num] if self.bus_forced and self.bus_num is not None else list(self.bus_candidates)
        last_exc: Optional[Exception] = None

        for busnum in candidates:
            try:
                self.logger.info("Trying I2C bus %s for MPU6050@0x%02X", busnum, self.address)
                self.bus = self._open_bus(busnum)
                self.bus_num = busnum
                self.is_open = True
                self.logger.info("Opened I2C bus %s", busnum)
                return True
            except Exception as exc:
                last_exc = exc
                self.logger.debug("Failed to open I2C bus %s: %s", busnum, exc)

        self.bus = None
        self.is_open = False
        self.bus_num = int(self.bus_num) if self.bus_forced and self.bus_num is not None else None
        message = f"Open failed: {last_exc}" if last_exc else "Open failed"
        self._set_error(message)
        self.logger.error(message)
        return False

    def close(self) -> bool:
        """Close the I2C bus. Idempotent."""
        self.logger.info("Closing I2C bus")
        self._clear_error()

        try:
            if self.bus is not None:
                self._close_transport()
            self.bus = None
            self.is_open = False
            self.logger.info("I2C bus closed")
            return True
        except Exception as exc:
            self.bus = None
            self.is_open = False
            self._set_error(f"Close failed: {exc}")
            self.logger.exception("Close failed: %s", exc)
            return False

    def probe(self) -> bool:
        """Public probe against the currently-open bus only."""
        try:
            present, _, _ = self._probe_current_bus()
            return present
        except Exception as exc:
            self.logger.debug("Probe failed on bus %s: %s", self.bus_num, exc)
            return False

    def test(self) -> bool:
        """Run a basic presence/communication smoke test and restore prior state."""
        self.logger.info("Running basic test")
        self._clear_error()

        if not self.is_initialized:
            self._set_error("Module is not initialized")
            self.logger.error(self.last_error)
            return False

        original_bus_num = self.bus_num
        original_address = self.address
        original_was_open = self.is_open and self.bus is not None

        try:
            if not original_was_open:
                if not self.open():
                    return False

            present, errors, _ = self._probe_current_bus()
            if present:
                return True

            if not self.bus_forced:
                present, scan_errors, scan_details = self._scan_for_device()
                errors.extend(scan_errors)
                if present:
                    self.logger.info("Device detected during fallback scan: %s", scan_details)
                    return True

            if errors:
                self._set_error("; ".join(errors[-3:]))
            return False
        except Exception as exc:
            self._set_error(f"Test failed: {exc}")
            self.logger.exception("Test failed: %s", exc)
            return False
        finally:
            if original_was_open:
                if not self.is_open:
                    self.bus_num = original_bus_num
                    self.address = original_address
                    self.open()
                else:
                    self.bus_num = original_bus_num
                    self.address = original_address
            else:
                self.close()
                self.bus_num = original_bus_num
                self.address = original_address

    def full_test(self) -> tuple[bool, dict]:
        """Run a complete diagnostic test and return structured results."""
        self.logger.info("Running full test")
        self._clear_error()

        details: dict[str, Any] = {
            "initialized": self.is_initialized,
            "opened": self.is_open,
            "device_present": False,
            "errors": [],
            "details": {
                "bus_num": self.bus_num,
                "address": f"0x{self.address:02X}",
                "scan": {},
                "who_am_i": None,
                "configured": False,
                "int_status": None,
                "data_ready": None,
                "fifo_overflow": None,
                "raw": None,
                "parsed": None,
                "corrected": None,
                "tilt_deg": None,
                "offsets": {
                    "accel_g": self.get_accel_offsets(),
                    "gyro_dps": self.get_gyro_offsets(),
                },
            },
        }

        original_bus_num = self.bus_num
        original_address = self.address
        original_was_open = self.is_open and self.bus is not None

        try:
            if not self.is_initialized:
                msg = "Module is not initialized"
                details["errors"].append(msg)
                self._set_error(msg)
                return False, details

            present = False
            scan_errors: list[str] = []
            scan_details: dict[str, Any] = {}

            if self.is_open and self.bus is not None:
                present, probe_errors, probe_details = self._probe_current_bus()
                scan_errors.extend(probe_errors)
                scan_details = {"current_bus_probe": probe_details}
            else:
                if self.open():
                    details["opened"] = True
                    present, probe_errors, probe_details = self._probe_current_bus()
                    scan_errors.extend(probe_errors)
                    scan_details = {"current_bus_probe": probe_details}
                else:
                    scan_errors.append(self.last_error or "Failed to open bus")

            if not present and not self.bus_forced:
                present, fallback_errors, fallback_details = self._scan_for_device()
                scan_errors.extend(fallback_errors)
                scan_details["fallback_scan"] = fallback_details
                if present:
                    self.close()
                    if not self.open():
                        scan_errors.append(self.last_error or "Failed to reopen selected bus after scan")
                        present = False

            details["opened"] = self.is_open
            details["device_present"] = present
            details["details"]["scan"] = scan_details
            details["errors"].extend(scan_errors)

            if not present:
                msg = "Basic probe failed"
                details["errors"].append(msg)
                self._set_error(msg)
                return False, details

            who = self.whoami()
            details["details"]["bus_num"] = self.bus_num
            details["details"]["address"] = f"0x{self.address:02X}"
            details["details"]["who_am_i"] = f"0x{who:02X}"
            if who not in (self.DEVICE_ID_68, self.DEVICE_ID_69):
                details["errors"].append(f"Unexpected WHO_AM_I value: 0x{who:02X}")
                return False, details

            self.reset()
            self.configure_default()
            details["details"]["configured"] = True

            int_status = self.read_int_status()
            details["details"]["int_status"] = f"0x{int_status:02X}"
            details["details"]["data_ready"] = self.is_data_ready(int_status)
            details["details"]["fifo_overflow"] = self.is_fifo_overflow(int_status)

            raw = self.read_all_raw()
            parsed = self.read_all()
            corrected = self.read_all_corrected()
            tilt = self.read_tilt_deg_corrected()

            details["details"]["raw"] = raw
            details["details"]["parsed"] = parsed
            details["details"]["corrected"] = corrected
            details["details"]["tilt_deg"] = {
                "roll": tilt[0],
                "pitch": tilt[1],
            }
            details["details"]["offsets"] = {
                "accel_g": self.get_accel_offsets(),
                "gyro_dps": self.get_gyro_offsets(),
            }

            plausible = (
                -16.5 <= parsed["ax_g"] <= 16.5
                and -16.5 <= parsed["ay_g"] <= 16.5
                and -16.5 <= parsed["az_g"] <= 16.5
                and -260.0 <= parsed["temp_c"] <= 150.0
            )
            details["details"]["plausible"] = plausible
            if not plausible:
                details["errors"].append("Measured values are outside plausible MPU6050 ranges")
                return False, details

            success = (
                details["initialized"]
                and details["device_present"]
                and not details["errors"]
            )
            return success, details
        except Exception as exc:
            details["errors"].append(str(exc))
            self._set_error(str(exc))
            self.logger.exception("Full test failed: %s", exc)
            return False, details
        finally:
            if original_was_open:
                if not self.is_open:
                    self.bus_num = original_bus_num
                    self.address = original_address
                    self.open()
                else:
                    self.bus_num = original_bus_num
                    self.address = original_address
            else:
                self.close()
                self.bus_num = original_bus_num
                self.address = original_address
            details["initialized"] = self.is_initialized
            details["opened"] = self.is_open

    def deinit(self) -> bool:
        """Release all resources and reset lifecycle state."""
        self.logger.info("Deinitializing module")
        self._clear_error()

        try:
            self.close()
            self.bus = None
            self.bus_num = None
            self.address = self.DEFAULT_ADDRESS
            self.bus_candidates = []
            self.bus_forced = False
            self.is_initialized = False
            self.is_open = False
            return True
        except Exception as exc:
            self._set_error(f"Deinitialization failed: {exc}")
            self.logger.exception("Deinitialization failed: %s", exc)
            return False

    def read_reg(self, reg: int) -> int:
        bus = self._require_bus()
        try:
            return int(bus.read_byte_data(self.address, reg)) & 0xFF
        except OSError as exc:
            raise I2CError(f"I2C error during read_reg(0x{reg:02X}): {exc}") from exc

    def write_reg(self, reg: int, value: int) -> None:
        bus = self._require_bus()
        try:
            bus.write_byte_data(self.address, reg, value & 0xFF)
        except OSError as exc:
            raise I2CError(f"I2C error during write_reg(0x{reg:02X}, 0x{value:02X}): {exc}") from exc

    def read_block(self, reg: int, n: int) -> bytes:
        bus = self._require_bus()
        try:
            data = bus.read_i2c_block_data(self.address, reg, n)
            return bytes(data)
        except OSError as exc:
            raise I2CError(f"I2C error during read_block(0x{reg:02X}, {n}): {exc}") from exc

    def whoami(self) -> int:
        return self.read_reg(self.REG_WHO_AM_I)

    def reset(self) -> None:
        self.write_reg(self.REG_PWR_MGMT_1, 0x80)
        time.sleep(0.10)

    def wake(self) -> None:
        self.write_reg(self.REG_PWR_MGMT_1, 0x01)
        time.sleep(0.05)

    def sleep(self) -> None:
        val = self.read_reg(self.REG_PWR_MGMT_1)
        self.write_reg(self.REG_PWR_MGMT_1, val | 0x40)
        time.sleep(0.01)

    def set_clock_source(self, source: int = CLOCK_PLL_XGYRO) -> None:
        if source < 0 or source > 7:
            raise ProtocolError(f"Invalid clock source: {source}")
        val = self.read_reg(self.REG_PWR_MGMT_1)
        val = (val & 0xF8) | (source & 0x07)
        self.write_reg(self.REG_PWR_MGMT_1, val)

    def set_dlpf(self, cfg: int) -> None:
        if cfg < 0 or cfg > 7:
            raise ProtocolError(f"Invalid DLPF cfg: {cfg}")
        val = self.read_reg(self.REG_CONFIG)
        val = (val & 0xF8) | (cfg & 0x07)
        self.write_reg(self.REG_CONFIG, val)

    def set_sample_rate_div(self, div: int) -> None:
        if div < 0 or div > 255:
            raise ProtocolError(f"Invalid sample rate divider: {div}")
        self.write_reg(self.REG_SMPLRT_DIV, div & 0xFF)

    def set_accel_range(self, fs_sel: int) -> None:
        if fs_sel < 0 or fs_sel > 3:
            raise ProtocolError(f"Invalid accel range: {fs_sel}")
        val = self.read_reg(self.REG_ACCEL_CONFIG)
        val = (val & ~0x18) | ((fs_sel & 0x03) << 3)
        self.write_reg(self.REG_ACCEL_CONFIG, val)

    def get_accel_range(self) -> int:
        val = self.read_reg(self.REG_ACCEL_CONFIG)
        return (val >> 3) & 0x03

    def set_gyro_range(self, fs_sel: int) -> None:
        if fs_sel < 0 or fs_sel > 3:
            raise ProtocolError(f"Invalid gyro range: {fs_sel}")
        val = self.read_reg(self.REG_GYRO_CONFIG)
        val = (val & ~0x18) | ((fs_sel & 0x03) << 3)
        self.write_reg(self.REG_GYRO_CONFIG, val)

    def get_gyro_range(self) -> int:
        val = self.read_reg(self.REG_GYRO_CONFIG)
        return (val >> 3) & 0x03

    def configure_default(self) -> None:
        self.wake()
        self.set_clock_source(self.CLOCK_PLL_XGYRO)
        self.set_dlpf(3)
        self.set_sample_rate_div(9)
        self.set_accel_range(self.ACCEL_FS_2G)
        self.set_gyro_range(self.GYRO_FS_250)
        self.disable_interrupts()

    @staticmethod
    def _to_int16(msb: int, lsb: int) -> int:
        value = ((msb & 0xFF) << 8) | (lsb & 0xFF)
        if value & 0x8000:
            value -= 0x10000
        return value

    def read_accel_raw(self) -> Tuple[int, int, int]:
        data = self.read_block(self.REG_ACCEL_XOUT_H, 6)
        if len(data) != 6:
            raise ProtocolError(f"Expected 6 accel bytes, got {data!r}")
        return (
            self._to_int16(data[0], data[1]),
            self._to_int16(data[2], data[3]),
            self._to_int16(data[4], data[5]),
        )

    def read_temp_raw(self) -> int:
        data = self.read_block(self.REG_TEMP_OUT_H, 2)
        if len(data) != 2:
            raise ProtocolError(f"Expected 2 temp bytes, got {data!r}")
        return self._to_int16(data[0], data[1])

    def read_gyro_raw(self) -> Tuple[int, int, int]:
        data = self.read_block(self.REG_GYRO_XOUT_H, 6)
        if len(data) != 6:
            raise ProtocolError(f"Expected 6 gyro bytes, got {data!r}")
        return (
            self._to_int16(data[0], data[1]),
            self._to_int16(data[2], data[3]),
            self._to_int16(data[4], data[5]),
        )

    def read_motion6_raw(self) -> Tuple[int, int, int, int, int, int]:
        data = self.read_block(self.REG_ACCEL_XOUT_H, 14)
        if len(data) != 14:
            raise ProtocolError(f"Expected 14 bytes, got {data!r}")

        return (
            self._to_int16(data[0], data[1]),
            self._to_int16(data[2], data[3]),
            self._to_int16(data[4], data[5]),
            self._to_int16(data[8], data[9]),
            self._to_int16(data[10], data[11]),
            self._to_int16(data[12], data[13]),
        )

    def read_all_raw(self) -> dict:
        data = self.read_block(self.REG_ACCEL_XOUT_H, 14)
        if len(data) != 14:
            raise ProtocolError(f"Expected 14 bytes, got {data!r}")

        return {
            "ax": self._to_int16(data[0], data[1]),
            "ay": self._to_int16(data[2], data[3]),
            "az": self._to_int16(data[4], data[5]),
            "temp_raw": self._to_int16(data[6], data[7]),
            "gx": self._to_int16(data[8], data[9]),
            "gy": self._to_int16(data[10], data[11]),
            "gz": self._to_int16(data[12], data[13]),
        }

    def accel_scale_g_per_lsb(self) -> float:
        fs = self.get_accel_range()
        if fs == self.ACCEL_FS_2G:
            return 1.0 / 16384.0
        if fs == self.ACCEL_FS_4G:
            return 1.0 / 8192.0
        if fs == self.ACCEL_FS_8G:
            return 1.0 / 4096.0
        if fs == self.ACCEL_FS_16G:
            return 1.0 / 2048.0
        raise ProtocolError(f"Unexpected accel FS_SEL={fs}")

    def gyro_scale_dps_per_lsb(self) -> float:
        fs = self.get_gyro_range()
        if fs == self.GYRO_FS_250:
            return 1.0 / 131.0
        if fs == self.GYRO_FS_500:
            return 1.0 / 65.5
        if fs == self.GYRO_FS_1000:
            return 1.0 / 32.8
        if fs == self.GYRO_FS_2000:
            return 1.0 / 16.4
        raise ProtocolError(f"Unexpected gyro FS_SEL={fs}")

    def parse_accel(self, raw: Tuple[int, int, int]) -> Tuple[float, float, float]:
        scale = self.accel_scale_g_per_lsb()
        ax, ay, az = raw
        return ax * scale, ay * scale, az * scale

    def parse_gyro(self, raw: Tuple[int, int, int]) -> Tuple[float, float, float]:
        scale = self.gyro_scale_dps_per_lsb()
        gx, gy, gz = raw
        return gx * scale, gy * scale, gz * scale

    def parse_temp(self, raw: int) -> float:
        return (raw / 340.0) + 36.53

    def read_accel_g(self) -> Tuple[float, float, float]:
        return self.parse_accel(self.read_accel_raw())

    def read_gyro_dps(self) -> Tuple[float, float, float]:
        return self.parse_gyro(self.read_gyro_raw())

    def read_temp_c(self) -> float:
        return self.parse_temp(self.read_temp_raw())

    def read_motion6(self) -> Tuple[float, float, float, float, float, float]:
        ax, ay, az, gx, gy, gz = self.read_motion6_raw()
        ax_g, ay_g, az_g = self.parse_accel((ax, ay, az))
        gx_dps, gy_dps, gz_dps = self.parse_gyro((gx, gy, gz))
        return ax_g, ay_g, az_g, gx_dps, gy_dps, gz_dps

    def read_all(self) -> dict:
        raw = self.read_all_raw()
        ax_g, ay_g, az_g = self.parse_accel((raw["ax"], raw["ay"], raw["az"]))
        gx_dps, gy_dps, gz_dps = self.parse_gyro((raw["gx"], raw["gy"], raw["gz"]))
        temp_c = self.parse_temp(raw["temp_raw"])
        return {
            "ax_g": ax_g,
            "ay_g": ay_g,
            "az_g": az_g,
            "temp_c": temp_c,
            "gx_dps": gx_dps,
            "gy_dps": gy_dps,
            "gz_dps": gz_dps,
        }

    def read_int_status(self) -> int:
        return self.read_reg(self.REG_INT_STATUS)

    def is_data_ready(self, status: Optional[int] = None) -> bool:
        if status is None:
            status = self.read_int_status()
        return bool(status & 0x01)

    def is_fifo_overflow(self, status: Optional[int] = None) -> bool:
        if status is None:
            status = self.read_int_status()
        return bool(status & 0x10)

    def enable_data_ready_interrupt(self) -> None:
        self.write_reg(self.REG_INT_ENABLE, 0x01)

    def disable_interrupts(self) -> None:
        self.write_reg(self.REG_INT_ENABLE, 0x00)

    def set_accel_offsets(self, x: float, y: float, z: float) -> None:
        self.accel_offsets_g = (float(x), float(y), float(z))

    def get_accel_offsets(self) -> Tuple[float, float, float]:
        return self.accel_offsets_g

    def clear_accel_offsets(self) -> None:
        self.accel_offsets_g = (0.0, 0.0, 0.0)

    def set_gyro_offsets(self, x: float, y: float, z: float) -> None:
        self.gyro_offsets_dps = (float(x), float(y), float(z))

    def get_gyro_offsets(self) -> Tuple[float, float, float]:
        return self.gyro_offsets_dps

    def clear_gyro_offsets(self) -> None:
        self.gyro_offsets_dps = (0.0, 0.0, 0.0)

    def clear_offsets(self) -> None:
        self.clear_accel_offsets()
        self.clear_gyro_offsets()

    def calibrate_gyro(self, samples: int = 500, delay: float = 0.005) -> Tuple[float, float, float]:
        if samples <= 0:
            raise CalibrationError("samples must be > 0")
        if delay < 0:
            raise CalibrationError("delay must be >= 0")

        sx = 0.0
        sy = 0.0
        sz = 0.0

        self.logger.info("Starting gyro calibration: samples=%s delay=%s", samples, delay)
        for _ in range(samples):
            gx, gy, gz = self.read_gyro_dps()
            sx += gx
            sy += gy
            sz += gz
            if delay > 0:
                time.sleep(delay)

        offsets = (sx / samples, sy / samples, sz / samples)
        self.set_gyro_offsets(*offsets)
        self.logger.info("Gyro calibration complete: offsets_dps=%s", offsets)
        return offsets

    def calibrate_accel(
        self,
        samples: int = 500,
        delay: float = 0.005,
        expected: Tuple[float, float, float] = (0.0, 0.0, 1.0),
    ) -> Tuple[float, float, float]:
        if samples <= 0:
            raise CalibrationError("samples must be > 0")
        if delay < 0:
            raise CalibrationError("delay must be >= 0")

        ex, ey, ez = expected
        sx = 0.0
        sy = 0.0
        sz = 0.0

        self.logger.info(
            "Starting accel calibration: samples=%s delay=%s expected=%s",
            samples,
            delay,
            expected,
        )
        for _ in range(samples):
            ax, ay, az = self.read_accel_g()
            sx += ax
            sy += ay
            sz += az
            if delay > 0:
                time.sleep(delay)

        mx = sx / samples
        my = sy / samples
        mz = sz / samples

        offsets = (mx - ex, my - ey, mz - ez)
        self.set_accel_offsets(*offsets)
        self.logger.info("Accel calibration complete: offsets_g=%s", offsets)
        return offsets

    def read_accel_g_corrected(self) -> Tuple[float, float, float]:
        ax, ay, az = self.read_accel_g()
        ox, oy, oz = self.accel_offsets_g
        return ax - ox, ay - oy, az - oz

    def read_gyro_dps_corrected(self) -> Tuple[float, float, float]:
        gx, gy, gz = self.read_gyro_dps()
        ox, oy, oz = self.gyro_offsets_dps
        return gx - ox, gy - oy, gz - oz

    def read_all_corrected(self) -> dict:
        ax_g, ay_g, az_g = self.read_accel_g_corrected()
        gx_dps, gy_dps, gz_dps = self.read_gyro_dps_corrected()
        temp_c = self.read_temp_c()
        return {
            "ax_g": ax_g,
            "ay_g": ay_g,
            "az_g": az_g,
            "temp_c": temp_c,
            "gx_dps": gx_dps,
            "gy_dps": gy_dps,
            "gz_dps": gz_dps,
        }

    def tilt_from_accel(self, accel_g: Tuple[float, float, float]) -> Tuple[float, float]:
        ax, ay, az = accel_g

        if az < 0.0:
            ay_ref = -ay
            az_ref = -az
        else:
            ay_ref = ay
            az_ref = az

        roll_rad = math.atan2(ay_ref, az_ref)
        pitch_rad = math.atan2(-ax, math.sqrt((ay * ay) + (az * az)))
        return math.degrees(roll_rad), math.degrees(pitch_rad)

    def read_tilt_deg(self) -> Tuple[float, float]:
        return self.tilt_from_accel(self.read_accel_g())

    def read_tilt_deg_corrected(self) -> Tuple[float, float]:
        return self.tilt_from_accel(self.read_accel_g_corrected())

    def read_tilt_deg_avg(self, samples: int = 10, delay: float = 0.02) -> Tuple[float, float]:
        if samples <= 0:
            raise CalibrationError("samples must be > 0")
        if delay < 0:
            raise CalibrationError("delay must be >= 0")

        sx = 0.0
        sy = 0.0
        sz = 0.0

        for i in range(samples):
            ax, ay, az = self.read_accel_g_corrected()
            sx += ax
            sy += ay
            sz += az
            if delay > 0 and i < (samples - 1):
                time.sleep(delay)

        return self.tilt_from_accel((sx / samples, sy / samples, sz / samples))


__all__ = [
    "MPU6050LowLevel",
    "MPU6050Error",
    "NotFound",
    "I2CError",
    "ProtocolError",
    "CalibrationError",
]


def _run_self_test(bus: Optional[int] = None, address: int = MPU6050LowLevel.DEFAULT_ADDRESS) -> int:
    drv = MPU6050LowLevel()

    try:
        drv.logger.info("Starting MPU6050 self-test")
        if not drv.init(bus=bus, address=address):
            drv.logger.error("Initialization failed: %s", drv.last_error)
            return 3

        result, details = drv.full_test()
        if result:
            drv.logger.info("MPU6050 self-test succeeded: %s", details)
            return 0

        drv.logger.error("MPU6050 self-test failed: %s", details)
        return 4
    except NotFound as exc:
        drv.logger.error("Missing dependency: %s", exc)
        return 3
    except Exception as exc:  # pragma: no cover
        drv.logger.exception("Unexpected error during MPU6050 self-test: %s", exc)
        return 5
    finally:
        try:
            drv.deinit()
        except Exception:
            pass

def main(argv=None) -> bool:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="MPU6050 / GY-521 low-level driver self-test"
    )

    parser.parse_args(argv)

    ll = MPU6050LowLevel()

    if not ll.init():
        report = {
            "initialized": False,
            "opened": False,
            "device_present": False,
            "errors": [ll.last_error] if ll.last_error else [],
            "details": {},
        }

        print(json.dumps(report, indent=2, default=str))
        return False

    ok, report = ll.full_test()

    print(json.dumps(report, indent=2, default=str))

    ll.deinit()

    return bool(ok)

if __name__ == "__main__":
    import sys

    ok = main(sys.argv[1:])
    raise SystemExit(0 if ok else 1)