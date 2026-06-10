from __future__ import annotations

import os
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from modules.support.log_utils import get_logger


class MockLowLevel:
    """Base mock implementation for LL modules."""

    def __init__(self, logger_name: str = "mock_LL") -> None:
        self.logger = get_logger(logger_name)
        self.is_initialized: bool = False
        self.is_open: bool = False
        self.last_error: Optional[str] = None
        self.bus: Optional[Any] = None
        self.bus_num: Optional[int] = None
        self.address: Optional[int] = None
        self.bus_candidates: list[int] = []
        self.bus_forced: bool = False

    def _set_error(self, message: str) -> None:
        self.last_error = message

    def _clear_error(self) -> None:
        self.last_error = None

    def _build_full_test_report(self) -> dict:
        return {
            "initialized": self.is_initialized,
            "opened": self.is_open,
            "device_present": True,
            "errors": [],
            "details": {},
        }

    def init(self, *args: Any, **kwargs: Any) -> bool:
        self.logger.info("Mock init called")
        self._clear_error()
        self.close()
        self.is_initialized = True
        self.is_open = False
        self.address = kwargs.get("address", self.address)
        self.bus_num = kwargs.get("bus", self.bus_num)
        self.bus_forced = bool(kwargs.get("bus", False))
        return True

    def open(self) -> bool:
        self.logger.info("Mock open called")
        self._clear_error()
        if not self.is_initialized:
            self._set_error("Module is not initialized")
            self.logger.error(self.last_error)
            return False
        self.is_open = True
        return True

    def close(self) -> bool:
        self.logger.info("Mock close called")
        self._clear_error()
        self.is_open = False
        return True

    def deinit(self) -> bool:
        self.logger.info("Mock deinit called")
        self.close()
        self.is_initialized = False
        return True

    def probe(self) -> bool:
        self.logger.info("Mock probe called")
        self._clear_error()
        if not self.is_initialized:
            self._set_error("Module is not initialized")
            return False
        return True

    def test(self) -> bool:
        self.logger.info("Mock test called")
        self._clear_error()
        if not self.is_initialized:
            self._set_error("Module is not initialized")
            return False
        return True

    def full_test(self) -> tuple[bool, dict]:
        self.logger.info("Mock full_test called")
        self._clear_error()
        report = self._build_full_test_report()
        return True, report


