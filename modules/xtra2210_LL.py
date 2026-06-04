"""modules/xtra2210.py

Low-level driver for the EPEVER XTRA2210 solar charge controller over RS485/serial.

Public lifecycle:
- init() -> bool
- open() -> bool
- close() -> bool
- test() -> bool
- full_test() -> tuple[bool, dict]
- deinit() -> bool

Functional helpers:
- probe() -> bool
- read_register_block()
- send_read_input_registers()
- read_pv()
- read_load()
- read_battery()
- read_temperatures()
- read_all_decoded()
"""

from __future__ import annotations

import json
import os
import struct
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import serial

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.support.log_utils import get_logger


class Xtra2210Error(Exception):
    """Base exception for the XTRA2210 driver."""


class TransportError(Xtra2210Error):
    """Raised on serial transport errors."""


class ProtocolError(Xtra2210Error):
    """Raised on Modbus protocol errors."""


class XTRA2210LowLevel:
    DEFAULT_PORT_CANDIDATES = [f"/dev/ttyS{i}" for i in range(6)]  # ttyS0 .. ttyS5
    DEFAULT_PREFERRED_PORT = "/dev/ttyS4"
    DEFAULT_BAUDRATE = 115200
    DEFAULT_SLAVE_ID = 1
    DEFAULT_TIMEOUT = 0.8
    DEFAULT_INTER_FRAME_DELAY = 0.15

    REGISTER_BLOCKS = [
        (0x3100, 2, "PV input voltage/current"),
        (0x310C, 4, "Load voltage/current/power"),
        (0x311A, 2, "Battery SOC / temperature"),
        (0x311D, 1, "Battery real rated voltage"),
        (0x3200, 2, "Battery temperature / device temperature"),
    ]

    IDENTIFICATION_REGISTERS = {
        "rated_battery_current_a": (0x3005, 1, 0x04),
        "charging_mode": (0x3008, 1, 0x04),
        "rated_load_current_a": (0x300E, 1, 0x04),
        "system_rated_voltage_v": (0x311D, 1, 0x04),
    }

    def __init__(
        self,
        logger_name: str = "xtra2210_LL",
        preferred_port: Optional[str] = None,
        baudrate: int = DEFAULT_BAUDRATE,
        slave_id: int = DEFAULT_SLAVE_ID,
        timeout: float = DEFAULT_TIMEOUT,
        inter_frame_delay: float = DEFAULT_INTER_FRAME_DELAY,
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
        self.port: Optional[str] = None
        self.port_candidates: List[str] = []
        self.preferred_port: Optional[str] = preferred_port or self.DEFAULT_PREFERRED_PORT
        self.baudrate: int = int(baudrate)
        self.slave_id: int = int(slave_id)
        self.timeout: float = float(timeout)
        self.inter_frame_delay: float = float(inter_frame_delay)
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
            port=port_name,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.timeout,
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

    def _require_open_serial(self) -> serial.Serial:
        if self.serial_port is None or not self.serial_port.is_open:
            raise TransportError("Serial port is not open")
        return self.serial_port

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

    @staticmethod
    def modbus_crc(data: bytes) -> int:
        crc = 0xFFFF
        for ch in data:
            crc ^= ch
            for _ in range(8):
                if crc & 0x0001:
                    crc = (crc >> 1) ^ 0xA001
                else:
                    crc >>= 1
        return crc & 0xFFFF

    def _build_request(self, function_code: int, start_reg: int, count: int) -> bytes:
        payload = struct.pack(">BBHH", self.slave_id, function_code, start_reg, count)
        crc = self.modbus_crc(payload)
        return payload + struct.pack("<H", crc)

    def build_read_input_registers_request(self, start_reg: int, count: int) -> bytes:
        return self._build_request(0x04, start_reg, count)

    @staticmethod
    def expected_response_length(register_count: int) -> int:
        return 1 + 1 + 1 + (2 * register_count) + 2

    def parse_modbus_response(self, resp: bytes, function_code: int, register_count: int) -> Tuple[bool, Any]:
        if len(resp) < 5:
            return False, "response too short"
        data = resp[:-2]
        rx_crc = struct.unpack("<H", resp[-2:])[0]
        calc_crc = self.modbus_crc(data)
        if rx_crc != calc_crc:
            return False, f"invalid CRC (rx=0x{rx_crc:04X}, calc=0x{calc_crc:04X})"
        if resp[0] != self.slave_id:
            return False, f"unexpected slave id ({resp[0]})"
        func = resp[1]
        if func == (function_code | 0x80):
            if len(resp) >= 5:
                exc = resp[2]
                return False, f"Modbus exception 0x{exc:02X}"
            return False, "Modbus exception"
        if func != function_code:
            return False, f"unexpected function 0x{func:02X}"
        byte_count = resp[2]
        if byte_count != register_count * 2:
            return False, f"unexpected byte count ({byte_count})"
        regs = []
        for i in range(register_count):
            off = 3 + 2 * i
            regs.append(struct.unpack(">H", resp[off:off + 2])[0])
        return True, regs

    @staticmethod
    def regs_to_u32(high_reg: int, low_reg: int) -> int:
        return ((high_reg & 0xFFFF) << 16) | (low_reg & 0xFFFF)

    def decode_block(self, start_reg: int, regs: List[int]) -> dict:
        decoded: Dict[str, Any] = {}
        if start_reg == 0x3005 and len(regs) >= 1:
            decoded["rated_battery_current_a"] = regs[0] / 100.0
        elif start_reg == 0x3008 and len(regs) >= 1:
            decoded["charging_mode_code"] = regs[0]
            decoded["charging_mode"] = {
                0x0000: "connect_disconnect",
                0x0001: "pwm",
                0x0002: "mppt",
            }.get(regs[0], f"unknown_0x{regs[0]:04X}")
        elif start_reg == 0x300E and len(regs) >= 1:
            decoded["rated_load_current_a"] = regs[0] / 100.0
        elif start_reg == 0x3100 and len(regs) >= 2:
            decoded["pv_voltage_v"] = regs[0] / 100.0
            decoded["pv_current_a"] = regs[1] / 100.0
        elif start_reg == 0x310C and len(regs) >= 4:
            decoded["load_voltage_v"] = regs[0] / 100.0
            decoded["load_current_a"] = regs[1] / 100.0
            decoded["load_power_w"] = self.regs_to_u32(regs[3], regs[2]) / 100.0
        elif start_reg == 0x311A and len(regs) >= 2:
            decoded["battery_soc_pct"] = regs[0]
            raw_temp = regs[1]
            decoded["battery_temp_c"] = raw_temp / 100.0 if raw_temp not in (0x7FFF, 0xFFFF) else None
        elif start_reg == 0x311D and len(regs) >= 1:
            decoded["system_rated_voltage_v"] = regs[0] / 100.0
        elif start_reg == 0x3200 and len(regs) >= 2:
            decoded["battery_temp_c"] = regs[0] / 100.0 if regs[0] not in (0x7FFF, 0xFFFF) else None
            decoded["device_temp_c"] = regs[1] / 100.0 if regs[1] not in (0x7FFF, 0xFFFF) else None
        return decoded

    @staticmethod
    def values_look_plausible(decoded: dict) -> bool:
        plausible = False
        if "pv_voltage_v" in decoded and 0.0 <= decoded["pv_voltage_v"] <= 200.0:
            plausible = True
        if "pv_current_a" in decoded and 0.0 <= decoded["pv_current_a"] <= 100.0:
            plausible = True
        if "load_voltage_v" in decoded and 0.0 <= decoded["load_voltage_v"] <= 100.0:
            plausible = True
        if "battery_soc_pct" in decoded and 0 <= decoded["battery_soc_pct"] <= 100:
            plausible = True
        if "system_rated_voltage_v" in decoded and decoded["system_rated_voltage_v"] in (12.0, 24.0, 36.0, 48.0):
            plausible = True
        if "device_temp_c" in decoded and decoded["device_temp_c"] is not None and -20.0 <= decoded["device_temp_c"] <= 120.0:
            plausible = True
        if "rated_battery_current_a" in decoded and 0.0 < decoded["rated_battery_current_a"] <= 100.0:
            plausible = True
        if "rated_load_current_a" in decoded and 0.0 < decoded["rated_load_current_a"] <= 100.0:
            plausible = True
        if "charging_mode" in decoded and decoded["charging_mode"] in ("connect_disconnect", "pwm", "mppt"):
            plausible = True
        return plausible

    def _send_request(self, request: bytes, function_code: int, reg_count: int) -> Tuple[bool, Any]:
        ser = self._require_open_serial()
        resp_len = self.expected_response_length(reg_count)
        ser.reset_input_buffer()
        ser.write(request)
        ser.flush()
        time.sleep(self.inter_frame_delay)
        resp = ser.read(resp_len)
        if not resp:
            return False, "timeout"
        return self.parse_modbus_response(resp, function_code, reg_count)

    def send_read_input_registers(self, start_reg: int, reg_count: int) -> Tuple[bool, Any]:
        req = self.build_read_input_registers_request(start_reg, reg_count)
        return self._send_request(req, 0x04, reg_count)

    def read_register_block(self, start_reg: int, reg_count: int, description: str = "", function_code: int = 0x04) -> Tuple[bool, Any]:
        if function_code != 0x04:
            return False, f"unsupported function code 0x{function_code:02X}"
        ok, result = self.send_read_input_registers(start_reg, reg_count)
        if not ok:
            return False, result
        decoded = self.decode_block(start_reg, result)
        return True, {
            "description": description,
            "function_code": function_code,
            "raw": result,
            "decoded": decoded,
            "plausible": self.values_look_plausible(decoded),
        }

    def read_pv(self) -> Dict[str, Any]:
        ok, result = self.read_register_block(0x3100, 2, "PV input voltage/current", function_code=0x04)
        if not ok:
            raise ProtocolError(str(result))
        return result["decoded"]

    def read_load(self) -> Dict[str, Any]:
        ok, result = self.read_register_block(0x310C, 4, "Load voltage/current/power", function_code=0x04)
        if not ok:
            raise ProtocolError(str(result))
        return result["decoded"]

    def read_battery(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        for start_reg, reg_count, description in [
            (0x311A, 2, "Battery SOC / temperature"),
            (0x311D, 1, "System rated voltage"),
        ]:
            ok, result = self.read_register_block(start_reg, reg_count, description, function_code=0x04)
            if not ok:
                raise ProtocolError(str(result))
            data.update(result["decoded"])
        return data

    def read_temperatures(self) -> Dict[str, Any]:
        ok, result = self.read_register_block(0x3200, 2, "Battery temperature / device temperature", function_code=0x04)
        if not ok:
            raise ProtocolError(str(result))
        return result["decoded"]

    def read_identity(self) -> Dict[str, Any]:
        decoded: Dict[str, Any] = {}
        for key, (start_reg, reg_count, function_code) in self.IDENTIFICATION_REGISTERS.items():
            ok, result = self.read_register_block(
                start_reg,
                reg_count,
                key.replace("_", " "),
                function_code=function_code,
            )
            if not ok:
                raise ProtocolError(str(result))
            decoded.update(result["decoded"])
        return decoded

    def read_all_decoded(self) -> Dict[str, Any]:
        return {
            "identity": self.read_identity(),
            "pv": self.read_pv(),
            "load": self.read_load(),
            "battery": self.read_battery(),
            "temperatures": self.read_temperatures(),
        }

    def _identity_matches_expected(self, decoded: Dict[str, Any]) -> bool:
        charging_mode = decoded.get("charging_mode")
        rated_battery_current_a = decoded.get("rated_battery_current_a")
        rated_load_current_a = decoded.get("rated_load_current_a")
        system_rated_voltage_v = decoded.get("system_rated_voltage_v")
        if charging_mode != "mppt":
            return False
        if rated_battery_current_a is None or not (15.0 <= rated_battery_current_a <= 25.0):
            return False
        if rated_load_current_a is None or not (1.0 <= rated_load_current_a <= 100.0):
            return False
        if system_rated_voltage_v not in (12.0, 24.0, 36.0, 48.0):
            return False
        return True

    def init(
        self,
        preferred_port: Optional[str] = None,
        baudrate: Optional[int] = None,
        slave_id: Optional[int] = None,
        timeout: Optional[float] = None,
        inter_frame_delay: Optional[float] = None,
    ) -> bool:
        self.logger.info("Initializing module")
        self._clear_error()
        try:
            self.close()
            if preferred_port is not None:
                self.preferred_port = preferred_port
            if baudrate is not None:
                self.baudrate = int(baudrate)
            if slave_id is not None:
                self.slave_id = int(slave_id)
            if timeout is not None:
                self.timeout = float(timeout)
            if inter_frame_delay is not None:
                self.inter_frame_delay = float(inter_frame_delay)
            self.bus_forced = False
            self.port_candidates = self._resolve_port_candidates(self.preferred_port)
            self.bus_candidates = list(self.port_candidates)
            self.is_initialized = True
            self.logger.info(
                "Module initialized: preferred_port=%s baudrate=%s slave_id=%s timeout=%s inter_frame_delay=%s candidates=%s",
                self.preferred_port,
                self.baudrate,
                self.slave_id,
                self.timeout,
                self.inter_frame_delay,
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

    def probe(self) -> bool:
        self.logger.info("Probing XTRA2210 controller")
        self._clear_error()
        try:
            identity = self.read_identity()
            result = self._identity_matches_expected(identity)
            self.logger.info("Probe result: %s", result)
            return result
        except Exception as exc:
            self._set_error(f"Probe failed: {exc}")
            self.logger.warning("Probe failed: %s", exc)
            return False

    def test(self) -> bool:
        self.logger.info("Running smoke test")
        self._clear_error()
        was_open = self.is_open and self.serial_port is not None and self.serial_port.is_open
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
        was_open = self.is_open and self.serial_port is not None and self.serial_port.is_open
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

            identity_blocks: Dict[str, Any] = {}
            identity_decoded: Dict[str, Any] = {}
            for key, (start_reg, reg_count, function_code) in self.IDENTIFICATION_REGISTERS.items():
                try:
                    ok, result = self.read_register_block(
                        start_reg,
                        reg_count,
                        key.replace("_", " "),
                        function_code=function_code,
                    )
                    if not ok:
                        report["errors"].append(f"identity 0x{start_reg:04X} ({key}): {result}")
                        continue
                    identity_blocks[f"0x{start_reg:04X}"] = result
                    identity_decoded.update(result.get("decoded", {}))
                except Exception as exc:
                    report["errors"].append(f"identity 0x{start_reg:04X} ({key}): {exc}")

            identity_confirmed = self._identity_matches_expected(identity_decoded)

            collected: Dict[str, Any] = {}
            successes = 0
            plausible_hits = 0
            for start_reg, reg_count, description in self.REGISTER_BLOCKS:
                try:
                    ok, result = self.read_register_block(start_reg, reg_count, description, function_code=0x04)
                    if not ok:
                        report["errors"].append(f"0x{start_reg:04X} ({description}): {result}")
                        continue
                    successes += 1
                    if result.get("plausible"):
                        plausible_hits += 1
                    collected[f"0x{start_reg:04X}"] = result
                except Exception as exc:
                    report["errors"].append(f"0x{start_reg:04X} ({description}): {exc}")

            readings: Dict[str, Any] = {}
            for block in collected.values():
                readings.update(block.get("decoded", {}))

            report["details"]["identity"] = {
                "charging_mode": identity_decoded.get("charging_mode"),
                "rated_battery_current_a": identity_decoded.get("rated_battery_current_a"),
                "rated_load_current_a": identity_decoded.get("rated_load_current_a"),
                "system_rated_voltage_v": identity_decoded.get("system_rated_voltage_v"),
                "identity_confirmed": identity_confirmed,
            }

            report["details"]["summary"] = {
                "blocks_responded": successes,
                "plausible_hits": plausible_hits,
            }

            report["device_present"] = bool(identity_confirmed)

            report["details"]["transport"] = {
                "port": self.port,
                "baudrate": self.baudrate,
                "slave_id": self.slave_id,
            }

            report["details"]["readings"] = {
                "pv_voltage_v": readings.get("pv_voltage_v"),
                "pv_current_a": readings.get("pv_current_a"),
                "load_voltage_v": readings.get("load_voltage_v"),
                "load_current_a": readings.get("load_current_a"),
                "load_power_w": readings.get("load_power_w"),
                "battery_soc_pct": readings.get("battery_soc_pct"),
                "battery_temp_c": readings.get("battery_temp_c"),
                "device_temp_c": readings.get("device_temp_c"),
            }

            success = bool(identity_confirmed)
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


def main(argv=None) -> bool:
    preferred_port = os.getenv("PREFERRED_PORT", XTRA2210LowLevel.DEFAULT_PREFERRED_PORT)
    try:
        baudrate = int(os.getenv("XTRA2210_BAUDRATE", str(XTRA2210LowLevel.DEFAULT_BAUDRATE)))
    except Exception:
        baudrate = XTRA2210LowLevel.DEFAULT_BAUDRATE
    try:
        slave_id = int(os.getenv("XTRA2210_SLAVE_ID", str(XTRA2210LowLevel.DEFAULT_SLAVE_ID)))
    except Exception:
        slave_id = XTRA2210LowLevel.DEFAULT_SLAVE_ID
    try:
        timeout = float(os.getenv("XTRA2210_TIMEOUT", str(XTRA2210LowLevel.DEFAULT_TIMEOUT)))
    except Exception:
        timeout = XTRA2210LowLevel.DEFAULT_TIMEOUT
    try:
        inter_frame_delay = float(
            os.getenv(
                "XTRA2210_INTER_FRAME_DELAY",
                str(XTRA2210LowLevel.DEFAULT_INTER_FRAME_DELAY),
            )
        )
    except Exception:
        inter_frame_delay = XTRA2210LowLevel.DEFAULT_INTER_FRAME_DELAY

    dev = XTRA2210LowLevel(
        preferred_port=preferred_port,
        baudrate=baudrate,
        slave_id=slave_id,
        timeout=timeout,
        inter_frame_delay=inter_frame_delay,
        show_ports=True,
    )
    dev.logger.info("Starting XTRA2210 self-test")

    if not dev.init():
        report = {
            "success": False,
            "initialized": False,
            "opened": False,
            "device_present": False,
            "errors": [dev.last_error] if dev.last_error else [],
            "details": {},
        }
        dev.logger.error("XTRA2210 self-test failed: initialization")
        dev.logger.error("Initialization report=%s", json.dumps(report, default=str))
        print(json.dumps(report, indent=2, default=str))
        return False

    ok, report = dev.full_test()
    report["success"] = bool(ok)
    if ok:
        dev.logger.info("XTRA2210 self-test succeeded")
    else:
        dev.logger.error("XTRA2210 self-test failed")
    print(json.dumps(report, indent=2, default=str))
    dev.deinit()
    return bool(ok)


if __name__ == "__main__":
    ok = main()
    raise SystemExit(0 if ok else 1)