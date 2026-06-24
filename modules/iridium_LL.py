"""modules/iridium_LL.py

Low-level Iridium modem driver with uniform lifecycle.

Public lifecycle:
- init() -> bool
- open() -> bool
- close() -> bool
- test() -> bool
- full_test() -> tuple[bool, dict]
- deinit() -> bool

Functional helpers preserved/adapted from the original module:
- send_command()
- check_status()
- probe()
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

import serial

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.support.log_utils import get_logger


class IridiumError(Exception):
    """Base exception for the Iridium driver."""


class NotFound(IridiumError):
    """Raised when a required dependency or device is missing."""


class TransportError(IridiumError):
    """Raised on serial transport errors."""


class ProtocolError(IridiumError):
    """Raised when the modem response is invalid."""


class IridiumLowLevel:
    DEFAULT_PORT_CANDIDATES = [f"/dev/ttyS{i}" for i in range(7)]  # ttyS0 .. ttyS6
    DEFAULT_PREFERRED_PORT = "/dev/ttyS2"
    DEFAULT_BAUDRATE = 19200
    DEFAULT_TIMEOUT = 1.0

    def __init__(
        self,
        logger_name: str = "iridium_LL",
        preferred_port: Optional[str] = DEFAULT_PREFERRED_PORT,
        baudrate: int = DEFAULT_BAUDRATE,
        timeout: float = DEFAULT_TIMEOUT,
        show_ports: bool = False,
    ) -> None:
        self.logger = get_logger(logger_name)

        self.is_initialized: bool = False
        self.is_open: bool = False
        self.last_error: Optional[str] = None

        self.bus = None
        self.bus_num = None
        self.address = None
        self.bus_candidates: List[str] = []
        self.bus_forced: bool = False

        self.serial_port: Optional[serial.Serial] = None
        self.port: Optional[str] = preferred_port
        self.port_candidates: List[str] = []
        self.preferred_port: Optional[str] = (
            preferred_port or self.DEFAULT_PREFERRED_PORT
        )
        self.baudrate: int = int(baudrate)
        self.timeout: float = float(timeout)
        self.show_ports: bool = bool(show_ports)

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
        if self.serial_port is not None and self.serial_port is not ser:
            try:
                if self.serial_port.is_open:
                    self.serial_port.close()
            except Exception:
                pass

        self.serial_port = ser
        self.bus = ser
        self.port = port_name
        self.is_open = True

    def _read_command_response(
        self, command: str, timeout: float = 1.0
    ) -> Optional[dict]:
        if (
            self.serial_port is None
            or not self.serial_port.is_open
            or self.port is None
        ):
            raise TransportError("Serial port is not open")

        self.serial_port.reset_input_buffer()
        self.serial_port.write((command + "\r\n").encode())

        response_bytes = b""
        start = time.time()
        status = ""

        while time.time() - start < timeout:
            chunk = self.serial_port.read(256)
            if chunk:
                response_bytes += chunk
                if b"OK" in response_bytes:
                    status = "OK"
                    break
                if b"ERROR" in response_bytes:
                    status = "ERROR"
                    break
            else:
                time.sleep(0.01)

        elapsed = time.time() - start
        if not response_bytes:
            self.logger.warning(
                "[send_command] Timeout (%.3fs) without response for '%s'",
                timeout,
                command,
            )
            return None

        response = response_bytes.decode("utf-8", errors="replace")
        lines = [line.strip() for line in response.splitlines() if line.strip()]

        echo = lines[0] if lines and lines[0] == command else ""
        status_line = ""
        if lines and lines[-1] in ("OK", "ERROR"):
            status_line = lines[-1]
            payload_lines = lines[1:-1] if echo else lines[:-1]
        else:
            payload_lines = lines[1:] if echo else lines[:]

        payload = "\n".join(payload_lines) if payload_lines else ""

        self.logger.info(
            "[send_command] command=%s elapsed=%.3fs echo=%r status=%r payload=%r",
            command,
            elapsed,
            echo,
            status or status_line,
            payload,
        )
        return {
            "echo": echo,
            "payload": payload,
            "status": status or status_line,
            "raw": response,
            "elapsed_s": elapsed,
        }

    def _probe_current_port_details(self) -> dict:
        response = self._read_command_response("AT", timeout=max(1.0, self.timeout))
        device_present = bool(response and response.get("status") == "OK")
        return {
            "command": "AT",
            "response": response,
            "device_present": device_present,
        }

    def _collect_identity(self) -> dict:
        model = self.send_command("AT+CGMM", 2.0)
        firmware = self.send_command("AT+CGMR", 2.0)
        return {
            "model": model.get("payload", "") if model else "",
            "firmware": firmware.get("payload", "") if firmware else "",
        }

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

    def init(
        self,
        preferred_port: Optional[str] = None,
        baudrate: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> bool:
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
        self.logger.info("Opening serial transport")
        self._clear_error()

        if not self.is_initialized:
            self._set_error("Module is not initialized")
            self.logger.error(self.last_error)
            return False

        if self.is_open and self.serial_port is not None and self.serial_port.is_open:
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

        self.serial_port = None
        self.bus = None
        self.port = None
        self.is_open = False
        self._set_error(f"Open failed: {last_exc}" if last_exc else "Open failed")
        self.logger.error(self.last_error)
        return False

    def close(self) -> bool:
        self.logger.info("Closing serial transport")
        self._clear_error()

        try:
            if self.serial_port is not None:
                try:
                    if self.serial_port.is_open:
                        self.serial_port.close()
                except Exception as exc:
                    self.logger.warning("Serial close warning: %s", exc)

            self.serial_port = None
            self.bus = None
            self.port = None
            self.is_open = False
            return True

        except Exception as exc:
            self.serial_port = None
            self.bus = None
            self.port = None
            self.is_open = False
            self._set_error(f"Close failed: {exc}")
            self.logger.exception("Close failed: %s", exc)
            return False

    def deinit(self) -> bool:
        self.logger.info("Deinitializing module")
        self._clear_error()

        try:
            self.close()
            self.is_initialized = False
            self.bus_candidates = []
            self.port_candidates = []
            return True
        except Exception as exc:
            self._set_error(f"Deinitialization failed: {exc}")
            self.logger.exception("Deinitialization failed: %s", exc)
            return False

    def send_command(self, command: str, timeout: float = 1.0) -> Optional[dict]:
        try:
            return self._read_command_response(command, timeout=timeout)
        except Exception as exc:
            self.logger.error("Error sending command %s: %s", command, exc)
            return None

    def probe(self) -> bool:
        self.logger.info("Probing Iridium modem")
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
        self.logger.info("Running smoke test")
        self._clear_error()

        was_open = (
            self.is_open and self.serial_port is not None and self.serial_port.is_open
        )
        temporarily_opened = False
        original_serial = self.serial_port
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
                self.serial_port = original_serial
                self.bus = original_bus
                self.port = original_port
                self.is_open = True

    def full_test(self) -> tuple[bool, dict]:
        self.logger.info("Running full diagnostic test")
        self._clear_error()

        report = self._build_full_test_report()
        was_open = (
            self.is_open and self.serial_port is not None and self.serial_port.is_open
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
                report["device_present"] = False

            if report["device_present"]:
                try:
                    report["details"]["identity"] = self._collect_identity()
                except Exception as exc:
                    report["errors"].append(f"Identity read failed: {exc}")

                try:
                    report["details"]["status"] = self.check_status()
                except Exception as exc:
                    report["errors"].append(f"Status read failed: {exc}")

            try:
                testfile = "iridium_test_perm.txt"
                with open(testfile, "w", encoding="utf-8") as f:
                    f.write("test")
                os.remove(testfile)
                fs_ok = True
            except Exception as exc:
                fs_ok = False
                report["errors"].append(f"Filesystem write check failed: {exc}")

            try:
                statvfs = os.statvfs(".")
                free_bytes = statvfs.f_frsize * statvfs.f_bavail
            except Exception as exc:
                free_bytes = 0
                report["errors"].append(f"Disk space check failed: {exc}")

            report["details"]["transport"] = {
                "port": self.port,
                "baudrate": self.baudrate,
                "timeout": self.timeout,
                "bus_forced": self.bus_forced,
                "preferred_port": self.preferred_port,
                "candidates": list(self.port_candidates),
            }

            report["details"]["filesystem"] = {
                "write_ok": fs_ok,
                "free_bytes": free_bytes,
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

        # ------------------------------------------------------------------

    # SBD send API
    # ------------------------------------------------------------------

    RECOVERABLE_MO_STATUS = {10, 17, 18, 19, 32, 35, 36, 38}
    SUCCESS_MO_STATUS = {0, 1, 2}

    @staticmethod
    def _sbd_checksum(payload: bytes) -> bytes:
        checksum = sum(payload) & 0xFFFF
        return bytes([(checksum >> 8) & 0xFF, checksum & 0xFF])

    @staticmethod
    def _parse_sbdix_payload(payload: str) -> dict:
        result = {
            "raw": payload,
            "parsed": False,
            "mo_status": None,
            "momsn": None,
            "mt_status": None,
            "mtmsn": None,
            "mt_len": None,
            "mt_queued": None,
        }

        try:
            line = ""
            for candidate in payload.splitlines():
                candidate = candidate.strip()
                if candidate.startswith("+SBDIX:"):
                    line = candidate
                    break

            if not line:
                return result

            values = [v.strip() for v in line.split(":", 1)[1].split(",")]
            if len(values) != 6:
                return result

            result.update(
                {
                    "parsed": True,
                    "mo_status": int(values[0]),
                    "momsn": int(values[1]),
                    "mt_status": int(values[2]),
                    "mtmsn": int(values[3]),
                    "mt_len": int(values[4]),
                    "mt_queued": int(values[5]),
                }
            )
            return result

        except Exception:
            return result

    @staticmethod
    def _mo_status_text(code: Optional[int]) -> str:
        mapping = {
            0: "MO message transferred successfully",
            1: "MO transferred, MT message too large",
            2: "MO transferred, location update not accepted",
            10: "GSS reported call did not complete in allowed time",
            11: "MO queue at GSS is full",
            12: "MO message has too many segments",
            13: "GSS reported session did not complete",
            14: "Invalid segment size",
            15: "Access denied",
            16: "ISU locked",
            17: "Gateway not responding",
            18: "Connection lost",
            19: "Link failure",
            32: "No network service",
            33: "Antenna fault",
            34: "Radio disabled",
            35: "ISU busy",
            36: "Try later, registration wait",
            37: "SBD service temporarily disabled",
            38: "Try later, traffic management",
            64: "Band violation",
            65: "PLL lock failure",
        }
        if code is None:
            return "Unknown MO status"

        return mapping.get(code, "Unknown MO status")

    def _send_sbdix_once(self, timeout: float = 90.0) -> dict:
        resp = self.send_command("AT+SBDIX", timeout=timeout)
        if not resp:
            return {
                "ok": False,
                "recoverable": True,
                "error": "No response to AT+SBDIX",
                "response": None,
                "sbdix": None,
            }

        parsed = self._parse_sbdix_payload(resp.get("payload", ""))
        mo_status = parsed.get("mo_status")
        parsed["mo_status_text"] = self._mo_status_text(mo_status)

        ok = bool(parsed.get("parsed") and mo_status in self.SUCCESS_MO_STATUS)
        recoverable = bool(mo_status in self.RECOVERABLE_MO_STATUS or mo_status is None)

        return {
            "ok": ok,
            "recoverable": recoverable,
            "error": None if ok else parsed["mo_status_text"],
            "response": resp,
            "sbdix": parsed,
        }

    def _send_sbdix_with_retries(
        self,
        max_attempts: int = 3,
        retry_delay_s: float = 10.0,
        timeout: float = 90.0,
    ) -> dict:
        attempts = []

        for attempt_no in range(1, max_attempts + 1):
            self.logger.info("Starting SBDIX attempt %s/%s", attempt_no, max_attempts)

            result = self._send_sbdix_once(timeout=timeout)
            result["attempt"] = attempt_no
            attempts.append(result)

            if result.get("ok"):
                return {
                    "ok": True,
                    "attempts": attempts,
                    "final": result,
                }

            if not result.get("recoverable"):
                return {
                    "ok": False,
                    "attempts": attempts,
                    "final": result,
                }

            if attempt_no < max_attempts:
                self.logger.info(
                    "SBDIX attempt failed with recoverable error: %s. Retrying in %.1fs",
                    result.get("error"),
                    retry_delay_s,
                )
                time.sleep(retry_delay_s)

        return {
            "ok": False,
            "attempts": attempts,
            "final": attempts[-1] if attempts else None,
        }

    def _clear_mo_buffer(self) -> dict:
        resp = self.send_command("AT+SBDD0", timeout=5.0)
        ok = bool(resp and resp.get("status") == "OK")
        return {
            "ok": ok,
            "response": resp,
        }

    def _read_until(self, needle: bytes, timeout: float) -> bytes:
        if self.serial_port is None or not self.serial_port.is_open:
            raise TransportError("Serial port is not open")

        deadline = time.time() + timeout
        data = b""

        while time.time() < deadline:
            chunk = self.serial_port.read(1)
            if chunk:
                data += chunk
                if needle in data:
                    return data
            else:
                time.sleep(0.01)

        return data

    def _ensure_open_for_operation(self) -> tuple[bool, bool]:
        """
        Returns (ok, temporarily_opened).
        """
        was_open = (
            self.is_open and self.serial_port is not None and self.serial_port.is_open
        )
        if was_open:
            return True, False

        if not self.open():
            return False, False

        return True, True

    def send_sbd_text(
        self,
        message: str,
        clear_after_success: bool = True,
        max_attempts: int = 3,
        retry_delay_s: float = 10.0,
        session_timeout: float = 90.0,
    ) -> tuple[bool, dict]:
        """
        Send a Mobile-Originated SBD text message.

        Intended for FSM use.
        Retries are short technical retries only; long scheduling belongs to the FSM.
        """
        self.logger.info("Sending SBD text message")
        self._clear_error()

        report = {
            "mode": "text",
            "input_size_bytes": 0,
            "buffer_write": None,
            "session": None,
            "clear_mo_buffer": None,
            "errors": [],
        }

        ok_open, temporarily_opened = self._ensure_open_for_operation()
        if not ok_open:
            report["errors"].append(self.last_error or "Could not open modem")
            return False, report

        try:
            payload = message.encode("utf-8")
            report["input_size_bytes"] = len(payload)

            if not payload:
                report["errors"].append("Text payload is empty")
                return False, report

            if len(payload) > 120:
                report["errors"].append(
                    "Text payload exceeds 120 bytes for inline AT+SBDWT; use send_sbd_binary()"
                )
                return False, report

            write_resp = self.send_command(f"AT+SBDWT={message}", timeout=10.0)
            write_ok = bool(write_resp and write_resp.get("status") == "OK")
            report["buffer_write"] = {
                "ok": write_ok,
                "response": write_resp,
            }

            if not write_ok:
                report["errors"].append("AT+SBDWT failed")
                return False, report

            session = self._send_sbdix_with_retries(
                max_attempts=max_attempts,
                retry_delay_s=retry_delay_s,
                timeout=session_timeout,
            )
            report["session"] = session

            success = bool(session.get("ok"))
            if not success:
                final = session.get("final") or {}
                report["errors"].append(final.get("error") or "AT+SBDIX failed")
                return False, report

            if clear_after_success:
                report["clear_mo_buffer"] = self._clear_mo_buffer()

            return True, report

        except Exception as exc:
            self._set_error(f"SBD text send failed: {exc}")
            self.logger.exception("SBD text send failed: %s", exc)
            report["errors"].append(str(exc))
            return False, report

        finally:
            if temporarily_opened:
                self.close()

    def send_sbd_binary(
        self,
        payload: bytes,
        clear_after_success: bool = True,
        max_attempts: int = 3,
        retry_delay_s: float = 10.0,
        session_timeout: float = 90.0,
        ready_timeout: float = 5.0,
    ) -> tuple[bool, dict]:
        """
        Send a Mobile-Originated SBD binary message.

        Intended for FSM use.
        Retries are short technical retries only; long scheduling belongs to the FSM.
        """
        self.logger.info("Sending SBD binary message")
        self._clear_error()

        report = {
            "mode": "binary",
            "input_size_bytes": len(payload) if payload else 0,
            "checksum": None,
            "ready_response": None,
            "buffer_write": None,
            "session": None,
            "clear_mo_buffer": None,
            "errors": [],
        }

        ok_open, temporarily_opened = self._ensure_open_for_operation()
        if not ok_open:
            report["errors"].append(self.last_error or "Could not open modem")
            return False, report

        try:
            if self.serial_port is None or not self.serial_port.is_open:
                raise TransportError("Serial port is not open")

            if not payload:
                report["errors"].append("Binary payload is empty")
                return False, report

            if len(payload) > 340:
                report["errors"].append("Binary payload exceeds 340 bytes")
                return False, report

            checksum = self._sbd_checksum(payload)
            report["checksum"] = {
                "hex": checksum.hex(),
                "sum16": sum(payload) & 0xFFFF,
            }

            self.serial_port.reset_input_buffer()
            self.serial_port.write(f"AT+SBDWB={len(payload)}\r".encode("ascii"))
            self.serial_port.flush()

            ready = self._read_until(b"READY", timeout=ready_timeout)
            report["ready_response"] = ready.decode(errors="replace")

            if b"READY" not in ready:
                report["errors"].append("AT+SBDWB did not return READY")
                return False, report

            self.serial_port.write(payload + checksum)
            self.serial_port.flush()

            raw_resp = self._read_until(b"OK", timeout=10.0)
            decoded_resp = raw_resp.decode(errors="replace")

            write_ok = False
            result_code = None

            for line in decoded_resp.splitlines():
                line = line.strip()
                if line in ("0", "1", "2", "3"):
                    result_code = int(line)
                    write_ok = result_code == 0
                    break

            report["buffer_write"] = {
                "ok": write_ok,
                "result_code": result_code,
                "response": decoded_resp,
            }

            if not write_ok:
                report["errors"].append(f"AT+SBDWB failed with code {result_code}")
                return False, report

            session = self._send_sbdix_with_retries(
                max_attempts=max_attempts,
                retry_delay_s=retry_delay_s,
                timeout=session_timeout,
            )
            report["session"] = session

            success = bool(session.get("ok"))
            if not success:
                final = session.get("final") or {}
                report["errors"].append(final.get("error") or "AT+SBDIX failed")
                return False, report

            if clear_after_success:
                report["clear_mo_buffer"] = self._clear_mo_buffer()

            return True, report

        except Exception as exc:
            self._set_error(f"SBD binary send failed: {exc}")
            self.logger.exception("SBD binary send failed: %s", exc)
            report["errors"].append(str(exc))
            return False, report

        finally:
            if temporarily_opened:
                self.close()

    def check_status(self) -> dict:
        status_info: Dict[str, Any] = {}

        if not self.serial_port or not self.serial_port.is_open:
            self.logger.error("Cannot query status: serial port is not open")
            return {"error": "Serial port is not open"}

        try:
            rssi_resp = self.send_command("AT+CSQ")
            rssi = rssi_resp["payload"] if rssi_resp else ""
            status_info["csq"] = rssi
            self.logger.info("[check_status] Signal strength (CSQ): %s", rssi)
        except Exception as exc:
            status_info["csq"] = f"Error: {exc}"
            self.logger.error("[check_status] Error reading CSQ: %s", exc)

        try:
            creg_resp = self.send_command("AT+CREG?")
            creg = creg_resp["payload"] if creg_resp else ""
            status_info["creg"] = creg
            self.logger.info("[check_status] Network registration (CREG): %s", creg)
        except Exception as exc:
            status_info["creg"] = f"Error: {exc}"
            self.logger.error("[check_status] Error reading CREG: %s", exc)

        try:
            ant_resp = self.send_command("AT+ANTST")
            ant = ant_resp["payload"] if ant_resp else ""
            status_info["antena"] = ant
            self.logger.info("[check_status] Antenna status (ANTST): %s", ant)
        except Exception as exc:
            status_info["antena"] = f"Error: {exc}"
            self.logger.warning("[check_status] Could not query ANTST: %s", exc)

        try:
            sbdix_resp = self.send_command("AT+SBDIX", timeout=7.0)
            sbdix = sbdix_resp["payload"] if sbdix_resp else ""
            status_info["sbdix"] = sbdix
            self.logger.info("[check_status] SBD mailbox state (SBDIX): %s", sbdix)
        except Exception as exc:
            status_info["sbdix"] = f"Error: {exc}"
            self.logger.error("[check_status] Error reading SBDIX: %s", exc)

        return status_info


def main(argv=None) -> bool:
    preferred_port = os.getenv("PREFERRED_PORT", IridiumLowLevel.DEFAULT_PREFERRED_PORT)
    try:
        baudrate = int(
            os.getenv("IRIDIUM_BAUDRATE", str(IridiumLowLevel.DEFAULT_BAUDRATE))
        )
    except Exception:
        baudrate = IridiumLowLevel.DEFAULT_BAUDRATE

    try:
        timeout = float(
            os.getenv("IRIDIUM_TIMEOUT", str(IridiumLowLevel.DEFAULT_TIMEOUT))
        )
    except Exception:
        timeout = IridiumLowLevel.DEFAULT_TIMEOUT

    modem = IridiumLowLevel(
        preferred_port=preferred_port,
        baudrate=baudrate,
        timeout=timeout,
        show_ports=True,
    )
    modem.logger.info("Starting Iridium self-test")

    if not modem.init():
        report = {
            "success": False,
            "initialized": False,
            "opened": False,
            "device_present": False,
            "errors": [modem.last_error] if modem.last_error else [],
            "details": {},
        }
        modem.logger.error("Iridium self-test failed: initialization")
        modem.logger.error("Initialization report=%s", json.dumps(report, default=str))
        print(json.dumps(report, indent=2, default=str))
        return False

    ok, report = modem.full_test()
    report["success"] = bool(ok)
    if ok:
        modem.logger.info("Iridium self-test succeeded")
    else:
        modem.logger.error("Iridium self-test failed")
    print(json.dumps(report, indent=2, default=str))
    modem.deinit()
    return bool(ok)


if __name__ == "__main__":
    ok = main()
    raise SystemExit(0 if ok else 1)