class AudioProcLowLevelMock(MockLowLevel):
    def __init__(self) -> None:
        super().__init__(logger_name="audioProc_LL_mock")
        self.output_path: Optional[str] = None
        self.test_wav_path: Optional[str] = None

    def process(self, wav_path: str) -> Optional[str]:
        self.logger.info("Mock processing audio file: %s", wav_path)
        self._clear_error()
        if not self.is_initialized:
            self._set_error("Module is not initialized")
            return None

        source = Path(wav_path) if wav_path else Path("audio_proc_mock_input.wav")
        output = source.with_name(f"{source.stem}_mock_processed.wav")
        output.parent.mkdir(parents=True, exist_ok=True)

        try:
            with wave.open(str(output), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(192000)
                wf.writeframes(b"\x00\x00" * 1920)
            self.output_path = str(output)
            return self.output_path
        except Exception as exc:
            self._set_error(f"Mock process failed: {exc}")
            self.logger.exception("Mock process failed: %s", exc)
            return None

    def full_test(self) -> tuple[bool, dict]:
        self.open()
        report = self._build_full_test_report()
        report["details"] = {
            "mock": True,
            "output_path": self.output_path,
        }
        return True, report


class BehringerLowLevelMock(MockLowLevel):
    def __init__(self) -> None:
        super().__init__(logger_name="behringer_LL_mock")
        self.output_path: Optional[str] = None
        self.last_record_ok: bool = False
        self.recordings_dir = Path(__file__).resolve().parent / "behringer_mock"
        self.recordings_dir.mkdir(parents=True, exist_ok=True)

    def record(self, duration: int | float) -> bool:
        self.logger.info("Mock record called: duration=%s", duration)
        self._clear_error()
        if not self.is_initialized:
            self._set_error("Module is not initialized")
            return False

        self.output_path = str(self.recordings_dir / "mock_behringer_recording.wav")
        try:
            with wave.open(self.output_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(192000)
                num_frames = int(max(1, duration)) * 1920
                wf.writeframes(b"\x00\x00" * num_frames)
            self.last_record_ok = True
            return True
        except Exception as exc:
            self.last_record_ok = False
            self._set_error(f"Mock record failed: {exc}")
            self.logger.exception("Mock record failed: %s", exc)
            return False

    def is_recording_done(self) -> tuple[bool, bool]:
        return True, self.last_record_ok

    def full_test(self) -> tuple[bool, dict]:
        self.open()
        report = self._build_full_test_report()
        report["details"] = {
            "mock": True,
            "recordings_dir": str(self.recordings_dir),
        }
        return True, report


class WindsonicLowLevelMock(MockLowLevel):
    def __init__(self) -> None:
        super().__init__(logger_name="windsonic_LL_mock")
        self.samples: int = 10
        self.spacing: float = 1.0
        self.identification: str = "Q"
        self.is_acquiring: bool = False
        self.last_acquisition_ok: bool = False
        self.last_samples: List[dict[str, Any]] = []

    def config(self, samples: int = 10, spacing: float = 1.0) -> None:
        self.samples = int(samples)
        self.spacing = float(spacing)

    def acquire(self, num_acq: Optional[int] = None) -> bool:
        self.logger.info("Mock acquire called: num_acq=%s", num_acq)
        self._clear_error()
        if not self.is_initialized:
            self._set_error("Module is not initialized")
            return False
        self.is_acquiring = False
        self.last_acquisition_ok = True
        count = int(num_acq if num_acq is not None else self.samples)
        self.last_samples = [
            {
                "direction_deg": 180.0,
                "direction_valid": True,
                "speed": 3.0 + index,
                "units": "M",
                "status": "00",
            }
            for index in range(count)
        ]
        return True

    def is_acquisition_done(self) -> tuple[bool, bool]:
        return True, self.last_acquisition_ok

    def full_test(self) -> tuple[bool, dict]:
        self.open()
        report = self._build_full_test_report()
        report["details"] = {
            "mock": True,
            "samples": self.samples,
            "spacing": self.spacing,
        }
        return True, report


class IridiumLowLevelMock(MockLowLevel):
    def __init__(self) -> None:
        super().__init__(logger_name="iridium_LL_mock")

    def check_status(self) -> dict[str, Any]:
        return {
            "csq": "99",
            "creg": "1",
            "antena": "OK",
            "sbdix": "MO:0,MT:0",
        }

    def send_sbd_text(
        self,
        message: str,
        clear_after_success: bool = True,
        max_attempts: int = 3,
        retry_delay_s: float = 10.0,
        session_timeout: float = 90.0,
    ) -> tuple[bool, dict]:
        self.logger.info("Mock send_sbd_text called")
        self._clear_error()
        if not message:
            return False, {"errors": ["Text payload is empty"]}
        return True, {"mode": "text", "payload": message, "mock": True}

    def send_sbd_binary(
        self,
        payload: bytes,
        clear_after_success: bool = True,
        max_attempts: int = 3,
        retry_delay_s: float = 10.0,
        session_timeout: float = 90.0,
        ready_timeout: float = 5.0,
    ) -> tuple[bool, dict]:
        self.logger.info("Mock send_sbd_binary called")
        self._clear_error()
        if not payload:
            return False, {"errors": ["Binary payload is empty"]}
        if len(payload) > 340:
            return False, {"errors": ["Binary payload exceeds 340 bytes"]}
        return True, {"mode": "binary", "size": len(payload), "mock": True}


class AHT10LowLevelMock(MockLowLevel):
    def __init__(self) -> None:
        super().__init__(logger_name="aht10_LL_mock")

    def read_status(self) -> int:
        self.logger.info("Mock read_status called")
        self._clear_error()
        if not self.is_initialized:
            self._set_error("Module is not initialized")
            return 0
        return 0

    def read_measurement_raw(self, timeout: float = 1.0, retry_on_null: bool = True) -> bytes:
        self.logger.info("Mock read_measurement_raw called: timeout=%s retry_on_null=%s", timeout, retry_on_null)
        self._clear_error()
        if not self.is_initialized:
            raise RuntimeError("Module is not initialized")
        return b"\x08\x00\x00\x00\x80\x00"

    def parse(self, raw: bytes) -> tuple[float, float]:
        self.logger.info("Mock parse called")
        self._clear_error()
        if not raw:
            raise ValueError("No raw measurement data provided")
        return 25.0, 50.0


class AISLowLevelMock(MockLowLevel):
    def __init__(self) -> None:
        super().__init__(logger_name="ais_LL_mock")

    def get_navigation(self) -> Dict[str, Any]:
        return {
            "lat": 0.0,
            "lon": 0.0,
            "timestamp": None,
            "fix": True,
            "fix_quality": 1,
            "num_sats": 4,
            "hdop": 0.9,
            "satellites_in_view": {},
            "used_sats": [],
        }

    def read_lines(self, seconds: float = 1.0) -> List[str]:
        self.logger.info("Mock read_lines called: seconds=%s", seconds)
        self._clear_error()
        if not self.is_initialized:
            self._set_error("Module is not initialized")
            return []
        return ["!AIVDM,1,1,,A,13aG;P0000G?u@N6V8D<0?v`0<0=,0*7D"]


class MPU6050LowLevelMock(MockLowLevel):
    def __init__(self) -> None:
        super().__init__(logger_name="mpu6050_LL_mock")

    def read_all(self) -> Dict[str, Any]:
        self.logger.info("Mock read_all called")
        self._clear_error()
        if not self.is_initialized:
            raise RuntimeError("Module is not initialized")
        return {
            "accel_raw": [0, 0, 0],
            "gyro_raw": [0, 0, 0],
            "temp_c": 25.0,
            "accel_g": [0.0, 0.0, 0.0],
            "gyro_dps": [0.0, 0.0, 0.0],
        }


class XTRA2210LowLevelMock(MockLowLevel):
    def __init__(self) -> None:
        super().__init__(logger_name="xtra2210_LL_mock")

    def read_all_decoded(self) -> Dict[str, Any]:
        self.logger.info("Mock read_all_decoded called")
        self._clear_error()
        if not self.is_initialized:
            raise RuntimeError("Module is not initialized")
        return {
            "identity": {"model": "XTRA2210", "firmware": "mock"},
            "pv": {"input_voltage": 12.0, "input_current": 1.2},
            "load": {"voltage": 12.0, "current": 0.5, "power": 6.0},
            "battery": {"voltage": 12.6, "soc": 85.0, "temperature": 25.0},
            "temperatures": {"battery": 25.0, "device": 26.0},
        }


__all__ = [
    "AudioProcLowLevelMock",
    "BehringerLowLevelMock",
    "WindsonicLowLevelMock",
    "IridiumLowLevelMock",
    "AHT10LowLevelMock",
    "AISLowLevelMock",
    "MPU6050LowLevelMock",
    "XTRA2210LowLevelMock",
    "MockLowLevel",
]
