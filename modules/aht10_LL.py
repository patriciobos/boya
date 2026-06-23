"""AHT10 low-level driver (refactored, I2C-uniformized).

This version follows the agreed stage-1 low-level conventions:
- init() prepares configuration only
- open() opens the real I2C bus
- test() performs a basic presence/communication check
- full_test() performs a richer diagnosis and scans buses before concluding
  that the device is not present
- close() closes the I2C bus
- deinit() leaves the driver in a neutral state

Notes:
- The module keeps a permissive/compatible detection strategy because some
  embedded I2C adapters do not support every smbus2 transaction style.
- Presence detection scans candidate buses before returning device_present=False.
"""

from __future__ import annotations

import argparse
import logging
import time
import os
import sys
from typing import Any, Iterable, Optional, Tuple, TYPE_CHECKING, cast

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.support.i2c_common import discover_i2c_buses
from modules.support.log_utils import get_logger

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
    """Base exception for the AHT10 driver."""


class NotFound(AHT10Error):
    """Raised when a required dependency is missing."""


class I2CError(AHT10Error):
    """Raised on low-level I2C communication errors."""


class BusyTimeout(AHT10Error):
    """Raised when the sensor remains busy beyond the timeout."""


class ProtocolError(AHT10Error):
    """Raised when the sensor response does not match expectations."""


class CRCError(AHT10Error):
    """Reserved for future CRC-aware variants of the protocol."""


