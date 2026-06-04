"""HTU21 low-level driver (AHT10-compatible interface)

Provides a minimal synchronous low-level driver for the HTU21 sensor while
preserving the public API of the AHT10 low-level driver for compatibility.

Key differences/notes:
- Uses non-hold measurement commands (0xF3 / 0xF5) to avoid clock-stretching
  issues when other devices (e.g. GY-521) are present on the same I2C bus.
- Implements CRC-8 check (polynomial 0x31) for each 2-byte measurement.

API (methods implemented):
- init(bus: int|None = None, address: int = 0x40)
- deinit()
- probe() -> bool
- reset()
- read_status() -> int
- is_busy(status: int|None = None) -> bool
- is_calibrated(status: int|None = None) -> bool
- trigger_measurement()
- read_measurement_raw() -> bytes
- parse(raw: bytes) -> (temp_c: float, rh: float)
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional, Tuple, TYPE_CHECKING, Any, Iterable, cast, Type

from modules.support.i2c_common import discover_i2c_buses

if TYPE_CHECKING:
    from smbus2 import SMBus as SMBusType
    from smbus2 import i2c_msg as i2c_msg_mod
else:
    SMBusType = Any
    i2c_msg_mod = Any

try:
    from smbus2 import SMBus, i2c_msg
except Exception:  # pragma: no cover
    SMBus = None
    i2c_msg = None

# Help static type checkers (Pylance) understand the runtime-imported symbols
# by casting them to the types declared under TYPE_CHECKING. These casts are
# no-ops at runtime but remove false-positive type errors in editors.
# SMBus and i2c_msg are class/module objects (callable factories), so cast
# them as Optional[Type[...]] so editors know they can be called.
SMBus = cast(Optional['Type[SMBusType]'], SMBus)
i2c_msg = cast(Optional['Type[i2c_msg_mod]'], i2c_msg)


class HTU21Error(Exception):
    pass


class NotFound(HTU21Error):
    pass


class I2CError(HTU21Error):
    pass


class BusyTimeout(HTU21Error):
    pass


class ProtocolError(HTU21Error):
    pass


class CRCError(HTU21Error):
    pass


class HTU21LowLevel:
    DEFAULT_ADDRESS = 0x40
    DEFAULT_BUS = 1

    # HTU21 commands
    CMD_TRIG_TEMP_NOHOLD = 0xF3
    CMD_TRIG_HUMI_NOHOLD = 0xF5
    CMD_READ_USER_REG = 0xE7
    CMD_SOFTRESET = 0xFE

    def __init__(self, logger_name: str = "HTU21_LL"):
        self.logger = self._create_logger(logger_name)
        self.bus_num: Optional[int] = None
        self.bus: Optional[SMBusType] = None
        self.address = self.DEFAULT_ADDRESS
        self.is_initialized: bool = False
        self.is_open: bool = False
        self.last_error: Optional[str] = None
        self.bus_candidates: list[int] = []
        self.bus_forced: bool = False
        self.detect_others: bool = True
        # MPU-6050 (GY-521) related attributes
        self.mpu_address: Optional[int] = None
        self.mpu_present: bool = False
        self._mpu_woken: bool = False
        # accelerometer scale for default FS = +/-2g -> 16384 LSB/g
        self._accel_scale = 16384.0
        # gyro scale for default FS = +/-250 deg/s -> 131 LSB/(deg/s)
        self._gyro_scale = 131.0

    def _require_i2c(self) -> None:
        if SMBus is None or i2c_msg is None:
            raise NotFound("smbus2 is required for HTU21 I2C operations. Install smbus2 package")

    def _create_logger(self, name: str):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s [HTU21LowLevel] %(levelname)s: %(message)s")
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        log_dir = os.path.join(base_dir, "logs")
        try:
            os.makedirs(log_dir, exist_ok=True)
            fh = logging.FileHandler(os.path.join(log_dir, "HTU21LowLevel.log"), mode='a', encoding='utf-8')
            fh.setFormatter(fmt)
        except Exception:
            fh = None
        if not logger.handlers:
            logger.addHandler(ch)
            if fh:
                logger.addHandler(fh)
        return logger

    def _set_error(self, msg: str) -> None:
        self.last_error = msg

    def _clear_error(self) -> None:
        self.last_error = None

    def _build_full_test_report(self) -> dict:
        return {
            "initialized": self.is_initialized,
            "opened": self.is_open,
            "device_present": False,
            "errors": [],
            "details": {},
        }

    def _resolve_bus_candidates(self, preferred_bus: Optional[int]) -> list[int]:
        return discover_i2c_buses(preferred_bus)

    def init(self, bus: Optional[int] = None, address: int = DEFAULT_ADDRESS, detect_others: bool = True) -> bool:
        self.logger.info("Initializing HTU21 module")
        self._clear_error()

        try:
            self.close()
            self._require_i2c()
            self.address = int(address)
            self.bus_forced = bus is not None
            self.bus_num = int(bus) if bus is not None else None
            self.detect_others = bool(detect_others)
            self.bus_candidates = self._resolve_bus_candidates(self.bus_num if self.bus_forced else None)
            self.is_initialized = True
            self.is_open = False
            self.mpu_address = None
            self.mpu_present = False
            self._mpu_woken = False
            self.logger.info(
                "Module initialized: address=0x%02X bus_num=%s forced=%s candidates=%s detect_others=%s",
                self.address,
                self.bus_num,
                self.bus_forced,
                self.bus_candidates,
                self.detect_others,
            )
            return True
        except Exception as exc:
            self.is_initialized = False
            self._set_error(f"Initialization failed: {exc}")
            self.logger.exception("Initialization failed: %s", exc)
            return False

    def open(self) -> bool:
        self.logger.info("Opening HTU21 I2C bus")
        self._clear_error()

        if not self.is_initialized:
            self._set_error("Module is not initialized")
            self.logger.error(self.last_error)
            return False

        if self.is_open and self.bus is not None:
            self.logger.info("HTU21 I2C bus already open on %s", self.bus_num)
            return True

        candidates = [self.bus_num] if self.bus_forced and self.bus_num is not None else list(self.bus_candidates)
        last_exc: Optional[Exception] = None

        for busnum in candidates:
            try:
                self.logger.info("Trying I2C bus %s for HTU21@0x%02X", busnum, self.address)
                if SMBus is None:
                    raise NotFound("smbus2 is required for HTU21 I2C operations. Install smbus2 package")
                self.bus = SMBus(busnum)
                self.bus_num = busnum
                self.is_open = True
                self.logger.info("Opened I2C bus %s", busnum)

                if self.detect_others:
                    try:
                        try_addr = 0x68
                        self.bus.read_byte(try_addr)
                        self.mpu_address = try_addr
                        self.mpu_present = True
                        self.logger.info("Detected MPU-6050 at 0x%02x", try_addr)
                    except OSError:
                        self.mpu_address = None
                        self.mpu_present = False
                return True
            except Exception as exc:
                last_exc = exc
                self.logger.warning("Could not open bus %s: %s", busnum, exc)
                if self.bus is not None:
                    try:
                        self.bus.close()
                    except Exception:
                        pass
                self.bus = None
                self.is_open = False

        self.is_open = False
        self.bus = None
        self._set_error(f"Could not open any I2C bus for HTU21 (tried {candidates}) - last error: {last_exc}")
        self.logger.error(self.last_error)
        return False

    def close(self) -> bool:
        self.logger.info("Closing HTU21 I2C bus")
        self._clear_error()

        try:
            if self.bus is not None:
                try:
                    self.bus.close()
                except Exception:
                    pass
            self.bus = None
            self.is_open = False
            return True
        except Exception as exc:
            self.bus = None
            self.is_open = False
            self._set_error(f"Close failed: {exc}")
            self.logger.exception("Close failed: %s", exc)
            return False

    def deinit(self) -> bool:
        self.logger.info("Deinitializing HTU21 module")
        self._clear_error()

        try:
            self.close()
            self.is_initialized = False
            self.is_open = False
            self.bus_num = None
            self.address = self.DEFAULT_ADDRESS
            self.bus_candidates = []
            self.bus_forced = False
            self.detect_others = True
            self.mpu_address = None
            self.mpu_present = False
            self._mpu_woken = False
            self.last_error = None
            return True
        except Exception as exc:
            self._set_error(f"Deinitialization failed: {exc}")
            self.logger.exception("Deinitialization failed: %s", exc)
            return False

    def probe(self) -> bool:
        self.logger.info("Probing HTU21 module")
        self._clear_error()

        if not self.is_initialized:
            self._set_error("Module is not initialized")
            self.logger.error(self.last_error)
            return False

        was_open = self.is_open and self.bus is not None
        temporarily_opened = False

        try:
            if not was_open:
                if not self.open():
                    return False
                temporarily_opened = True

            _ = self.read_status()
            return True
        except Exception as exc:
            self._set_error(f"Probe failed: {exc}")
            self.logger.warning("Probe failed: %s", exc)
            return False
        finally:
            if temporarily_opened:
                self.close()

    def test(self) -> bool:
        self.logger.info("Running HTU21 smoke test")
        self._clear_error()
        was_open = self.is_open and self.bus is not None
        temporarily_opened = False

        try:
            if not was_open:
                if not self.open():
                    return False
                temporarily_opened = True

            result = self.probe()
            self.logger.info("Smoke test completed: success=%s", result)
            return result
        except Exception as exc:
            self._set_error(f"Test failed: {exc}")
            self.logger.warning("Test failed: %s", exc)
            return False
        finally:
            if temporarily_opened:
                self.close()

    def full_test(self) -> tuple[bool, dict]:
        self.logger.info("Running full diagnostic test")
        self._clear_error()
        report = self._build_full_test_report()

        if not self.is_initialized:
            msg = "Module is not initialized"
            report["errors"].append(msg)
            self._set_error(msg)
            self.logger.error(msg)
            return False, report

        was_open = self.is_open and self.bus is not None
        temporarily_opened = False

        try:
            if not was_open:
                if not self.open():
                    report["errors"].append(self.last_error or "Open failed")
                    return False, report
                temporarily_opened = True
            report["opened"] = True

            probe_ok = self.probe()
            report["device_present"] = probe_ok
            if not probe_ok:
                report["errors"].append(self.last_error or "Probe failed")

            try:
                status = self.read_status()
                report["details"]["status"] = f"0x{status:02x}"
            except Exception as exc:
                report["errors"].append(f"Status read failed: {exc}")

            try:
                raw = self.read_measurement_raw(timeout=2.0)
                temp_c, rh = self.parse(raw)
                report["details"]["measurement"] = {
                    "temp_c": temp_c,
                    "rh": rh,
                }
            except Exception as exc:
                report["errors"].append(f"Measurement failed: {exc}")

            if self.mpu_present:
                try:
                    mpu_ok = self.test_mpu_accel()
                    report["details"]["mpu_accel_ok"] = mpu_ok
                    if not mpu_ok:
                        report["errors"].append("MPU-6050 accelerometer self-test failed")
                except Exception as exc:
                    report["errors"].append(f"MPU self-test failed: {exc}")

            success = bool(report["initialized"] and report["opened"] and report["device_present"] and not report["errors"])
            return success, report
        except Exception as exc:
            report["errors"].append(f"Unexpected full_test failure: {exc}")
            self._set_error(f"Full test failed: {exc}")
            self.logger.exception("Full test failed: %s", exc)
            return False, report
        finally:
            if temporarily_opened:
                self.close()

    def reset(self) -> None:
        self._require_i2c()
        if self.bus is None:
            raise I2CError("Bus is not initialized. Call init() first.")
        cmd = bytes([self.CMD_SOFTRESET])
        assert i2c_msg is not None, "smbus2 i2c_msg missing"
        try:
            write = i2c_msg.write(self.address, cmd)
            # Help static type checkers: self.bus was checked above, cast to concrete SMBus type
            b = cast(SMBusType, self.bus)
            b.i2c_rdwr(write)
            time.sleep(0.05)
            self.logger.info("Sent soft reset to HTU21")
        except OSError as e:
            raise I2CError(f"I2C error during reset: {e}")

    def _read_raw(self, n: int) -> bytes:
        self._require_i2c()
        if self.bus is None:
            raise I2CError("Bus is not initialized. Call init() first.")
        assert i2c_msg is not None, "smbus2 i2c_msg missing"
        try:
            r = i2c_msg.read(self.address, n)
            b = cast(SMBusType, self.bus)
            b.i2c_rdwr(r)
            return bytes(list(cast(Iterable[int], r)))
        except OSError as e:
            raise I2CError(f"I2C error during raw read({n}): {e}")

    def read_status(self) -> int:
        # Read HTU21 user register (1 byte)
        self._require_i2c()
        if self.bus is None:
            raise I2CError("Bus is not initialized. Call init() first.")
        assert i2c_msg is not None, "smbus2 i2c_msg missing"
        try:
            write = i2c_msg.write(self.address, bytes([self.CMD_READ_USER_REG]))
            read = i2c_msg.read(self.address, 1)
            b = cast(SMBusType, self.bus)
            b.i2c_rdwr(write, read)
            data = bytes(list(cast(Iterable[int], read)))
            return data[0]
        except OSError as e:
            raise I2CError(f"I2C error during read_status (user reg): {e}")

    def read_measurement_raw(self, timeout: float = 1.0) -> bytes:
        """Perform non-hold measurements: humidity then temperature.

        Returns 6 bytes: [h_msb, h_lsb, h_crc, t_msb, t_lsb, t_crc]
        """
        self._require_i2c()
        if self.bus is None:
            raise I2CError("Bus is not initialized. Call init() first.")
        assert i2c_msg is not None, "smbus2 i2c_msg missing"
        i2c = i2c_msg

        def _do_nohold(cmd: int) -> bytes:
            # send command
            try:
                write = i2c.write(self.address, bytes([cmd]))
                b = cast(SMBusType, self.bus)
                b.i2c_rdwr(write)
            except OSError as e:
                raise I2CError(f"I2C error sending command 0x{cmd:02x}: {e}")
            # then poll for read-ready
            start = time.time()
            while True:
                try:
                    r = i2c.read(self.address, 3)
                    b = cast(SMBusType, self.bus)
                    b.i2c_rdwr(r) 
                    data = bytes(list(cast(Iterable[int], r)))
                    if len(data) == 3:
                        return data
                except OSError:
                    # measurement not ready yet
                    pass
                if (time.time() - start) > timeout:
                    raise BusyTimeout(f"Timeout waiting for HTU21 measurement (cmd=0x{cmd:02x})")
                time.sleep(0.01)

        h = _do_nohold(self.CMD_TRIG_HUMI_NOHOLD)
        t = _do_nohold(self.CMD_TRIG_TEMP_NOHOLD)
        return h + t

    def is_busy(self, status: Optional[int] = None) -> bool:
        # HTU21 doesn't expose a busy flag like AHT10; use non-blocking behavior
        return False

    def is_calibrated(self, status: Optional[int] = None) -> bool:
        # Not applicable to HTU21; keep interface compatibility
        return True

    def trigger_measurement(self) -> None:
        # Keep API compatibility. Using non-hold commands so actual measurement is
        # performed in read_measurement_raw(). We log for observability.
        self.logger.debug("trigger_measurement() called (no-op for HTU21; measurements performed on read)")

    @staticmethod
    def _crc8(data: bytes) -> int:
        """CRC-8 (polynomial 0x31) used by Sensirion/HTU21.

        Algorithm: initial crc=0x00, poly=0x31, shift-left implementation.
        """
        crc = 0
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x80:
                    crc = ((crc << 1) ^ 0x31) & 0xFF
                else:
                    crc = (crc << 1) & 0xFF
        return crc

    def parse(self, raw: bytes) -> Tuple[float, float]:
        """Parse 6-byte HTU21 measurement into (temp_C, rh_pct).

        raw layout: [h_msb, h_lsb, h_crc, t_msb, t_lsb, t_crc]
        Validates CRC on each 2-byte word.
        """
        if not raw or len(raw) < 6:
            raise ProtocolError("Raw measurement must be at least 6 bytes for HTU21")
        h_msb, h_lsb, h_crc, t_msb, t_lsb, t_crc = raw[0:6]
        hum_raw = (h_msb << 8) | h_lsb
        temp_raw = (t_msb << 8) | t_lsb

        # Mask status bits: HTU21/SHT21 use 14-bit humidity/temperature values
        # The two least-significant bits contain status flags and must be cleared
        hum_raw = hum_raw & 0xFFFC
        temp_raw = temp_raw & 0xFFFC

        # CRC validation
        if self._crc8(bytes([h_msb, h_lsb])) != h_crc:
            raise CRCError("Humidity CRC mismatch")
        if self._crc8(bytes([t_msb, t_lsb])) != t_crc:
            raise CRCError("Temperature CRC mismatch")

        # per datasheet conversions
        rh = -6.0 + 125.0 * (hum_raw / 65536.0)
        temp_c = -46.85 + 175.72 * (temp_raw / 65536.0)
        return temp_c, rh

    # --- HTU21 convenience API ---
    def read_htu21(self, timeout: float = 1.0) -> Tuple[float, float]:
        """Convenience: perform HTU21 measurement and return (temp_c, rh_pct)."""
        raw = self.read_measurement_raw(timeout=timeout)
        return self.parse(raw)

    # --- MPU-6050 (GY-521) support ---
    def probe_mpu(self, address: int = 0x68) -> bool:
        """Probe for an MPU-6050 at the given address. Returns True if present."""
        self._require_i2c()
        if self.bus is None:
            raise I2CError("Bus is not initialized. Call init() first.")
        try:
            b = cast(SMBusType, self.bus)
            b.read_byte(address)
            self.mpu_address = address
            self.mpu_present = True
            return True
        except OSError:
            return False

    def _mpu_write(self, register: int, value: int) -> None:
        if self.bus is None:
            raise I2CError("Bus is not initialized. Call init() first.")
        try:
            b = cast(SMBusType, self.bus)
            b.write_byte_data(self.mpu_address, register, value)  # type: ignore[arg-type]
        except Exception as e:
            raise I2CError(f"I2C error writing MPU register 0x{register:02x}: {e}")

    def _mpu_read_block(self, register: int, length: int) -> bytes:
        if self.bus is None:
            raise I2CError("Bus is not initialized. Call init() first.")
        try:
            b = cast(SMBusType, self.bus)
            data = b.read_i2c_block_data(self.mpu_address, register, length)  # type: ignore[arg-type]
            return bytes(data)
        except Exception as e:
            raise I2CError(f"I2C error reading MPU block 0x{register:02x}: {e}")

    def mpu_wake(self) -> None:
        """Wake MPU-6050 (clear sleep bit in PWR_MGMT_1)."""
        if not self.mpu_present or self.mpu_address is None:
            raise I2CError("MPU-6050 not present; probe or init with detection enabled")
        # PWR_MGMT_1 = 0x6B, write 0x00 to wake
        self._mpu_write(0x6B, 0x00)
        time.sleep(0.01)
        self._mpu_woken = True

    def read_mpu6050(self) -> Tuple[float, float, float]:
        """Read accelerometer (x,y,z) in g from MPU-6050.

        Returns (ax, ay, az) in units of g. This only reads accelerometer.
        """
        if not self.mpu_present:
            # try to probe default address automatically
            if not self.probe_mpu(0x68):
                # try alternate
                if not self.probe_mpu(0x69):
                    raise I2CError("MPU-6050 not detected on I2C bus")
        if not self._mpu_woken:
            try:
                self.mpu_wake()
            except I2CError:
                # try to continue anyway
                pass

        data = self._mpu_read_block(0x3B, 6)
        if len(data) < 6:
            raise ProtocolError("Incomplete accelerometer data from MPU-6050")
        def _to_signed(msb: int, lsb: int) -> int:
            v = (msb << 8) | lsb
            if v & 0x8000:
                v = -((~v & 0xFFFF) + 1)
            return v

        ax = _to_signed(data[0], data[1]) / self._accel_scale
        ay = _to_signed(data[2], data[3]) / self._accel_scale
        az = _to_signed(data[4], data[5]) / self._accel_scale
        return ax, ay, az

    def read_mpu_gyro(self) -> Tuple[float, float, float]:
        """Read gyroscope (x,y,z) in deg/s from MPU-6050.

        Returns (gx, gy, gz) in deg/s using default full-scale (±250 dps).
        """
        if not self.mpu_present:
            if not self.probe_mpu(0x68):
                raise I2CError("MPU-6050 not detected on I2C bus")
        data = self._mpu_read_block(0x43, 6)
        if len(data) < 6:
            raise ProtocolError("Incomplete gyroscope data from MPU-6050")
        def _to_signed(msb: int, lsb: int) -> int:
            v = (msb << 8) | lsb
            if v & 0x8000:
                v = -((~v & 0xFFFF) + 1)
            return v

        gx = _to_signed(data[0], data[1]) / self._gyro_scale
        gy = _to_signed(data[2], data[3]) / self._gyro_scale
        gz = _to_signed(data[4], data[5]) / self._gyro_scale
        return gx, gy, gz

    def read_mpu_temp(self) -> float:
        """Read internal MPU-6050 temperature in degrees Celsius.

        Datasheet: Temp_degC = (TEMP_OUT Register)/340 + 36.53
        """
        if not self.mpu_present:
            if not self.probe_mpu(0x68):
                raise I2CError("MPU-6050 not detected on I2C bus")
        data = self._mpu_read_block(0x41, 2)
        if len(data) < 2:
            raise ProtocolError("Incomplete temperature data from MPU-6050")
        raw = (data[0] << 8) | data[1]
        if raw & 0x8000:
            raw = -((~raw & 0xFFFF) + 1)
        temp_c = (raw / 340.0) + 36.53
        return temp_c

    def test_mpu_accel(self, samples: int = 5, delay: float = 0.02) -> bool:
        """Basic accelerometer self-test for MPU-6050.

        Reads `samples` accelerometer measurements and verifies values are
        responsive and within plausible bounds. Returns True on pass.
        """
        if not self.mpu_present:
            if not self.probe_mpu(0x68):
                self.logger.warning("test_mpu_accel: MPU-6050 not detected at 0x68")
                return False
        try:
            if not self._mpu_woken:
                self.mpu_wake()
        except I2CError:
            pass

        readings = []
        for _ in range(max(1, samples)):
            ax, ay, az = self.read_mpu6050()
            readings.append((ax, ay, az))
            time.sleep(delay)

        # sanity checks: no all-zero readings and values within +/-4 g
        all_zero = all(abs(a) < 1e-6 and abs(b) < 1e-6 and abs(c) < 1e-6 for (a, b, c) in readings)
        if all_zero:
            self.logger.error("MPU-6050 accelerometer appears to be stuck at zero")
            return False

        for (a, b, c) in readings:
            if any(abs(x) > 4.0 for x in (a, b, c)):
                self.logger.error("MPU-6050 accelerometer reading out of plausible range: %s", (a, b, c))
                return False

        # basic pass
        return True


__all__ = [
    'HTU21LowLevel', 'HTU21Error', 'NotFound', 'I2CError', 'BusyTimeout', 'ProtocolError', 'CRCError'
]


def _run_self_test(bus: Optional[int] = None) -> int:
    # Instantiate driver first so its logger (file handler) is configured
    drv = HTU21LowLevel()
    logger = drv.logger
    try:
        logger.info('Starting HTU21 self-test')
        drv.init(bus=bus)
        present = drv.probe()
        if not present and bus is None:
            logger.warning(f'HTU21 not present at address 0x{drv.address:02x} on initial bus {drv.bus_num}; scanning other I2C buses')
            found = False
            for p in sorted(Path('/dev').glob('i2c-*')):
                try:
                    n = int(p.name.split('-')[1])
                except Exception:
                    continue
                if n == drv.bus_num:
                    continue
                try:
                    drv.deinit()
                    logger.info(f'Trying alternative I2C bus {n}')
                    drv.init(bus=n)
                    if drv.probe():
                        logger.info(f'Found HTU21 at address 0x{drv.address:02x} on bus {n}')
                        found = True
                        break
                except Exception as e:
                    logger.debug(f'Could not open/probe bus {n}: {e}')
                    continue
            if not found:
                logger.error(f'HTU21 not present at address 0x{drv.address:02x} on any scanned bus')
                return 2
        elif not present:
            logger.error(f'HTU21 not present at address 0x{drv.address:02x} on bus {drv.bus_num}')
            return 2
        status = drv.read_status()
        logger.info(f'User reg: 0x{status:02x}')
        raw = drv.read_measurement_raw(timeout=2.0)
        temp_c, rh = drv.parse(raw)
        logger.info(f'Measurement: temp={temp_c:.2f} C, rh={rh:.2f} %')
        # MPU-6050 (GY-521) self-test (best-effort)
        try:
            if drv.probe_mpu(0x68):
                logger.info('MPU-6050 detected at 0x68; running accelerometer self-test')
                try:
                    ok = drv.test_mpu_accel()
                    if ok:
                        logger.info('MPU-6050 accelerometer self-test: OK')
                    else:
                        logger.error('MPU-6050 accelerometer self-test: FAILED')
                        return 6
                except HTU21Error as e:
                    logger.exception('MPU-6050 self-test error: %s', e)
                    return 6
            else:
                logger.warning('MPU-6050 not detected at 0x68')
        except Exception:
            logger.exception('Unexpected error during MPU-6050 self-test')
            return 6
        return 0
    except NotFound as e:
        logger.error('Missing dependency: smbus2 is required for I2C operations.\n' + str(e))
        return 3
    except HTU21Error as e:
        logger.exception('HTU21 self-test failed: %s', e)
        return 4
    except Exception as e:  # pragma: no cover - unexpected
        logger.exception('Unexpected error during HTU21 self-test: %s', e)
        return 5
    finally:
        try:
            drv.deinit()
        except Exception:
            pass


def main(argv=None) -> bool:
    import argparse
    import logging

    parser = argparse.ArgumentParser(description='HTU21 low-level driver self-test')
    parser.add_argument('--bus', '-b', type=int, default=None, help='I2C bus number override (optional)')
    args = parser.parse_args(argv)
    logger = logging.getLogger('HTU21LowLevel')
    rc = _run_self_test(bus=args.bus)
    success = rc == 0
    if success:
        logger.info('HTU21 self-test: OK')
    else:
        logger.error(f'HTU21 self-test: FAILED (rc={rc})')
    return success


if __name__ == '__main__':
    import sys

    ok = main(sys.argv[1:])
    raise SystemExit(0 if ok else 1)
