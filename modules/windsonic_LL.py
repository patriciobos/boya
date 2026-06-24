"""modules/windsonic_LL.py

Low-level WindSonic serial driver with uniform lifecycle.

Public lifecycle:
- init() -> bool
- open() -> bool
- close() -> bool
- test() -> bool
- full_test() -> tuple[bool, dict]
- deinit() -> bool

Functional helpers preserved/adapted from the original module:
- config()
- acquire()
- is_acquisition_done()
- verify_checksum()
- parse_data()
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional

import serial

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.support.log_utils import get_logger

STX = "\x02"
ETX = "\x03"


class WindsonicError(Exception):
    """Base exception for the Windsonic driver."""


class NotFound(WindsonicError):
    """Raised when a required dependency or device is missing."""


class TransportError(WindsonicError):
    """Raised on serial transport errors."""


class ProtocolError(WindsonicError):
    """Raised when the Windsonic response is invalid."""


class WindsonicLowLevel:
    DEFAULT_PORT_CANDIDATES = [f"/dev/ttyS{i}" for i in range(6)]  # ttyS0 .. ttyS5
    DEFAULT_PREFERRED_PORT = "/dev/ttyS1"
    DEFAULT_BAUDRATE = 9600
    DEFAULT_TIMEOUT = 1.0
    DEFAULT_IDENTIFICATION = "Q"

    def __init__(
        self,
        logger_name: str = "windsonic_LL",
        preferred_port: Optional[str] = None,
        baudrate: int = DEFAULT_BAUDRATE,
        timeout: float = DEFAULT_TIMEOUT,
        samples: int = 10,
        spacing: float = 1.0,
        identification: str = DEFAULT_IDENTIFICATION,
        show_ports: bool = False,
    ) -> None:
        self.logger = get_logger(logger_name)

        # standard lifecycle state
        self.is_initialized: bool = False
        self.is_open: bool = False
        self.last_error: Optional[str] = None

        # standard transport state
        self.bus = None
        self.bus_num = None
        self.address = None
        self.bus_candidates: List[str] = []
        self.bus_forced: bool = False

        # serial-specific state
        self.serial_connection: Optional[serial.Serial] = None
        self.port: Optional[str] = None
        self.port_candidates: List[str] = []
        self.baudrate: int = int(baudrate)
        self.timeout: float = float(timeout)
        self.preferred_port: Optional[str] = (
            preferred_port or self.DEFAULT_PREFERRED_PORT
        )
        self.show_ports: bool = bool(show_ports)

        # functional state preserved from original module
        self.samples: int = int(samples)
        self.spacing: float = float(spacing)
        self.identification: str = str(identification)
        self.acquisition_thread: Optional[threading.Thread] = None
        self.is_acquiring: bool = False
        self.last_acquisition_ok: bool = False
        self.last_samples: List[dict] = []

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _set_error(self, msg: str) -> None:
        self.last_error = msg

    def _clear_error(self) -> None:
        self.last_error = None

    def _resolve_port_candidates(self, preferred_port: Optional[str]) -> List[str]:
        candidates = list(self.DEFAULT_PORT_CANDIDATES)

        if preferred_port is None:
            return candidates

        preferred_port = str(preferred_port).strip()
        if preferred_port not in candidates:
            raise ValueError(
                f"Invalid preferred_port '{preferred_port}'. Allowed values: {candidates}"
            )

        return [preferred_port] + [p for p in candidates if p != preferred_port]

    def _open_serial(self, port_name: str) -> serial.Serial:
        return serial.Serial(
            port_name,
            self.baudrate,
            timeout=self.timeout,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
        )

    def _adopt_serial(self, ser: serial.Serial, port_name: str) -> None:
        if self.serial_connection is not None and self.serial_connection is not ser:
            try:
                if self.serial_connection.is_open:
                    self.serial_connection.close()
            except Exception:
                pass

        self.serial_connection = ser
        self.bus = ser
        self.port = port_name
        self.is_open = True

    def _query_once(self) -> str:
        if self.serial_connection is None or not self.serial_connection.is_open:
            raise TransportError("Serial connection is not open")

        try:
            self.serial_connection.reset_input_buffer()
        except Exception:
            pass

        # Preserve original handshake.
        self.serial_connection.write((self.identification + "?").encode())
        self.serial_connection.write(self.identification.encode())

        raw = self.serial_connection.readline()
        if not raw:
            return ""
        return raw.decode(errors="ignore").strip()

    def _probe_current_port_details(self) -> dict:
        response = self._query_once()
        checksum_ok = self.verify_checksum(response)
        parsed = self.parse_data(response) if checksum_ok else None

        device_present = False
        fields: List[str] = []
        if parsed:
            fields = parsed.split(",")
            if len(fields) >= 1 and fields[0] == self.identification:
                device_present = True

        return {
            "response": response,
            "checksum_ok": checksum_ok,
            "parsed": parsed,
            "fields": fields,
            "device_present": device_present,
        }

    def _read_sample(self) -> dict:
        details = self._probe_current_port_details()
        return self._sample_from_probe_details(details)

    def _build_full_test_report(self) -> dict:
        return {
            "initialized": self.is_initialized,
            "opened": self.is_open,
            "device_present": False,
            "errors": [],
            "details": {},
        }

    def _log_full_test_result(self, success: bool, report: dict) -> None:
        self.logger.info(
            "Full diagnostic test completed: success=%s port=%s device_present=%s report=%s",
            success,
            report.get("details", {}).get("transport", {}).get("port"),
            report.get("device_present"),
            json.dumps(report, default=str),
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def config(self, samples: int = 10, spacing: float = 1.0) -> None:
        self.samples = int(samples)
        self.spacing = float(spacing)

    def init(
        self,
        preferred_port: Optional[str] = None,
        baudrate: Optional[int] = None,
        timeout: Optional[float] = None,
        samples: Optional[int] = None,
        spacing: Optional[float] = None,
        identification: Optional[str] = None,
    ) -> bool:
        """
        Prepare configuration and internal state only.
        Does not touch hardware.
        """
        self.logger.info("Initializing module")
        self._clear_error()

        try:
            self.close()

            if preferred_port is not None:
                self.preferred_port = preferred_port
            if baudrate is not None:
                self.baudrate = int(baudrate)
            if timeout is not None:
                self.timeout = float(timeout)
            if samples is not None:
                self.samples = int(samples)
            if spacing is not None:
                self.spacing = float(spacing)
            if identification is not None:
                self.identification = str(identification)

            self.bus_forced = False
            self.port_candidates = self._resolve_port_candidates(self.preferred_port)
            self.bus_candidates = list(self.port_candidates)

            self.is_initialized = True

            self.logger.info(
                "Module initialized: preferred_port=%s baudrate=%s timeout=%s candidates=%s",
                self.preferred_port,
                self.baudrate,
                self.timeout,
                self.port_candidates,
            )
            return True

        except Exception as exc:
            self.is_initialized = False
            self._set_error(f"Initialization failed: {exc}")
            self.logger.exception("Initialization failed: %s", exc)
            return False

    def open(self) -> bool:
        """
        Open the serial transport only.
        Presence validation belongs to test()/full_test().
        """
        self.logger.info("Opening serial transport")
        self._clear_error()

        if not self.is_initialized:
            self._set_error("Module is not initialized")
            self.logger.error(self.last_error)
            return False

        if (
            self.is_open
            and self.serial_connection is not None
            and self.serial_connection.is_open
        ):
            self.logger.info("Serial transport already open on %s", self.port)
            return True

        last_exc: Optional[Exception] = None

        for port_name in self.port_candidates:
            if not os.path.exists(port_name):
                if self.show_ports:
                    self.logger.info("Skipping missing port %s", port_name)
                continue
            try:
                self.logger.info("Trying port %s", port_name)
                ser = self._open_serial(port_name)
                self._adopt_serial(ser, port_name)
                self.logger.info("Serial transport opened on %s", port_name)
                return True
            except Exception as exc:
                last_exc = exc
                if self.show_ports:
                    self.logger.warning("Failed to open port %s: %s", port_name, exc)

        self.serial_connection = None
        self.bus = None
        self.port = None
        self.is_open = False
        self._set_error(f"Open failed: {last_exc}" if last_exc else "Open failed")
        self.logger.error(self.last_error)
        return False

    def close(self) -> bool:
        """
        Close the serial transport.
        Idempotent.
        """
        self.logger.info("Closing serial transport")
        self._clear_error()

        try:
            if self.serial_connection is not None:
                try:
                    if self.serial_connection.is_open:
                        self.serial_connection.close()
                except Exception as exc:
                    self.logger.warning("Serial close warning: %s", exc)

            self.serial_connection = None
            self.bus = None
            self.port = None
            self.is_open = False
            return True

        except Exception as exc:
            self.serial_connection = None
            self.bus = None
            self.port = None
            self.is_open = False
            self._set_error(f"Close failed: {exc}")
            self.logger.exception("Close failed: %s", exc)
            return False

    def deinit(self) -> bool:
        """
        Total cleanup. Leaves module in a neutral state.
        """
        self.logger.info("Deinitializing module")
        self._clear_error()

        try:
            self.close()
            self.is_initialized = False
            self.bus_candidates = []
            self.port_candidates = []
            self.is_acquiring = False
            self.last_acquisition_ok = False
            return True
        except Exception as exc:
            self._set_error(f"Deinitialization failed: {exc}")
            self.logger.exception("Deinitialization failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # probe / tests
    # ------------------------------------------------------------------

    def probe(self) -> bool:
        self.logger.info("Probing Windsonic device")
        self._clear_error()

        try:
            details = self._probe_current_port_details()
            result = bool(details["device_present"])
            self.logger.info("Probe result: %s", result)
            return result
        except Exception as exc:
            self._set_error(f"Probe failed: {exc}")
            self.logger.warning("Probe failed: %s", exc)
            return False

    def test(self) -> bool:
        """
        Fast smoke test.
        May open temporarily and restores original state.
        """
        self.logger.info("Running smoke test")
        self._clear_error()

        was_open = (
            self.is_open
            and self.serial_connection is not None
            and self.serial_connection.is_open
        )
        temporarily_opened = False
        original_serial = self.serial_connection
        original_bus = self.bus
        original_port = self.port

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
            elif was_open:
                self.serial_connection = original_serial
                self.bus = original_bus
                self.port = original_port
                self.is_open = True

    def full_test(self) -> tuple[bool, dict]:
        """
        Full diagnostic test.
        Never propagates uncaught exceptions.
        """
        self.logger.info("Running full diagnostic test")
        self._clear_error()

        report = self._build_full_test_report()
        was_open = (
            self.is_open
            and self.serial_connection is not None
            and self.serial_connection.is_open
        )
        temporarily_opened = False

        try:
            report["initialized"] = self.is_initialized
            if not self.is_initialized:
                msg = "Module is not initialized"
                report["errors"].append(msg)
                self._set_error(msg)
                self._log_full_test_result(False, report)
                return False, report

            if not was_open:
                if self.open():
                    temporarily_opened = True
                    report["opened"] = True
                else:
                    report["opened"] = False
                    if self.last_error:
                        report["errors"].append(self.last_error)
                    self._log_full_test_result(False, report)
                    return False, report
            else:
                report["opened"] = True

            try:
                probe_details = self._probe_current_port_details()
                report["device_present"] = bool(probe_details["device_present"])
                report["details"]["probe"] = probe_details
            except Exception as exc:
                report["errors"].append(f"Probe failed: {exc}")
                probe_details = None
                report["device_present"] = False

            if report["device_present"] and probe_details:
                try:
                    report["details"]["sample"] = self._sample_from_probe_details(
                        probe_details
                    )
                except Exception as exc:
                    report["errors"].append(f"Sample parse failed: {exc}")

            report["details"]["transport"] = {
                "port": self.port,
                "baudrate": self.baudrate,
                "timeout": self.timeout,
                "bus_forced": self.bus_forced,
                "preferred_port": self.preferred_port,
            }

            success = bool(
                report["initialized"] and report["opened"] and report["device_present"]
            )
            self._log_full_test_result(success, report)
            return success, report

        except Exception as exc:
            report["errors"].append(f"Unexpected full_test failure: {exc}")
            self._set_error(f"Full test failed: {exc}")
            self.logger.exception("Full test failed: %s", exc)
            self._log_full_test_result(False, report)
            return False, report

        finally:
            if temporarily_opened:
                self.close()

    def _sample_from_probe_details(self, details: dict) -> dict:
        if not details.get("device_present"):
            raise ProtocolError("Invalid Windsonic response or checksum")

        fields = details.get("fields") or []

        if len(fields) < 5:
            raise ProtocolError("Incomplete Windsonic response")

        direction_raw = fields[1].strip()
        speed_raw = fields[2].strip()
        units = fields[3].strip()
        status = fields[4].strip()

        direction_deg = None
        direction_valid = False

        if direction_raw not in ("", "---"):
            direction_deg = float(direction_raw)
            direction_valid = True

        return {
            "raw": details.get("response"),
            "parsed": details.get("parsed"),
            "identification": fields[0],
            "direction_deg": direction_deg,
            "direction_valid": direction_valid,
            "speed": float(speed_raw),
            "units": units,
            "status": status,
        }

    # ------------------------------------------------------------------
    # preserved functional API
    # ------------------------------------------------------------------

    def acquire(self, num_acq: Optional[int] = None) -> bool:
        """
        Start acquisition of num_acq samples spaced by self.spacing seconds.
        Preserves the original threaded acquisition behaviour.
        """
        if self.serial_connection is None or not self.serial_connection.is_open:
            self.logger.warning("Serial connection is not open")
            return False

        if self.is_acquiring:
            self.logger.warning("Acquisition is already running")
            return False

        if num_acq is None:
            num_acq = self.samples

        self.is_acquiring = True
        self.last_acquisition_ok = False
        self.last_samples = []
        self.acquisition_thread = threading.Thread(
            target=self._acquisition_loop,
            args=(int(num_acq),),
            daemon=True,
        )
        self.acquisition_thread.start()
        return True

    def _acquisition_loop(self, num_acq: int) -> None:
        acquired = 0
        samples: List[dict] = []

        try:
            for _ in range(num_acq):
                if self.serial_connection is None or not self.serial_connection.is_open:
                    break

                try:
                    sample = self._read_sample()
                    samples.append(sample)
                    acquired += 1
                except Exception as exc:
                    self.logger.warning("Invalid response during acquisition: %s", exc)

                time.sleep(self.spacing)

            if acquired == num_acq:
                self.logger.info(
                    "Acquisition completed successfully: %s samples", acquired
                )
                self.last_acquisition_ok = True
            elif acquired > 0:
                self.logger.warning(
                    "Acquisition partially completed: %s of %s samples",
                    acquired,
                    num_acq,
                )
                self.last_acquisition_ok = True
            else:
                self.logger.error(
                    "Acquisition incomplete: %s of %s samples", acquired, num_acq
                )
                self.last_acquisition_ok = False

        except Exception as exc:
            self.logger.error("Acquisition failed: %s", exc)
            self.last_acquisition_ok = False
        finally:
            self.last_samples = samples
            self.is_acquiring = False

    def is_acquisition_done(self) -> tuple[bool, bool]:
        done = not self.is_acquiring and (
            self.acquisition_thread is None or not self.acquisition_thread.is_alive()
        )
        return done, self.last_acquisition_ok

    def verify_checksum(self, data: str) -> bool:
        """
        Verify the XOR checksum between <STX> and <ETX> according to the Gill protocol.
        Preserved from the original implementation.
        """
        stx_index = data.find(STX) + 1
        etx_index = data.find(ETX)
        if stx_index == 0 or etx_index == -1 or etx_index <= stx_index:
            return False

        data_to_check = data[stx_index:etx_index]
        try:
            checksum_received = int(data[etx_index + 1 :], 16)
        except ValueError:
            return False

        checksum_calculated = 0
        for char in data_to_check:
            checksum_calculated ^= ord(char)

        return checksum_calculated == checksum_received

    def parse_data(self, data: str) -> Optional[str]:
        """
        Extract fields between <STX> and <ETX> from a received string.
        Preserved from the original implementation.
        """
        stx_index = data.find(STX) + 1
        etx_index = data.find(ETX)
        if stx_index == 0 or etx_index == -1 or etx_index <= stx_index:
            return None
        return data[stx_index:etx_index]


def main(argv=None) -> bool:
    preferred_port = os.getenv(
        "PREFERRED_PORT", WindsonicLowLevel.DEFAULT_PREFERRED_PORT
    )

    try:
        baudrate = int(
            os.getenv("WINDSONIC_BAUDRATE", str(WindsonicLowLevel.DEFAULT_BAUDRATE))
        )
    except Exception:
        baudrate = WindsonicLowLevel.DEFAULT_BAUDRATE

    try:
        timeout = float(
            os.getenv("WINDSONIC_TIMEOUT", str(WindsonicLowLevel.DEFAULT_TIMEOUT))
        )
    except Exception:
        timeout = WindsonicLowLevel.DEFAULT_TIMEOUT

    w = WindsonicLowLevel(
        preferred_port=preferred_port,
        baudrate=baudrate,
        timeout=timeout,
        show_ports=True,
    )
    w.logger.info("Starting Windsonic self-test")

    if not w.init():
        report = {
            "success": False,
            "initialized": False,
            "opened": False,
            "device_present": False,
            "errors": [w.last_error] if w.last_error else [],
            "details": {},
        }
        w.logger.error("Windsonic self-test failed: initialization")
        w.logger.error("Initialization report=%s", json.dumps(report, default=str))
        print(json.dumps(report, indent=2, default=str))
        return False

    ok, report = w.full_test()
    report["success"] = bool(ok)
    if ok:
        w.logger.info("Windsonic self-test succeeded")
    else:
        w.logger.error("Windsonic self-test failed")
    print(json.dumps(report, indent=2, default=str))
    w.deinit()
    return bool(ok)


if __name__ == "__main__":
    ok = main()
    raise SystemExit(0 if ok else 1)
