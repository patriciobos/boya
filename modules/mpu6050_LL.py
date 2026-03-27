"""MPU-6050 / GY-521 low-level driver

Provides a minimal, synchronous low-level driver for the MPU-6050 sensor
commonly found on the GY-521 breakout board.

API (methods implemented):
- init(bus: int|None = None, address: int = 0x68)
- deinit()
- probe() -> bool
- reset()
- wake()
- sleep()
- whoami() -> int
- read_reg(reg: int) -> int
- write_reg(reg: int, value: int)
- read_block(reg: int, n: int) -> bytes

- set_clock_source(source: int = 1)
- set_dlpf(cfg: int)
- set_sample_rate_div(div: int)
- set_accel_range(fs_sel: int)
- get_accel_range() -> int
- set_gyro_range(fs_sel: int)
- get_gyro_range() -> int
- configure_default()

- read_accel_raw() -> (ax: int, ay: int, az: int)
- read_gyro_raw() -> (gx: int, gy: int, gz: int)
- read_temp_raw() -> int
- read_motion6_raw() -> (ax, ay, az, gx, gy, gz)
- read_all_raw() -> dict

- accel_scale_g_per_lsb() -> float
- gyro_scale_dps_per_lsb() -> float
- parse_accel(raw: tuple[int, int, int]) -> tuple[float, float, float]
- parse_gyro(raw: tuple[int, int, int]) -> tuple[float, float, float]
- parse_temp(raw: int) -> float

- read_accel_g() -> tuple[float, float, float]
- read_gyro_dps() -> tuple[float, float, float]
- read_temp_c() -> float
- read_motion6() -> tuple[float, float, float, float, float, float]
- read_all() -> dict

- read_int_status() -> int
- is_data_ready(status: int|None = None) -> bool
- is_fifo_overflow(status: int|None = None) -> bool
- enable_data_ready_interrupt()
- disable_interrupts()

- set_accel_offsets(x: float, y: float, z: float)
- get_accel_offsets() -> tuple[float, float, float]
- clear_accel_offsets()
- set_gyro_offsets(x: float, y: float, z: float)
- get_gyro_offsets() -> tuple[float, float, float]
- clear_gyro_offsets()
- clear_offsets()
- calibrate_gyro(samples: int = 500, delay: float = 0.005) -> tuple[float, float, float]
- calibrate_accel(samples: int = 500, delay: float = 0.005, expected=(0.0, 0.0, 1.0)) -> tuple[float, float, float]
- read_accel_g_corrected() -> tuple[float, float, float]
- read_gyro_dps_corrected() -> tuple[float, float, float]
- read_all_corrected() -> dict

Notes:
- Uses `smbus2` for I2C. If not installed a NotFound exception is raised.
- Performs bus discovery: tries bus 12 first, then scans available /dev/i2c-* devices.
- Default I2C address is 0x68. Some boards use AD0=HIGH => 0x69.
"""

from __future__ import annotations

import time, math
from typing import Optional, Tuple, TYPE_CHECKING, Any

