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

    def init(self, bus: Optional[int] = None, address: int = DEFAULT_ADDRESS) -> None:
        if SMBus is None:
            raise NotFound("smbus2 is required for HTU21 I2C operations. Install smbus2 package")
        self.address = address
        candidates = []
        if bus is not None:
            candidates.append(int(bus))
        else:
            candidates.append(self.DEFAULT_BUS)
            for p in sorted(Path('/dev').glob('i2c-*')):
                try:
                    n = int(p.name.split('-')[1])
                except Exception:
                    continue
                if n not in candidates:
                    candidates.append(n)

        last_exc = None
        for busnum in candidates:
            try:
                self.logger.info(f"Trying I2C bus {busnum} for HTU21@0x{self.address:02x}")
                b = SMBus(busnum)
                try:
                    b.read_byte(self.address)
                except OSError:
                    pass
                self.bus = b
                self.bus_num = busnum
                self.logger.info(f"Opened I2C bus {busnum}")
                return
            except Exception as e:
                last_exc = e
                self.logger.debug(f"Could not open bus {busnum}: {e}")
        raise I2CError(f"Could not open any I2C bus for HTU21 (tried {candidates}) - last error: {last_exc}")

    def deinit(self) -> None:
        if self.bus is not None:
            try:
                self.bus.close()
            except Exception:
                pass
        self.bus = None
        self.bus_num = None

    def probe(self) -> bool:
        if self.bus is None:
            raise I2CError("Bus is not initialized. Call init() first.")
        try:
            # read user register as presence probe
            _ = self.read_status()
            return True
        except HTU21Error:
            return False
        except OSError:
            return False

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

        # CRC validation
        if self._crc8(bytes([h_msb, h_lsb])) != h_crc:
            raise CRCError("Humidity CRC mismatch")
        if self._crc8(bytes([t_msb, t_lsb])) != t_crc:
            raise CRCError("Temperature CRC mismatch")

        # per datasheet conversions
        rh = -6.0 + 125.0 * (hum_raw / 65536.0)
        temp_c = -46.85 + 175.72 * (temp_raw / 65536.0)
        return temp_c, rh


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
