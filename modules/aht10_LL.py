"""AHT10 low-level driver

Provides a minimal, synchronous low-level driver for the AHT10 sensor.

API (methods implemented):
- init(bus: int|None = None, address: int = 0x38)
- deinit()
- probe() -> bool
- reset()
- read_status() -> int
- is_busy(status: int|None = None) -> bool
- is_calibrated(status: int|None = None) -> bool
- trigger_measurement()
- read_measurement_raw() -> bytes
- parse(raw: bytes) -> (temp_c: float, rh: float)

Notes:
- Uses `smbus2` for I2C. If not installed a NotFound exception is raised.
- Performs bus discovery: tries bus 1 first, then scans available /dev/i2c-* devices.
"""

from __future__ import annotations

import time
from typing import Optional, Tuple, TYPE_CHECKING, Any, Iterable, cast

from modules.support.i2c_common import create_driver_logger, discover_i2c_buses

if TYPE_CHECKING:
    from smbus2 import SMBus as SMBusType
else:
    SMBusType = Any

try:
    from smbus2 import SMBus, i2c_msg
except Exception:  # pragma: no cover
    SMBus = None
    i2c_msg = None


class AHT10Error(Exception):
    pass


class NotFound(AHT10Error):
    pass


class I2CError(AHT10Error):
    pass


class BusyTimeout(AHT10Error):
    pass


class ProtocolError(AHT10Error):
    pass


class CRCError(AHT10Error):
    pass


class AHT10LowLevel:
    DEFAULT_ADDRESS = 0x38
    DEFAULT_BUS = 1

    def __init__(self, logger_name: str = "AHT10LowLevel"):
        self.logger = self._create_logger(logger_name)
        self.bus_num: Optional[int] = None
        self.bus: Optional[SMBusType] = None
        self.address = self.DEFAULT_ADDRESS

    def _require_i2c(self) -> None:
        if SMBus is None or i2c_msg is None:
            raise NotFound("smbus2 is required for AHT10 I2C operations. Install smbus2 package")

    def _create_logger(self, name: str):
        return create_driver_logger(
            logger_name=name,
            tag="AHT10LowLevel",
            logfile_name="AHT10LowLevel.log",
        )

    def init(self, bus: Optional[int] = None, address: int = DEFAULT_ADDRESS) -> None:
        """Initialize the I2C bus and locate the AHT10 device.

        If `bus` is None the driver will try discovery: try DEFAULT_BUS first,
        then scan /dev for i2c-* devices and attempt to probe each one.
        """
        if SMBus is None:
            raise NotFound("smbus2 is required for AHT10 I2C operations. Install smbus2 package")

        self.address = address
        candidates = discover_i2c_buses(bus if bus is not None else self.DEFAULT_BUS)

        last_exc = None
        for busnum in candidates:
            try:
                self.logger.info(f"Trying I2C bus {busnum} for AHT10@0x{self.address:02x}")
                b = SMBus(busnum)
                try:
                    # igual que el original: quick probe blando
                    b.read_byte(self.address)
                except OSError:
                    # no falla por eso; la presencia real la decide probe()
                    pass
                self.bus = b
                self.bus_num = busnum
                self.logger.info(f"Opened I2C bus {busnum}")
                return
            except Exception as e:
                last_exc = e
                self.logger.debug(f"Could not open bus {busnum}: {e}")

        raise I2CError(f"Could not open any I2C bus for AHT10 (tried {candidates}) - last error: {last_exc}")

    def deinit(self) -> None:
        if self.bus is not None:
            try:
                self.bus.close()
            except Exception:
                pass
        self.bus = None
        self.bus_num = None

    def probe(self) -> bool:
        """Keep original permissive probe behavior."""
        if self.bus is None:
            raise I2CError("Bus is not initialized. Call init() first.")
        try:
            self.trigger_measurement()
            time.sleep(0.01)
            _ = self._read_raw(1)
            return True
        except AHT10Error:
            return False
        except OSError:
            return False

    def reset(self) -> None:
        """Attempt a soft reset. If not supported this will try a no-op sequence."""
        self._require_i2c()
        if self.bus is None:
            raise I2CError("Bus is not initialized. Call init() first.")

        cmd = bytes([0xBA])
        assert i2c_msg is not None, "smbus2 i2c_msg missing"
        try:
            write = i2c_msg.write(self.address, cmd)
            self.bus.i2c_rdwr(write)
            time.sleep(0.05)
            self.logger.info("Sent soft reset to AHT10")
        except OSError as e:
            raise I2CError(f"I2C error during reset: {e}")

    def _read_raw(self, n: int) -> bytes:
        self._require_i2c()
        if self.bus is None:
            raise I2CError("Bus is not initialized. Call init() first.")

        assert i2c_msg is not None, "smbus2 i2c_msg missing"
        try:
            r = i2c_msg.read(self.address, n)
            self.bus.i2c_rdwr(r)
            return bytes(list(cast(Iterable[int], r)))
        except OSError as e:
            raise I2CError(f"I2C error during raw read({n}): {e}")

    def read_status(self) -> int:
        data = self._read_raw(1)
        if not data:
            raise ProtocolError("Empty status read")
        return data[0]

    def read_measurement_raw(self, timeout: float = 1.0, retry_on_null: bool = True) -> bytes:
        """Read one measurement from AHT10.

        For robustness with USB-I2C adapters, use a fixed post-trigger delay
        before reading the 6-byte payload.

        If `retry_on_null` is True and the payload appears null/empty, retry once.
        """
        time.sleep(0.10)

        data = self._read_raw(6)
        if len(data) < 6:
            raise ProtocolError(f"Expected 6 bytes, got {data!r}")

        payload_is_null = (
            data[1] == 0x00 and
            data[2] == 0x00 and
            data[3] == 0x00 and
            data[4] == 0x00 and
            data[5] == 0x00
        )

        if payload_is_null and retry_on_null:
            self.logger.debug(f"Null AHT10 payload detected, retrying once: {data!r}")
            time.sleep(0.05)
            data = self._read_raw(6)
            if len(data) < 6:
                raise ProtocolError(f"Expected 6 bytes on retry, got {data!r}")

        return data
    
    def is_busy(self, status: Optional[int] = None) -> bool:
        if status is None:
            status = self.read_status()
        return bool(status & 0x80)

    def is_calibrated(self, status: Optional[int] = None) -> bool:
        if status is None:
            status = self.read_status()
        return bool(status & 0x08)

    def trigger_measurement(self) -> None:
        """Send measurement command to AHT10: 0xAC 0x33 0x00"""
        self._require_i2c()
        if self.bus is None:
            raise I2CError("Bus is not initialized. Call init() first.")

        cmd = bytes([0xAC, 0x33, 0x00])
        assert i2c_msg is not None, "smbus2 i2c_msg missing"
        try:
            write = i2c_msg.write(self.address, cmd)
            self.bus.i2c_rdwr(write)
        except OSError:
            try:
                time.sleep(0.01)
                write = i2c_msg.write(self.address, cmd)
                self.bus.i2c_rdwr(write)
            except OSError as e2:
                raise I2CError(f"I2C error during trigger_measurement: {e2}")

    def parse(self, raw: bytes) -> Tuple[float, float]:
        """Parse 6-byte raw measurement from AHT10 into (temp_C, rh_pct).

        Format (standard):
        - raw[0] : status
        - bits 4..23 of next three bytes -> humidity (20-bit)
        - remaining 20 bits -> temperature (20-bit)
        """
        if not raw or len(raw) < 6:
            raise ProtocolError("Raw measurement must be at least 6 bytes")

        b = list(raw)
        hum_raw = ((b[1] << 16) | (b[2] << 8) | b[3]) >> 4
        temp_raw = ((b[3] & 0x0F) << 16) | (b[4] << 8) | b[5]

        rh = (hum_raw * 100.0) / float(1 << 20)
        temp_c = (temp_raw * 200.0) / float(1 << 20) - 50.0
        return temp_c, rh