from modules.support.i2c_common import create_driver_logger, discover_i2c_buses

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

    def __init__(self, logger_name: str = "MPU6050LowLevel"):
        self.logger = self._create_logger(logger_name)
        self.bus_num: Optional[int] = None
        self.bus: Optional[SMBusType] = None
        self.address = self.DEFAULT_ADDRESS

        self.accel_offsets_g: Tuple[float, float, float] = (0.0, 0.0, 0.0)
        self.gyro_offsets_dps: Tuple[float, float, float] = (0.0, 0.0, 0.0)

    def _require_i2c(self) -> None:
        if SMBus is None:
            raise NotFound("smbus2 is required for MPU6050 I2C operations. Install smbus2 package")

    def _create_logger(self, name: str):
        return create_driver_logger(
            logger_name=name,
            tag="MPU6050LowLevel",
            logfile_name="MPU6050LowLevel.log",
        )

    def init(self, bus: Optional[int] = None, address: int = DEFAULT_ADDRESS) -> None:
        """Initialize I2C bus and select target address."""
        self._require_i2c()
        if SMBus is None:
            raise NotFound("smbus2 is required for MPU6050 I2C operations. Install smbus2 package")
        bus_cls = SMBus

        self.address = address

        candidates = discover_i2c_buses(bus if bus is not None else self.DEFAULT_BUS)

        last_exc = None
        for busnum in candidates:
            try:
                self.logger.info(f"Trying I2C bus {busnum} for MPU6050@0x{self.address:02x}")
                b = bus_cls(busnum)
                self.bus = b
                self.bus_num = busnum
                self.logger.info(f"Opened I2C bus {busnum}")
                return
            except Exception as e:
                last_exc = e
                self.logger.debug(f"Could not open bus {busnum}: {e}")

        raise I2CError(f"Could not open any I2C bus for MPU6050 (tried {candidates}) - last error: {last_exc}")

    def deinit(self) -> None:
        if self.bus is not None:
            try:
                self.bus.close()
            except Exception:
                pass
        self.bus = None
        self.bus_num = None

    def _require_bus(self) -> SMBusType:
        self._require_i2c()
        if self.bus is None:
            raise I2CError("Bus is not initialized. Call init() first.")
        return self.bus

    def probe(self) -> bool:
        if self.bus is None:
            raise I2CError("Bus is not initialized. Call init() first.")
        try:
            dev_id = self.whoami()
            return dev_id in (self.DEVICE_ID_68, self.DEVICE_ID_69)
        except MPU6050Error:
            return False
        except OSError:
            return False

    def read_reg(self, reg: int) -> int:
        bus = self._require_bus()
        try:
            return int(bus.read_byte_data(self.address, reg)) & 0xFF
        except OSError as e:
            raise I2CError(f"I2C error during read_reg(0x{reg:02x}): {e}")

    def write_reg(self, reg: int, value: int) -> None:
        bus = self._require_bus()
        try:
            bus.write_byte_data(self.address, reg, value & 0xFF)
        except OSError as e:
            raise I2CError(f"I2C error during write_reg(0x{reg:02x}, 0x{value:02x}): {e}")

    def read_block(self, reg: int, n: int) -> bytes:
        bus = self._require_bus()
        try:
            data = bus.read_i2c_block_data(self.address, reg, n)
            return bytes(data)
        except OSError as e:
            raise I2CError(f"I2C error during read_block(0x{reg:02x}, {n}): {e}")

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
        """Apply sane default configuration for host polling."""
        self.wake()
        self.set_clock_source(self.CLOCK_PLL_XGYRO)
        self.set_dlpf(3)
        self.set_sample_rate_div(9)
        self.set_accel_range(self.ACCEL_FS_2G)
        self.set_gyro_range(self.GYRO_FS_250)
        self.disable_interrupts()

    @staticmethod
    def _to_int16(msb: int, lsb: int) -> int:
        v = ((msb & 0xFF) << 8) | (lsb & 0xFF)
        if v & 0x8000:
            v -= 0x10000
        return v

    def read_accel_raw(self) -> Tuple[int, int, int]:
        data = self.read_block(self.REG_ACCEL_XOUT_H, 6)
        if len(data) != 6:
            raise ProtocolError(f"Expected 6 accel bytes, got {data!r}")
        ax = self._to_int16(data[0], data[1])
        ay = self._to_int16(data[2], data[3])
        az = self._to_int16(data[4], data[5])
        return ax, ay, az

    def read_temp_raw(self) -> int:
        data = self.read_block(self.REG_TEMP_OUT_H, 2)
        if len(data) != 2:
            raise ProtocolError(f"Expected 2 temp bytes, got {data!r}")
        return self._to_int16(data[0], data[1])

    def read_gyro_raw(self) -> Tuple[int, int, int]:
        data = self.read_block(self.REG_GYRO_XOUT_H, 6)
        if len(data) != 6:
            raise ProtocolError(f"Expected 6 gyro bytes, got {data!r}")
        gx = self._to_int16(data[0], data[1])
        gy = self._to_int16(data[2], data[3])
        gz = self._to_int16(data[4], data[5])
        return gx, gy, gz

    def read_motion6_raw(self) -> Tuple[int, int, int, int, int, int]:
        data = self.read_block(self.REG_ACCEL_XOUT_H, 14)
        if len(data) != 14:
            raise ProtocolError(f"Expected 14 bytes, got {data!r}")

        ax = self._to_int16(data[0], data[1])
        ay = self._to_int16(data[2], data[3])
        az = self._to_int16(data[4], data[5])

        gx = self._to_int16(data[8], data[9])
        gy = self._to_int16(data[10], data[11])
        gz = self._to_int16(data[12], data[13])

        return ax, ay, az, gx, gy, gz

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

    def calibrate_gyro(
        self,
        samples: int = 500,
        delay: float = 0.005,
    ) -> Tuple[float, float, float]:
        if samples <= 0:
            raise CalibrationError("samples must be > 0")
        if delay < 0:
            raise CalibrationError("delay must be >= 0")

        sx = 0.0
        sy = 0.0
        sz = 0.0

        self.logger.info(f"Starting gyro calibration: samples={samples} delay={delay}")
        for _ in range(samples):
            gx, gy, gz = self.read_gyro_dps()
            sx += gx
            sy += gy
            sz += gz
            if delay > 0:
                time.sleep(delay)

        offsets = (sx / samples, sy / samples, sz / samples)
        self.set_gyro_offsets(*offsets)
        self.logger.info(f"Gyro calibration complete: offsets_dps={offsets}")
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
            f"Starting accel calibration: samples={samples} delay={delay} expected={expected}"
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
        self.logger.info(f"Accel calibration complete: offsets_g={offsets}")
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
        """Read accelerometer and return (roll_deg, pitch_deg)."""
        return self.tilt_from_accel(self.read_accel_g())

    def read_tilt_deg_corrected(self) -> Tuple[float, float]:
        """Read corrected accelerometer and return (roll_deg, pitch_deg)."""
        return self.tilt_from_accel(self.read_accel_g_corrected())

    def read_tilt_deg_avg(self, samples: int = 10, delay: float = 0.02) -> Tuple[float, float]:
        """Average several corrected accel samples and return (roll_deg, pitch_deg)."""
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
    """Run a quick self-test when module executed as a script."""
    import logging

    logger = logging.getLogger("MPU6050LowLevel")
    drv = MPU6050LowLevel()

    try:
        logger.info("Starting MPU6050 self-test")
        drv.init(bus=bus, address=address)

        present = drv.probe()
        if not present and address == MPU6050LowLevel.DEFAULT_ADDRESS:
            logger.warning("MPU6050 not present at 0x68, trying alternate address 0x69")
            drv.address = MPU6050LowLevel.ALT_ADDRESS
            present = drv.probe()

        if not present and bus is None:
            logger.warning(
                f"MPU6050 not present on initial bus {drv.bus_num}; scanning other I2C buses and both addresses"
            )
            found = False
            for n in discover_i2c_buses():
                if n == drv.bus_num:
                    continue
                for addr in (MPU6050LowLevel.DEFAULT_ADDRESS, MPU6050LowLevel.ALT_ADDRESS):
                    try:
                        drv.deinit()
                        logger.info(f"Trying alternative I2C bus {n}, address 0x{addr:02x}")
                        drv.init(bus=n, address=addr)
                        if drv.probe():
                            logger.info(f"Found MPU6050 at address 0x{addr:02x} on bus {n}")
                            found = True
                            break
                    except Exception as e:
                        logger.debug(f"Could not open/probe bus {n}, addr 0x{addr:02x}: {e}")
                        continue
                if found:
                    break

            if not found:
                logger.error("MPU6050 not present on any scanned bus/address")
                return 2
        elif not present:
            logger.error(f"MPU6050 not present at address 0x{drv.address:02x} on bus {drv.bus_num}")
            return 2

        who = drv.whoami()
        logger.info(f"WHO_AM_I: 0x{who:02x}")

        drv.reset()
        drv.configure_default()

        int_status = drv.read_int_status()
        logger.info(
            f"INT_STATUS: 0x{int_status:02x} "
            f"data_ready={drv.is_data_ready(int_status)} "
            f"fifo_overflow={drv.is_fifo_overflow(int_status)}"
        )

        raw = drv.read_all_raw()
        parsed = drv.read_all()

        logger.info(
            "Measurement raw: "
            f"accel=({raw['ax']}, {raw['ay']}, {raw['az']}) "
            f"temp_raw={raw['temp_raw']} "
            f"gyro=({raw['gx']}, {raw['gy']}, {raw['gz']})"
        )
        logger.info(
            "Measurement parsed: "
            f"accel_g=({parsed['ax_g']:.5f}, {parsed['ay_g']:.5f}, {parsed['az_g']:.5f}) "
            f"temp_c={parsed['temp_c']:.2f} "
            f"gyro_dps=({parsed['gx_dps']:.5f}, {parsed['gy_dps']:.5f}, {parsed['gz_dps']:.5f})"
        )
        logger.info(
            f"Offsets: accel_g={drv.get_accel_offsets()} gyro_dps={drv.get_gyro_offsets()}"
        )
        return 0

    except NotFound as e:
        logger.error("Missing dependency: smbus2 is required for I2C operations.\n" + str(e))
        return 3
    except MPU6050Error as e:
        logger.exception("MPU6050 self-test failed: %s", e)
        return 4
    except Exception as e:  # pragma: no cover
        logger.exception("Unexpected error during MPU6050 self-test: %s", e)
        return 5
    finally:
        try:
            drv.deinit()
        except Exception:
            pass


def main(argv=None) -> bool:
    """Run self-test as a script and return True on success, False on failure."""
    import argparse
    import logging

    parser = argparse.ArgumentParser(description="MPU6050 / GY-521 low-level driver self-test")
    parser.add_argument("--bus", "-b", type=int, default=None, help="I2C bus number override (optional)")
    parser.add_argument(
        "--address",
        "-a",
        type=lambda x: int(x, 0),
        default=MPU6050LowLevel.DEFAULT_ADDRESS,
        help="I2C address override (default: 0x68, alternate: 0x69)",
    )
    args = parser.parse_args(argv)

    logger = logging.getLogger("MPU6050LowLevel")
    rc = _run_self_test(bus=args.bus, address=args.address)
    success = rc == 0
    if success:
        logger.info("MPU6050 self-test: OK")
    else:
        logger.error(f"MPU6050 self-test: FAILED (rc={rc})")
    return success


if __name__ == "__main__":
    import sys

    ok = main(sys.argv[1:])
    raise SystemExit(0 if ok else 1)