class AHT10LowLevel:
    DEFAULT_ADDRESS = 0x38
    DEFAULT_BUS = 1
    MIN_PLAUSIBLE_TEMP_C = -40.0
    MAX_PLAUSIBLE_TEMP_C = 85.0
    MIN_PLAUSIBLE_RH = 0.0
    MAX_PLAUSIBLE_RH = 100.0
    INIT_CMD = bytes([0xE1, 0x08, 0x00])
    SOFT_RESET_CMD = bytes([0xBA])
    TRIGGER_CMD = bytes([0xAC, 0x33, 0x00])

    def __init__(self, logger_name: str = "aht10_LL") -> None:
        self.logger = get_logger(logger_name)
        self.bus_num: Optional[int] = None
        self.bus: Optional[SMBusType] = None
        self.address: int = self.DEFAULT_ADDRESS
        self.bus_candidates: list[int] = []
        self.bus_forced: bool = False
        self.is_initialized: bool = False
        self.is_open: bool = False
        self.last_error: Optional[str] = None

    def _require_i2c(self) -> None:
        if SMBus is None or i2c_msg is None:
            raise NotFound("smbus2 is required for AHT10 I2C operations")

    def _set_error(self, msg: str) -> None:
        self.last_error = msg

    def _clear_error(self) -> None:
        self.last_error = None

    def init(self, bus: Optional[int] = None, address: int = DEFAULT_ADDRESS) -> bool:
        """Prepare configuration only. Does not open the I2C bus."""
        self.logger.info("Initializing module")
        self._clear_error()

        try:
            self._require_i2c()
            self.close()

            self.address = int(address)
            candidates = discover_i2c_buses(
                bus if bus is not None else self.DEFAULT_BUS
            )
            self.bus_candidates = list(candidates)
            self.bus_num = int(bus) if bus is not None else None
            self.bus_forced = bus is not None
            self.is_initialized = True

            self.logger.info(
                "Module initialized: address=0x%02X bus_candidates=%s",
                self.address,
                self.bus_candidates,
            )
            return True
        except Exception as exc:
            self.is_initialized = False
            self._set_error(f"Initialization failed: {exc}")
            self.logger.exception("Initialization failed: %s", exc)
            return False

    def open(self) -> bool:
        """Open the preferred/current I2C bus only."""
        self.logger.info("Opening I2C bus")
        self._clear_error()

        if not self.is_initialized:
            self._set_error("Module is not initialized")
            self.logger.error(self.last_error)
            return False

        if self.is_open and self.bus is not None:
            self.logger.info("I2C bus already open on bus %s", self.bus_num)
            return True

        candidates: list[int]
        if self.bus_num is not None:
            candidates = [self.bus_num]
        else:
            candidates = list(self.bus_candidates)

        last_exc: Optional[Exception] = None
        for busnum in candidates:
            try:
                self.logger.info(
                    "Trying I2C bus %s for AHT10@0x%02X", busnum, self.address
                )
                if SMBus is None:
                    raise NotFound(
                        "smbus2.SMBus is not available. Did the import fail?"
                    )
                self.bus = SMBus(busnum)
                self.bus_num = busnum
                self.is_open = True
                self.logger.info("Opened I2C bus %s", busnum)
                return True
            except Exception as exc:
                last_exc = exc
                self.logger.debug("Could not open I2C bus %s: %s", busnum, exc)

        self.bus = None
        self.is_open = False
        self._set_error(
            f"Could not open any I2C bus for AHT10 (tried {candidates}) - last error: {last_exc}"
        )
        self.logger.error(self.last_error)
        return False

    def close(self) -> bool:
        """Close the current I2C bus."""
        self.logger.info("Closing I2C bus")
        try:
            if self.bus is not None:
                try:
                    self.bus.close()
                except Exception as exc:
                    self._set_error(f"Error while closing I2C bus: {exc}")
                    self.logger.debug("Ignoring close error: %s", exc)
                finally:
                    self.bus = None
            self.is_open = False
            return True
        except Exception as exc:
            self._set_error(f"Unexpected close failure: {exc}")
            self.logger.exception("Unexpected close failure: %s", exc)
            return False

    def deinit(self) -> bool:
        """Release resources and reset lifecycle state."""
        self.logger.info("Deinitializing module")
        ok = self.close()
        self.is_initialized = False
        self.bus_num = None
        self.bus_forced = False
        self.bus_candidates = []
        return ok

    def _require_open_bus(self) -> SMBusType:
        self._require_i2c()
        if not self.is_open or self.bus is None:
            raise I2CError("I2C bus is not open. Call open() first.")
        return cast(SMBusType, self.bus)

    def _write_bytes(self, payload: bytes) -> None:
        bus = self._require_open_bus()
        assert i2c_msg is not None
        try:
            write = i2c_msg.write(self.address, payload)
            bus.i2c_rdwr(write)
        except OSError as exc:
            raise I2CError(f"I2C error during write({payload!r}): {exc}") from exc

    def _read_raw(self, n: int) -> bytes:
        bus = self._require_open_bus()
        assert i2c_msg is not None
        try:
            r = i2c_msg.read(self.address, n)
            bus.i2c_rdwr(r)
            return bytes(list(cast(Iterable[int], r)))
        except OSError as exc:
            raise I2CError(f"I2C error during raw read({n}): {exc}") from exc

    def _read_raw_with_retries(
        self, n: int, max_attempts: int = 3, delay_s: float = 0.02
    ) -> bytes:
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                data = self._read_raw(n)
                if len(data) == n:
                    return data
                self.logger.warning(
                    "I2C raw read returned %s/%s bytes on attempt %s",
                    len(data),
                    n,
                    attempt,
                )
            except Exception as exc:
                last_exc = exc
                self.logger.debug(
                    "I2C raw read attempt %s failed: %s",
                    attempt,
                    exc,
                )
            time.sleep(delay_s)

        if last_exc is not None:
            raise ProtocolError(
                f"Could not read {n} bytes from AHT10: {last_exc}"
            ) from last_exc
        raise ProtocolError(
            f"Could not read {n} bytes from AHT10 after {max_attempts} attempts"
        )

    def initialize_sensor(self) -> bool:
        """Try the standard AHT10 initialization/calibration command."""
        try:
            self._write_bytes(self.INIT_CMD)
            time.sleep(0.04)
            return True
        except AHT10Error as exc:
            self.logger.debug(
                "AHT10 initialize command failed on bus %s: %s", self.bus_num, exc
            )
            return False

    def reset(self) -> bool:
        """Attempt a soft reset."""
        try:
            self._write_bytes(self.SOFT_RESET_CMD)
            time.sleep(0.03)
            return True
        except AHT10Error as exc:
            self._set_error(str(exc))
            self.logger.error("Soft reset failed: %s", exc)
            return False

    def trigger_measurement(self) -> None:
        """Send the standard AHT10 measurement trigger command."""
        self._write_bytes(self.TRIGGER_CMD)

    def read_status(self) -> int:
        """Read a status byte using progressively more compatible fallbacks."""
        bus = self._require_open_bus()

        # Fallback 1: simple SMBus byte read
        try:
            return int(bus.read_byte(self.address))
        except Exception as exc1:
            self.logger.debug(
                "read_status: read_byte failed on bus %s: %s", self.bus_num, exc1
            )

        # Fallback 2: raw read(1)
        try:
            data = self._read_raw(1)
            if data:
                return data[0]
        except Exception as exc2:
            self.logger.debug(
                "read_status: raw read(1) failed on bus %s: %s", self.bus_num, exc2
            )

        # Fallback 3: trigger + 6-byte payload, first byte is status
        try:
            self.trigger_measurement()
            time.sleep(0.10)
            data6 = self._read_raw(6)
            if len(data6) >= 1:
                return data6[0]
        except Exception as exc3:
            self.logger.debug(
                "read_status: trigger+read6 fallback failed on bus %s: %s",
                self.bus_num,
                exc3,
            )

        raise ProtocolError("Could not read AHT10 status using any supported method")

    def is_busy(self, status: Optional[int] = None) -> bool:
        if status is None:
            status = self.read_status()
        return bool(status & 0x80)

    def is_calibrated(self, status: Optional[int] = None) -> bool:
        if status is None:
            status = self.read_status()
        return bool(status & 0x08)

    def read_measurement_raw(
        self, timeout: float = 1.0, retry_on_null: bool = True
    ) -> bytes:
        """Trigger and read a standard 6-byte AHT10 measurement."""
        for attempt in range(1, 3 if retry_on_null else 2):
            self.logger.debug("AHT10 measurement attempt %s", attempt)
            self.trigger_measurement()
            start = time.time()

            while True:
                time.sleep(0.10)
                data = self._read_raw_with_retries(6, max_attempts=2, delay_s=0.02)
                if len(data) < 6:
                    if time.time() - start > timeout:
                        raise BusyTimeout(f"Expected 6 bytes from AHT10, got {data!r}")
                    time.sleep(0.02)
                    continue

                status = data[0]
                if not self.is_busy(status):
                    break

                if (time.time() - start) > timeout:
                    raise BusyTimeout("Timeout waiting for AHT10 measurement readiness")

                time.sleep(0.02)

            payload_is_null = all(x == 0x00 for x in data[1:6])
            if payload_is_null:
                self.logger.warning(
                    "Null AHT10 payload detected on attempt %s: %r",
                    attempt,
                    data,
                )
                if attempt < 2 and retry_on_null:
                    time.sleep(0.05)
                    continue
                raise ProtocolError("AHT10 returned a null measurement payload")

            return data

        raise ProtocolError("AHT10 measurement failed after multiple attempts")

    def parse(self, raw: bytes) -> Tuple[float, float]:
        """Parse a standard 6-byte AHT10 measurement into (temp_C, rh_pct)."""
        if not raw or len(raw) < 6:
            raise ProtocolError("Raw measurement must be at least 6 bytes")

        if raw[1:] == b"\x00\x00\x00\x00\x00":
            raise ProtocolError("AHT10 returned an all-zero payload")

        b = list(raw)
        hum_raw = ((b[1] << 16) | (b[2] << 8) | b[3]) >> 4
        temp_raw = ((b[3] & 0x0F) << 16) | (b[4] << 8) | b[5]

        rh = (hum_raw * 100.0) / float(1 << 20)
        temp_c = (temp_raw * 200.0) / float(1 << 20) - 50.0

        if not (
            self.MIN_PLAUSIBLE_TEMP_C <= temp_c <= self.MAX_PLAUSIBLE_TEMP_C
            and self.MIN_PLAUSIBLE_RH <= rh <= self.MAX_PLAUSIBLE_RH
        ):
            self.logger.warning(
                "Parsed AHT10 values out of expected range: temp=%s humidity=%s raw=%r",
                temp_c,
                rh,
                raw,
            )

        return temp_c, rh

    def _probe_current_bus(self) -> tuple[bool, list[str], dict]:
        """Try several compatibility checks on the currently-open bus."""
        errors: list[str] = []
        details: dict[str, Any] = {
            "bus": self.bus_num,
            "address": f"0x{self.address:02X}",
            "checks": [],
        }

        # Check 1: simple read_byte
        try:
            bus = self._require_open_bus()
            value = int(bus.read_byte(self.address))
            details["checks"].append({"name": "read_byte", "ok": True, "value": value})
            return True, errors, details
        except Exception as exc:
            msg = f"read_byte failed: {exc}"
            errors.append(msg)
            details["checks"].append(
                {"name": "read_byte", "ok": False, "error": str(exc)}
            )

        # Check 2: init/calibration command, then read status
        init_ok = self.initialize_sensor()
        details["checks"].append({"name": "initialize_sensor", "ok": init_ok})
        try:
            status = self.read_status()
            details["checks"].append(
                {"name": "read_status", "ok": True, "status": status}
            )
            return True, errors, details
        except Exception as exc:
            msg = f"read_status failed: {exc}"
            errors.append(msg)
            details["checks"].append(
                {"name": "read_status", "ok": False, "error": str(exc)}
            )

        # Check 3: full trigger+read(6)+parse
        try:
            raw = self.read_measurement_raw(timeout=1.5, retry_on_null=True)
            temp_c, rh = self.parse(raw)
            details["checks"].append(
                {
                    "name": "trigger_read_parse",
                    "ok": True,
                    "raw": [int(x) for x in raw],
                    "temperature_c": temp_c,
                    "humidity_rh": rh,
                }
            )
            return True, errors, details
        except Exception as exc:
            msg = f"trigger_read_parse failed: {exc}"
            errors.append(msg)
            details["checks"].append(
                {"name": "trigger_read_parse", "ok": False, "error": str(exc)}
            )

        return False, errors, details

    def _scan_for_device(self) -> tuple[bool, list[str], dict]:
        """Scan candidate buses before concluding the device is absent."""
        scan_details: dict[str, Any] = {
            "address": f"0x{self.address:02X}",
            "scanned_buses": [],
            "selected_bus": None,
        }
        errors: list[str] = []

        candidates = (
            list(self.bus_candidates)
            if self.bus_candidates
            else discover_i2c_buses(self.DEFAULT_BUS)
        )
        if self.bus_num is not None and self.bus_num not in candidates:
            candidates = [self.bus_num] + candidates

        original_bus_num = self.bus_num
        original_was_open = self.is_open

        if original_was_open:
            self.close()

        for busnum in candidates:
            bus_entry: dict[str, Any] = {"bus": busnum}
            scan_details["scanned_buses"].append(bus_entry)

            self.bus_num = busnum
            if not self.open():
                bus_entry["open_ok"] = False
                bus_entry["error"] = self.last_error
                errors.append(f"bus {busnum}: open failed: {self.last_error}")
                continue

            bus_entry["open_ok"] = True
            present, probe_errors, probe_details = self._probe_current_bus()
            bus_entry["probe"] = probe_details
            errors.extend([f"bus {busnum}: {e}" for e in probe_errors])

            if present:
                scan_details["selected_bus"] = busnum
                return True, errors, scan_details

            self.close()

        self.bus_num = original_bus_num
        if original_was_open and original_bus_num is not None:
            self.open()

        return False, errors, scan_details

    def probe(self) -> bool:
        """Public probe against the currently-open bus only."""
        try:
            present, _, _ = self._probe_current_bus()
            return present
        except Exception as exc:
            self.logger.debug("Probe failed on bus %s: %s", self.bus_num, exc)
            return False

    def test(self) -> bool:
        """Run a basic presence test; scans buses before returning False."""
        self.logger.info("Running basic test")
        self._clear_error()

        if not self.is_initialized:
            self._set_error("Module is not initialized")
            self.logger.error(self.last_error)
            return False

        present, errors, _ = self._scan_for_device()
        if not present and errors:
            self._set_error("; ".join(errors[-3:]))
        return present

    def full_test(self) -> tuple[bool, dict]:
        """Run a full diagnostic and scan buses before declaring absence."""
        self.logger.info("Running full test")
        self._clear_error()

        details: dict[str, Any] = {
            "initialized": self.is_initialized,
            "opened": self.is_open,
            "device_present": False,
            "errors": [],
            "details": {},
        }

        if not self.is_initialized:
            msg = "Module is not initialized"
            details["errors"].append(msg)
            self._set_error(msg)
            self.logger.error(msg)
            return False, details

        present, scan_errors, scan_details = self._scan_for_device()
        details["opened"] = self.is_open
        details["device_present"] = present
        details["details"]["scan"] = scan_details
        details["errors"].extend(scan_errors)

        if not present:
            msg = "Basic probe failed after scanning all candidate I2C buses"
            details["errors"].append(msg)
            self._set_error(msg)
            self.logger.error("AHT10 full_test failed: %s", details)
            return False, details

        # At this point a bus is selected and open.
        try:
            status = self.read_status()
            details["details"]["status"] = {
                "value": status,
                "busy": self.is_busy(status),
                "calibrated": self.is_calibrated(status),
            }

            measurements = []
            for attempt in range(1, 4):
                raw = self.read_measurement_raw(timeout=2.0, retry_on_null=True)
                temp_c, rh = self.parse(raw)
                measurement = {
                    "attempt": attempt,
                    "raw": [int(x) for x in raw],
                    "temperature_c": temp_c,
                    "humidity_rh": rh,
                    "plausible": (
                        self.MIN_PLAUSIBLE_TEMP_C <= temp_c <= self.MAX_PLAUSIBLE_TEMP_C
                        and self.MIN_PLAUSIBLE_RH <= rh <= self.MAX_PLAUSIBLE_RH
                    ),
                }
                measurements.append(measurement)
                if measurement["plausible"]:
                    details["details"]["measurement"] = measurement
                    details["details"]["measurement_attempts"] = measurements
                    return True, details
                time.sleep(0.1)

            details["details"]["measurement"] = measurements[-1]
            details["details"]["measurement_attempts"] = measurements
            details["errors"].append(
                "Measurement values are outside plausible AHT10 range"
            )
            self.logger.error(
                "AHT10 measurement outside plausible range: %s",
                details["details"]["measurement"],
            )
            return False, details
        except Exception as exc:
            details["errors"].append(str(exc))
            self._set_error(str(exc))
            self.logger.exception("AHT10 full_test measurement stage failed: %s", exc)
            return False, details

    def __repr__(self) -> str:
        return (
            f"AHT10LowLevel(address=0x{self.address:02X}, bus_num={self.bus_num}, "
            f"is_initialized={self.is_initialized}, is_open={self.is_open})"
        )


