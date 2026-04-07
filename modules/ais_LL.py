"""modules/ais_LL.py

Low-level AIS/GPS serial driver with uniform lifecycle.

Public lifecycle:
- init() -> bool
- open() -> bool
- close() -> bool
- test() -> bool
- full_test() -> tuple[bool, dict]
- deinit() -> bool

Functional helpers:
- probe() -> bool
- parse_nmea()
- get_navigation()
- has_fix()
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import serial

# allow execution as script from repo root
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.support.log_utils import get_logger


def _nmea_coord_to_decimal(coord: str, hemi: str) -> Optional[float]:
    try:
        if not coord:
            return None

        parts = coord.split(".")
        if len(parts) < 2:
            return None

        head = parts[0]
        if len(head) < 4:
            return None

        # Latitude normally ddmm.mmmm, longitude dddmm.mmmm.
        # Infer by hemisphere when possible, otherwise fall back to width.
        if hemi in ("N", "S"):
            degrees_len = 2
        elif hemi in ("E", "W"):
            degrees_len = 3
        else:
            degrees_len = 3 if len(head) > 4 else 2

        deg = int(coord[:degrees_len])
        minutes = float(coord[degrees_len:])
        dec = deg + minutes / 60.0

        if hemi in ("S", "W"):
            dec = -dec

        return dec
    except Exception:
        return None


def _nmea_validate_checksum(line: str) -> bool:
    """Validate NMEA/AIS checksum. Returns False if checksum is missing."""
    try:
        if "*" not in line:
            return False

        body, chk = line.strip().split("*", 1)
        if body.startswith("$") or body.startswith("!"):
            body = body[1:]

        calc = 0
        for c in body:
            calc ^= ord(c)

        chk_int = int(chk.strip()[:2], 16)
        return calc == chk_int
    except Exception:
        return False


class AISLowLevel:
    """
    Low-level AIS/GPS serial driver with uniform lifecycle.
    """

    DEFAULT_PORT_CANDIDATES = [f"/dev/ttyS{i}" for i in range(6)]  # ttyS0 .. ttyS5
    DEFAULT_PREFERRED_PORT = "/dev/ttyS3"
    DEFAULT_BAUD_CANDIDATES = [115200]
    DEFAULT_SCAN_WINDOW = 6.0
    DEFAULT_WAIT_FOR_FIX = 20.0

    def __init__(
        self,
        logger_name: str = "ais_LL",
        preferred_port: Optional[str] = None,
        scan_window: float = DEFAULT_SCAN_WINDOW,
        wait_for_fix: float = DEFAULT_WAIT_FOR_FIX,
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
        self.serial: Optional[serial.Serial] = None
        self.port: Optional[str] = None
        self.baud: Optional[int] = None
        self.port_candidates: List[str] = []
        self.baud_candidates: List[int] = list(self.DEFAULT_BAUD_CANDIDATES)
        self._buffer: List[str] = []

        # configuration
        self.preferred_port = preferred_port or os.getenv("PREFERRED_PORT") or self.DEFAULT_PREFERRED_PORT

        try:
            self.scan_window = max(1.0, float(scan_window)) if scan_window is not None else self.DEFAULT_SCAN_WINDOW
        except Exception:
            self.scan_window = self.DEFAULT_SCAN_WINDOW

        try:
            self.wait_for_fix = max(1.0, float(wait_for_fix)) if wait_for_fix is not None else self.DEFAULT_WAIT_FOR_FIX
        except Exception:
            self.wait_for_fix = self.DEFAULT_WAIT_FOR_FIX

        self.show_ports = bool(show_ports)

        # navigation state
        self.nav: Dict[str, Any] = {
            "lat": None,
            "lon": None,
            "timestamp": None,
            "fix": False,
            "fix_quality": 0,
            "num_sats": 0,
            "hdop": None,
            "satellites_in_view": {},
            "used_sats": [],
        }

    # ---------------------------------------------------------------------
    # internal helpers
    # ---------------------------------------------------------------------

    def _set_error(self, msg: str) -> None:
        self.last_error = msg

    def _clear_error(self) -> None:
        self.last_error = None

    def _reset_navigation(self) -> None:
        self.nav = {
            "lat": None,
            "lon": None,
            "timestamp": None,
            "fix": False,
            "fix_quality": 0,
            "num_sats": 0,
            "hdop": None,
            "satellites_in_view": {},
            "used_sats": [],
        }

    def _extract_sentence_tag(self, line: str) -> str:
        try:
            return re.split(r"[,*]", line[1:])[0]
        except Exception:
            return ""

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

    def _adopt_open_serial(self, ser: serial.Serial, port_name: str, baud: int) -> None:
        if self.serial is not None and self.serial is not ser:
            try:
                if self.serial.is_open:
                    self.serial.close()
            except Exception:
                pass

        self.serial = ser
        self.bus = ser
        self.port = port_name
        self.baud = baud
        self.is_open = True

    def _wake_device(self, ser: serial.Serial) -> None:
        try:
            ser.reset_input_buffer()
        except Exception:
            pass

        try:
            ser.write(b"\r")
            ser.flush()
        except Exception:
            pass

        # Give the device time to wake up, but do NOT clear the buffer again.
        time.sleep(0.5)

    def _open_serial_port(self, port_name: str, baud: int) -> serial.Serial:
        return serial.Serial(
            port_name,
            baudrate=baud,
            timeout=0.25,
            bytesize=8,
            parity="N",
            stopbits=1,
        )
    
    def _build_full_test_report(self) -> dict:
        return {
            "initialized": self.is_initialized,
            "opened": self.is_open,
            "device_present": False,
            "errors": [],
            "details": {},
        }

    def _probe_current_port(self, timeout: Optional[float] = None) -> dict:
        """
        Short presence probe.
        Confirms AIS/NMEA traffic on the currently open port.
        Uses buffered byte reads instead of relying only on readline().
        """
        if self.serial is None or not self.serial.is_open:
            raise RuntimeError("Serial port is not open")

        probe_timeout = max(5.0, float(timeout if timeout is not None else self.scan_window))

        result = {
            "lines_seen": 0,
            "valid_nmea_lines": 0,
            "valid_ais_lines": 0,
            "sentence_types": [],
            "navigation": self.get_navigation(),
            "device_present": False,
            "has_fix": False,
        }

        seen_tags: set[str] = set()
        start = time.time()
        buffer = b""

        self._wake_device(self.serial)

        while time.time() - start < probe_timeout:
            try:
                waiting = self.serial.in_waiting
            except Exception:
                waiting = 0

            try:
                if waiting > 0:
                    chunk = self.serial.read(waiting)
                else:
                    chunk = self.serial.read(1)
            except Exception:
                chunk = b""

            if not chunk:
                time.sleep(0.02)
                continue

            buffer += chunk

            while b"\n" in buffer:
                raw_line, buffer = buffer.split(b"\n", 1)

                try:
                    line = raw_line.decode(errors="ignore").strip()
                except Exception:
                    continue

                if not line:
                    continue

                result["lines_seen"] += 1

                if line.startswith("!AIVDO") or line.startswith("!AIVDM"):
                    if _nmea_validate_checksum(line):
                        result["valid_ais_lines"] += 1
                        result["device_present"] = True
                        seen_tags.add(self._extract_sentence_tag(line))

                elif line.startswith("$") and _nmea_validate_checksum(line):
                    result["valid_nmea_lines"] += 1
                    tag = self._extract_sentence_tag(line)
                    seen_tags.add(tag)

                    self.parse_nmea(line)
                    nav = self.get_navigation()
                    result["navigation"] = nav

                    if tag in (
                        "GPRMC", "GNRMC",
                        "GPGGA", "GNGGA",
                        "GPGLL", "GNGLL",
                        "GPGSA", "GNGSA",
                        "GPGSV", "GNGSV",
                        "GPTXT", "GNTXT",
                    ):
                        result["device_present"] = True

                    if nav.get("fix") or (
                        nav.get("lat") is not None and nav.get("lon") is not None
                    ):
                        result["has_fix"] = True
                        result["device_present"] = True

                if result["device_present"] and result["has_fix"]:
                    break

            if result["device_present"] and result["has_fix"]:
                break

        result["sentence_types"] = sorted(seen_tags)

        self.logger.info(
            "Presence probe finished on %s: lines=%s valid_nmea=%s valid_ais=%s present=%s fix=%s",
            self.port,
            result["lines_seen"],
            result["valid_nmea_lines"],
            result["valid_ais_lines"],
            result["device_present"],
            result["has_fix"],
        )

        return result

    def _wait_for_fix_on_current_port(self, timeout: Optional[float] = None) -> dict:
        """
        Longer second-phase read on the already selected port.
        Waits for GPS fix while continuing to accept AIS/NMEA traffic.
        Uses buffered byte reads instead of readline().
        """
        if self.serial is None or not self.serial.is_open:
            raise RuntimeError("Serial port is not open")

        wait_timeout = float(timeout if timeout is not None else self.wait_for_fix)

        result = {
            "lines_seen": 0,
            "valid_nmea_lines": 0,
            "valid_ais_lines": 0,
            "sentence_types": [],
            "navigation": self.get_navigation(),
            "device_present": False,
            "has_fix": False,
        }

        seen_tags: set[str] = set()
        start = time.time()
        buffer = b""

        while time.time() - start < wait_timeout:
            try:
                waiting = self.serial.in_waiting
            except Exception:
                waiting = 0

            try:
                if waiting > 0:
                    chunk = self.serial.read(waiting)
                else:
                    chunk = self.serial.read(1)
            except Exception:
                chunk = b""

            if not chunk:
                time.sleep(0.02)
                continue

            buffer += chunk

            while b"\n" in buffer:
                raw_line, buffer = buffer.split(b"\n", 1)

                try:
                    line = raw_line.decode(errors="ignore").strip()
                except Exception:
                    continue

                if not line:
                    continue

                result["lines_seen"] += 1

                if line.startswith("!AIVDO") or line.startswith("!AIVDM"):
                    if _nmea_validate_checksum(line):
                        result["valid_ais_lines"] += 1
                        result["device_present"] = True
                        seen_tags.add(self._extract_sentence_tag(line))

                elif line.startswith("$") and _nmea_validate_checksum(line):
                    result["valid_nmea_lines"] += 1
                    tag = self._extract_sentence_tag(line)
                    seen_tags.add(tag)

                    self.parse_nmea(line)
                    nav = self.get_navigation()
                    result["navigation"] = nav

                    if tag in (
                        "GPRMC", "GNRMC",
                        "GPGGA", "GNGGA",
                        "GPGLL", "GNGLL",
                        "GPGSA", "GNGSA",
                        "GPGSV", "GNGSV",
                        "GPTXT", "GNTXT",
                    ):
                        result["device_present"] = True

                    if nav.get("fix") or (
                        nav.get("lat") is not None and nav.get("lon") is not None
                    ):
                        result["has_fix"] = True
                        result["device_present"] = True
                        break

            if result["has_fix"]:
                break

        result["sentence_types"] = sorted(seen_tags)

        self.logger.info(
            "Wait-for-fix finished on %s: lines=%s valid_nmea=%s valid_ais=%s present=%s fix=%s",
            self.port,
            result["lines_seen"],
            result["valid_nmea_lines"],
            result["valid_ais_lines"],
            result["device_present"],
            result["has_fix"],
        )

        return result

    # ---------------------------------------------------------------------
    # lifecycle
    # ---------------------------------------------------------------------

    def init(
        self,
        preferred_port: Optional[str] = None,
        baud_candidates: Optional[List[int]] = None,
        scan_window: Optional[float] = None,
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

            if scan_window is not None:
                self.scan_window = max(1.0, float(scan_window))

            if baud_candidates is None:
                baud_candidates = list(self.DEFAULT_BAUD_CANDIDATES)

            self.baud_candidates = [int(b) for b in baud_candidates]

            self.port_candidates = self._resolve_port_candidates(self.preferred_port)

            # preferred first, but still fallback to the rest
            self.bus_forced = False
            self.bus_candidates = list(self.port_candidates)

            self._buffer.clear()
            self._reset_navigation()
            self.is_initialized = True

            self.logger.info(
                "Module initialized: preferred_port=%s candidates=%s baud_candidates=%s",
                self.preferred_port,
                self.port_candidates,
                self.baud_candidates,
            )
            return True

        except Exception as exc:
            self.is_initialized = False
            self._set_error(f"Initialization failed: {exc}")
            self.logger.exception("Initialization failed: %s", exc)
            return False

    def open(self) -> bool:
        """
        Open the serial transport.

        Order:
        1. preferred port first
        2. remaining ttyS candidates
        """
        self.logger.info("Opening serial transport")
        self._clear_error()

        if not self.is_initialized:
            self._set_error("Module is not initialized")
            self.logger.error(self.last_error)
            return False

        if self.is_open and self.serial is not None and self.serial.is_open:
            self.logger.info("Serial transport already open on %s", self.port)
            return True

        last_exc: Optional[Exception] = None

        for port_name in self.port_candidates:
            for baud in self.baud_candidates:
                try:
                    self.logger.info("Trying port=%s baud=%s", port_name, baud)
                    ser = self._open_serial_port(port_name, baud)
                    self._adopt_open_serial(ser, port_name, baud)
                    self.logger.info("Serial transport opened on %s @ %s", port_name, baud)
                    return True
                except Exception as exc:
                    last_exc = exc
                    if self.show_ports:
                        self.logger.warning(
                            "Failed to open port=%s baud=%s: %s",
                            port_name,
                            baud,
                            exc,
                        )

        self.serial = None
        self.bus = None
        self.port = None
        self.baud = None
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
            if self.serial is not None:
                try:
                    if self.serial.is_open:
                        self.serial.close()
                except Exception as exc:
                    self.logger.warning("Serial close warning: %s", exc)

            self.serial = None
            self.bus = None
            self.port = None
            self.baud = None
            self.is_open = False
            return True

        except Exception as exc:
            self.serial = None
            self.bus = None
            self.port = None
            self.baud = None
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

            self.bus_candidates = []
            self.port_candidates = []
            self.is_initialized = False
            self.is_open = False
            self._buffer.clear()
            self._reset_navigation()

            return True

        except Exception as exc:
            self._set_error(f"Deinitialization failed: {exc}")
            self.logger.exception("Deinitialization failed: %s", exc)
            return False

    # ---------------------------------------------------------------------
    # probe / tests
    # ---------------------------------------------------------------------

    def probe(self) -> bool:
        """
        Minimal presence/communication check on the currently open port.
        """
        self.logger.info("Probing AIS/GPS device")
        self._clear_error()

        try:
            probe = self._probe_current_port(timeout=max(self.scan_window, 5.0))
            return bool(probe.get("device_present", False))
        except Exception as exc:
            self._set_error(f"Probe failed: {exc}")
            self.logger.warning("Probe failed: %s", exc)
            return False

    def test(self) -> bool:
        """
        Fast smoke test.
        Can open temporarily and restores original state.
        """
        self.logger.info("Running smoke test")
        self._clear_error()

        was_open = self.is_open and self.serial is not None and self.serial.is_open
        original_serial = self.serial
        original_bus = self.bus
        original_port = self.port
        original_baud = self.baud
        temporarily_opened = False

        try:
            if not was_open:
                if not self.open():
                    return False
                temporarily_opened = True

            probe = self._probe_current_port(timeout=max(self.scan_window, 5.0))
            result = bool(probe.get("device_present", False))
            self.logger.info("Smoke test completed: success=%s port=%s", result, self.port)
            return result

        except Exception as exc:
            self._set_error(f"Test failed: {exc}")
            self.logger.warning("Test failed: %s", exc)
            return False

        finally:
            if temporarily_opened:
                self.close()
            elif was_open:
                self.serial = original_serial
                self.bus = original_bus
                self.port = original_port
                self.baud = original_baud
                self.is_open = True

    def full_test(self) -> tuple[bool, dict]:
        """
        Full diagnostic test.
        Never propagates uncaught exceptions.
        """
        self.logger.info("Running full diagnostic test")
        self._clear_error()

        report = self._build_full_test_report()
        original_serial = self.serial
        original_bus = self.bus
        original_port = self.port
        original_baud = self.baud
        original_is_open = self.is_open and self.serial is not None and self.serial.is_open

        scan_details: List[dict] = []

        try:
            report["initialized"] = self.is_initialized
            if not self.is_initialized:
                msg = "Module is not initialized"
                report["errors"].append(msg)
                self._set_error(msg)
                return False, report

            selected_ser: Optional[serial.Serial] = None
            selected_port: Optional[str] = None
            selected_baud: Optional[int] = None
            selected_probe: Optional[dict] = None

            for port_name in self.port_candidates:
                port_info = {
                    "port": port_name,
                    "baud_attempts": [],
                }

                for baud in self.baud_candidates:
                    attempt_info = {
                        "baud": baud,
                        "open_ok": False,
                    }
                    ser = None

                    try:
                        ser = self._open_serial_port(port_name, baud)
                        attempt_info["open_ok"] = True

                        # temporary adoption for parsing/probing
                        temp_old_serial = self.serial
                        temp_old_bus = self.bus
                        temp_old_port = self.port
                        temp_old_baud = self.baud
                        temp_old_is_open = self.is_open

                        self.serial = ser
                        self.bus = ser
                        self.port = port_name
                        self.baud = baud
                        self.is_open = True

                        self._reset_navigation()
                        probe = self._probe_current_port(timeout=max(self.scan_window, 5.0))
                        attempt_info["probe"] = probe

                        # restore temporary previous state
                        self.serial = temp_old_serial
                        self.bus = temp_old_bus
                        self.port = temp_old_port
                        self.baud = temp_old_baud
                        self.is_open = temp_old_is_open

                        port_info["baud_attempts"].append(attempt_info)

                        if probe.get("device_present", False):
                            selected_ser = ser
                            selected_port = port_name
                            selected_baud = baud
                            selected_probe = probe
                            break

                        try:
                            ser.close()
                        except Exception:
                            pass

                    except Exception as exc:
                        attempt_info["error"] = str(exc)
                        port_info["baud_attempts"].append(attempt_info)

                        if self.show_ports:
                            self.logger.warning(
                                "Port %s @ %s failed: %s",
                                port_name,
                                baud,
                                exc,
                            )

                        if ser is not None:
                            try:
                                ser.close()
                            except Exception:
                                pass

                scan_details.append(port_info)

                if selected_ser is not None:
                    break

            fix_probe = None

            if selected_ser is not None and selected_port is not None and selected_baud is not None:
                self._adopt_open_serial(selected_ser, selected_port, selected_baud)
                report["opened"] = True
                report["device_present"] = True

                try:
                    fix_probe = self._wait_for_fix_on_current_port(timeout=self.wait_for_fix)
                except Exception as exc:
                    report["errors"].append(f"Wait for fix failed: {exc}")
                    fix_probe = None
            else:
                report["opened"] = False
                report["device_present"] = False
                report["errors"].append("No AIS/GPS traffic detected on candidate ports")

            report["details"]["scan"] = {
                "scanned_ports": scan_details,
                "selected_port": selected_port,
            }

            report["details"]["transport"] = {
                "port": self.port,
                "baudrate": self.baud,
                "bus_forced": self.bus_forced,
                "candidates": list(self.port_candidates),
            }

            if fix_probe is not None:
                report["details"]["navigation"] = fix_probe.get("navigation", {})
                report["details"]["has_fix"] = bool(fix_probe.get("has_fix", False))
                report["details"]["lines_collected"] = int(
                    selected_probe.get("lines_seen", 0) + fix_probe.get("lines_seen", 0)
                )
                report["details"]["wait_for_fix_s"] = self.wait_for_fix
                report["details"]["presence_probe"] = selected_probe
                report["details"]["fix_probe"] = fix_probe
            elif selected_probe is not None:
                report["details"]["navigation"] = selected_probe.get("navigation", {})
                report["details"]["has_fix"] = bool(selected_probe.get("has_fix", False))
                report["details"]["lines_collected"] = int(selected_probe.get("lines_seen", 0))
                report["details"]["wait_for_fix_s"] = self.wait_for_fix
                report["details"]["presence_probe"] = selected_probe
            else:
                report["details"]["navigation"] = self.get_navigation()
                report["details"]["has_fix"] = self.has_fix()
                report["details"]["lines_collected"] = 0
                report["details"]["wait_for_fix_s"] = self.wait_for_fix

            success = bool(report["device_present"])

            self.logger.info(
                "Full diagnostic test completed: success=%s selected_port=%s transport_port=%s has_fix=%s lines=%s",
                success,
                report["details"].get("scan", {}).get("selected_port"),
                report["details"].get("transport", {}).get("port"),
                report["details"].get("has_fix"),
                report["details"].get("lines_collected"),
            )

            return success, report

        except Exception as exc:
            report["errors"].append(f"Unexpected full_test failure: {exc}")
            self._set_error(f"Full test failed: {exc}")
            self.logger.exception("Full test failed: %s", exc)
            self.logger.error("Full diagnostic test completed with failure")
            return False, report

        finally:
            if not report["device_present"]:
                if original_is_open and original_serial is not None:
                    self.serial = original_serial
                    self.bus = original_bus
                    self.port = original_port
                    self.baud = original_baud
                    self.is_open = True
                else:
                    self.close()

    # ---------------------------------------------------------------------
    # NMEA/AIS parsing helpers
    # ---------------------------------------------------------------------

    def parse_nmea(self, line: str) -> None:
        """
        Parse NMEA line and update internal navigation state.
        """
        try:
            line = line.strip()
            if not line.startswith("$"):
                return

            if not _nmea_validate_checksum(line):
                return

            body = line[1:].split("*", 1)[0]
            parts = body.split(",")
            if not parts:
                return

            tag = parts[0]

            if tag in ("GPRMC", "GNRMC"):
                if len(parts) >= 10:
                    status = parts[2]
                    lat = _nmea_coord_to_decimal(parts[3], parts[4]) if len(parts) > 4 else None
                    lon = _nmea_coord_to_decimal(parts[5], parts[6]) if len(parts) > 6 else None

                    ts_val = None
                    try:
                        hhmmss = parts[1]
                        ddmmyy = parts[9]
                        if hhmmss and ddmmyy:
                            ts_val = datetime.strptime(ddmmyy + hhmmss.split(".")[0], "%d%m%y%H%M%S")
                    except Exception:
                        ts_val = None

                    self.nav.update(
                        {
                            "lat": lat if lat is not None else self.nav.get("lat"),
                            "lon": lon if lon is not None else self.nav.get("lon"),
                            "timestamp": ts_val if ts_val is not None else self.nav.get("timestamp"),
                            "fix": status == "A",
                        }
                    )

            elif tag in ("GPGGA", "GNGGA"):
                if len(parts) >= 9:
                    lat = _nmea_coord_to_decimal(parts[2], parts[3]) if len(parts) > 3 else None
                    lon = _nmea_coord_to_decimal(parts[4], parts[5]) if len(parts) > 5 else None

                    try:
                        fix_q = int(parts[6]) if parts[6] else 0
                    except Exception:
                        fix_q = 0

                    try:
                        num_sats = int(parts[7]) if parts[7] else 0
                    except Exception:
                        num_sats = 0

                    try:
                        hdop = float(parts[8]) if parts[8] else None
                    except Exception:
                        hdop = None

                    self.nav.update(
                        {
                            "lat": lat if lat is not None else self.nav.get("lat"),
                            "lon": lon if lon is not None else self.nav.get("lon"),
                            "fix_quality": fix_q,
                            "num_sats": num_sats,
                            "hdop": hdop,
                            "fix": fix_q > 0,
                        }
                    )

            elif tag in ("GPGLL", "GNGLL"):
                if len(parts) >= 7:
                    lat = _nmea_coord_to_decimal(parts[1], parts[2]) if len(parts) > 2 else None
                    lon = _nmea_coord_to_decimal(parts[3], parts[4]) if len(parts) > 4 else None
                    status = parts[6] if len(parts) > 6 else ""

                    self.nav.update(
                        {
                            "lat": lat if lat is not None else self.nav.get("lat"),
                            "lon": lon if lon is not None else self.nav.get("lon"),
                            "fix": status == "A" or self.nav.get("fix", False),
                        }
                    )

            elif tag in ("GPGSA", "GNGSA"):
                used = [p for p in parts[3:15] if p]

                hdop = None
                if len(parts) > 16 and parts[16]:
                    try:
                        hdop = float(parts[16])
                    except Exception:
                        hdop = None

                self.nav["used_sats"] = used
                if hdop is not None:
                    self.nav["hdop"] = hdop

            elif tag in ("GPGSV", "GNGSV"):
                try:
                    blocks = parts[4:]
                    for i in range(0, len(blocks), 4):
                        if i + 3 < len(blocks):
                            prn = blocks[i]
                            snr = blocks[i + 3]
                            if prn:
                                try:
                                    self.nav["satellites_in_view"][prn] = int(snr) if snr and snr.isdigit() else None
                                except Exception:
                                    self.nav["satellites_in_view"][prn] = None
                except Exception:
                    pass

        except Exception as exc:
            self.logger.debug("Error parsing NMEA: %s -- %s", exc, line)

    def get_navigation(self) -> Dict[str, Any]:
        return {
            "lat": self.nav.get("lat"),
            "lon": self.nav.get("lon"),
            "timestamp": self.nav.get("timestamp"),
            "fix": self.nav.get("fix", False),
            "fix_quality": self.nav.get("fix_quality", 0),
            "num_sats": self.nav.get("num_sats", 0),
            "hdop": self.nav.get("hdop"),
            "satellites_in_view": dict(self.nav.get("satellites_in_view", {})),
            "used_sats": list(self.nav.get("used_sats", [])),
        }

    def has_fix(self) -> bool:
        nav = self.get_navigation()
        return bool(
            nav.get("fix")
            or nav.get("fix_quality", 0) > 0
            or (nav.get("lat") is not None and nav.get("lon") is not None)
        )

    def read_lines(self, seconds: float = 1.0) -> List[str]:
        """
        Read raw lines from the current serial transport for a bounded duration.
        """
        lines: List[str] = []

        if self.serial is None or not self.serial.is_open:
            return lines

        end_time = time.time() + max(0.1, float(seconds))
        while time.time() < end_time:
            try:
                raw = self.serial.readline()
            except Exception:
                raw = b""

            if not raw:
                continue

            try:
                line = raw.decode(errors="ignore").strip()
            except Exception:
                line = ""

            if line:
                lines.append(line)

        return lines


def main(argv=None) -> bool:
    preferred = os.getenv("PREFERRED_PORT", "/dev/ttyS3")

    try:
        scan_window = float(os.getenv("SCAN_WINDOW", str(AISLowLevel.DEFAULT_SCAN_WINDOW)))
    except Exception:
        scan_window = AISLowLevel.DEFAULT_SCAN_WINDOW

    try:
        wait_for_fix = float(os.getenv("WAIT_FOR_FIX", str(AISLowLevel.DEFAULT_WAIT_FOR_FIX)))
    except Exception:
        wait_for_fix = AISLowLevel.DEFAULT_WAIT_FOR_FIX

    ll = AISLowLevel(
        preferred_port=preferred,
        scan_window=scan_window,
        wait_for_fix=wait_for_fix,
        show_ports=True,
    )

    if not ll.init():
        print(
            json.dumps(
                {
                    "initialized": False,
                    "opened": False,
                    "device_present": False,
                    "errors": [ll.last_error] if ll.last_error else [],
                    "details": {},
                },
                indent=2,
                default=str,
            )
        )
        return False

    ok, report = ll.full_test()
    print(json.dumps(report, indent=2, default=str))
    return bool(ok)


if __name__ == "__main__":
    ok = main()
    raise SystemExit(0 if ok else 1)