__all__ = [
    "AHT10LowLevel",
    "AHT10Error",
    "NotFound",
    "I2CError",
    "BusyTimeout",
    "ProtocolError",
    "CRCError",
]


def _run_self_test(bus: Optional[int] = None) -> int:
    """Run a quick self-test when module executed as a script.

    - Initializes the sensor (with optional bus override)
    - Probes for presence
    - Reads status and logs busy/calibrated flags
    - Triggers a measurement and reads one sample
    - Parses and logs temperature (C) and humidity (%RH)
    - Deinitializes and returns exit code
    """
    import logging

    logger = logging.getLogger("AHT10LowLevel")
    drv = AHT10LowLevel()
    try:
        logger.info("Starting AHT10 self-test")
        drv.init(bus=bus)
        present = drv.probe()

        # replica del comportamiento original: si no aparece y no forzaste bus, prueba otros
        if not present and bus is None:
            logger.warning(
                f"AHT10 not present at address 0x{drv.address:02x} on initial bus {drv.bus_num}; scanning other I2C buses"
            )
            found = False
            for n in discover_i2c_buses():
                if n == drv.bus_num:
                    continue
                try:
                    drv.deinit()
                    logger.info(f"Trying alternative I2C bus {n}")
                    drv.init(bus=n)
                    if drv.probe():
                        logger.info(f"Found AHT10 at address 0x{drv.address:02x} on bus {n}")
                        found = True
                        break
                except Exception as e:
                    logger.debug(f"Could not open/probe bus {n}: {e}")
                    continue
            if not found:
                logger.error(f"AHT10 not present at address 0x{drv.address:02x} on any scanned bus")
                return 2
        elif not present:
            logger.error(f"AHT10 not present at address 0x{drv.address:02x} on bus {drv.bus_num}")
            return 2

        status = drv.read_status()
        logger.info(f"Status: 0x{status:02x} busy={drv.is_busy(status)} calibrated={drv.is_calibrated(status)}")

        drv.trigger_measurement()
        raw = drv.read_measurement_raw(timeout=2.0, retry_on_null=True)
        logger.debug(f"Raw measurement bytes: {[f'0x{x:02x}' for x in raw]}")
        temp_c, rh = drv.parse(raw)
        logger.info(f"Measurement: temp={temp_c:.2f} C, rh={rh:.2f} %")
        return 0

    except NotFound as e:
        logger.error("Missing dependency: smbus2 is required for I2C operations.\n" + str(e))
        return 3
    except AHT10Error as e:
        logger.exception("AHT10 self-test failed: %s", e)
        return 4
    except Exception as e:  # pragma: no cover
        logger.exception("Unexpected error during AHT10 self-test: %s", e)
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

    parser = argparse.ArgumentParser(description="AHT10 low-level driver self-test")
    parser.add_argument("--bus", "-b", type=int, default=None, help="I2C bus number override (optional)")
    args = parser.parse_args(argv)

    logger = logging.getLogger("AHT10LowLevel")
    rc = _run_self_test(bus=args.bus)
    success = rc == 0
    if success:
        logger.info("AHT10 self-test: OK")
    else:
        logger.error(f"AHT10 self-test: FAILED (rc={rc})")
    return success


if __name__ == "__main__":
    import sys

    ok = main(sys.argv[1:])
    raise SystemExit(0 if ok else 1)