__all__ = [
    "AHT10LowLevel",
    "AHT10Error",
    "NotFound",
    "I2CError",
    "BusyTimeout",
    "ProtocolError",
    "CRCError",
]


def _run_self_test(
    bus: Optional[int] = None, address: int = AHT10LowLevel.DEFAULT_ADDRESS
) -> int:
    logger = logging.getLogger("aht10_LL")
    drv = AHT10LowLevel()

    try:
        logger.info("Starting AHT10 self-test")
        if not drv.init(bus=bus, address=address):
            logger.error("AHT10 init failed: %s", drv.last_error)
            return 1

        result, details = drv.full_test()
        if not result:
            logger.error("AHT10 self-test failed: %s", details)
            return 2

        logger.info("AHT10 self-test passed: %s", details)
        return 0
    except NotFound as exc:
        logger.error("Missing dependency: %s", exc)
        return 3
    except Exception as exc:  # pragma: no cover
        logger.exception("Unexpected error during AHT10 self-test: %s", exc)
        return 5
    finally:
        try:
            drv.deinit()
        except Exception:
            pass


def main(argv: Optional[list[str]] = None) -> bool:
    import json

    parser = argparse.ArgumentParser(description="AHT10 low-level driver self-test")
    parser.add_argument(
        "--bus", "-b", type=int, default=None, help="Preferred I2C bus number"
    )
    parser.add_argument(
        "--address",
        "-a",
        type=lambda x: int(x, 0),
        default=AHT10LowLevel.DEFAULT_ADDRESS,
        help="I2C device address (default: 0x38)",
    )
    args = parser.parse_args(argv)

    logger = get_logger("aht10_LL")
    logger.info("Starting AHT10 self-test")

    ll = AHT10LowLevel()
    if not ll.init(bus=args.bus, address=args.address):
        report = {
            "success": False,
            "initialized": False,
            "opened": False,
            "device_present": False,
            "errors": [ll.last_error] if ll.last_error else [],
            "details": {},
        }
        logger.error("AHT10 self-test failed: initialization")
        print(json.dumps(report, indent=2))
        return False

    ok, report = ll.full_test()
    report["success"] = bool(ok)
    if ok:
        logger.info("AHT10 self-test succeeded")
    else:
        logger.error("AHT10 self-test failed")

    print(json.dumps(report, indent=2, default=str))

    ll.deinit()
    return bool(ok)


if __name__ == "__main__":
    import sys

    raise SystemExit(0 if main(sys.argv[1:]) else 1)
