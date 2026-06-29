"""modules/behringer_LL.py

Low-level driver for the Behringer USB audio interface.

Public lifecycle:
- init() -> bool
- open() -> bool
- close() -> bool
- test() -> bool
- full_test() -> tuple[bool, dict]
- deinit() -> bool

Functional helpers:
- record(duration) -> bool
- stop_recording() -> bool
- is_recording_done() -> tuple[bool, bool]
- list_recordings() -> list[str]
- delete_old_recordings(days) -> int
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import wave
from typing import Any, Dict, List, Optional, Tuple

import pyaudio

import sys


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.support.log_utils import get_logger
from modules.support.system_config import compact_utc_minus_3_timestamp, get_config_value
from modules.support.storage_guard import (
    RECORDING_INTERRUPTED,
    RECORDING_SKIPPED_INVALID_AUDIO_CONFIG,
    RECORDING_STOPPED_AUDIO_ERROR,
    RECORDING_STOPPED_MAX_DURATION,
    RECORDING_STOPPED_MAX_FILE_SIZE,
    STORAGE_CRITICAL_LOW_FREE_SPACE,
    disk_free_bytes,
    estimate_recording_size,
    evaluate_file_size_limit,
    evaluate_free_space_warnings,
    evaluate_storage_admission,
    get_directory_size_bytes,
    validate_recordings_dir,
)

# Suppress common ALSA/JACK warnings and prevent JACK server autostart.
os.environ["PYTHONWARNINGS"] = "ignore"
os.environ["JACK_NO_START_SERVER"] = "1"


class BehringerError(Exception):
    """Base exception for Behringer low-level errors."""


class NotFound(BehringerError):
    """Raised when PyAudio or a compatible device is not available."""


class AudioError(BehringerError):
    """Raised on audio interface or stream errors."""


class BehringerLowLevel:
    """Low-level controller for the Behringer USB audio interface."""

    DEFAULT_SAMPLE_RATE = 192000
    DEFAULT_CHANNELS = 2
    DEFAULT_OUTPUT_CHANNELS = 1
    DEFAULT_FORMAT = pyaudio.paInt24
    DEFAULT_FRAMES_PER_BUFFER = 8192
    DEFAULT_DEVICE_NAME_FILTERS = ("Behringer", "USB")
    DEFAULT_RECORDINGS_STORAGE_DIR = "/storage/boya/recordings"
    DEFAULT_RECORDINGS_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "recordings",
    )

    def __init__(
        self,
        logger_name: str = "behringer_LL",
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = DEFAULT_CHANNELS,
        output_channels: int = DEFAULT_OUTPUT_CHANNELS,
        audio_format: int = DEFAULT_FORMAT,
        frames_per_buffer: int = DEFAULT_FRAMES_PER_BUFFER,
        device_name_filters: Optional[Tuple[str, ...]] = None,
        recordings_dir: Optional[str] = None,
        bits_per_sample: Optional[int] = None,
        storage_guard_enabled: Optional[bool] = None,
    ) -> None:
        self.logger = get_logger(logger_name)

        self.is_initialized: bool = False
        self.is_open: bool = False
        self.last_error: Optional[str] = None

        self.bus = None
        self.bus_num = None
        self.address = None
        self.bus_candidates: List[int] = []
        self.bus_forced: bool = False

        config_sample_rate = get_config_value("fs[Hz]", sample_rate)
        config_output_channels = get_config_value("behringer_output_channels", output_channels)
        config_recordings_dir = get_config_value("recordings_dir", self.DEFAULT_RECORDINGS_STORAGE_DIR)
        config_bits_per_sample = get_config_value("bits_per_sample", bits_per_sample or 24)

        self.sample_rate: int = int(config_sample_rate)
        self.channels: int = int(channels)
        self.output_channels: int = int(config_output_channels)
        self.audio_format: int = int(audio_format)
        self.frames_per_buffer: int = int(frames_per_buffer)
        self.device_name_filters: Tuple[str, ...] = tuple(
            device_name_filters or self.DEFAULT_DEVICE_NAME_FILTERS
        )
        self.recordings_dir: str = str(recordings_dir or config_recordings_dir)
        self.bits_per_sample: int = int(config_bits_per_sample)
        self.storage_guard_enabled: bool = bool(
            get_config_value("storage_guard_enabled", True)
            if storage_guard_enabled is None
            else storage_guard_enabled
        )
        self.storage_guard_max_recordings_dir_bytes: int = int(
            get_config_value("storage_guard_max_recordings_dir_bytes", 860 * 1024 ** 3)
        )
        self.storage_guard_min_free_warning_bytes: int = int(
            get_config_value("storage_guard_min_free_warning_bytes", 100 * 1024 ** 3)
        )
        self.storage_guard_min_free_critical_bytes: int = int(
            get_config_value("storage_guard_min_free_critical_bytes", 50 * 1024 ** 3)
        )
        self.storage_guard_hard_reserve_bytes: int = int(
            get_config_value("storage_guard_hard_reserve_bytes", 10 * 1024 ** 3)
        )
        self.storage_guard_file_margin_factor: float = float(
            get_config_value("storage_guard_file_margin_factor", 1.10)
        )

        self.audio_interface: Optional[pyaudio.PyAudio] = None
        self.device_index: Optional[int] = None
        self.device_info: Optional[Dict[str, Any]] = None
        self.stream: Optional[pyaudio.Stream] = None

        self.is_recording_event = threading.Event()
        self.recording_thread: Optional[threading.Thread] = None
        self.last_record_ok: bool = False
        self.start_time: Optional[float] = None
        self.duration: Optional[float] = None
        self.frames_queue: "queue.Queue[bytes]" = queue.Queue()
        self.output_path: Optional[str] = None
        self.max_file_size_bytes: Optional[int] = None
        self.expected_size_bytes: Optional[int] = None
        self.recording_warnings: List[str] = []
        self.last_recording_metadata: Dict[str, Any] = {}
        self.last_storage_admission = None

    def _set_error(self, msg: str) -> None:
        self.last_error = msg

    def _clear_error(self) -> None:
        self.last_error = None

    def _format_name(self) -> str:
        if self.audio_format == pyaudio.paInt24:
            return "paInt24"
        if self.audio_format == pyaudio.paInt16:
            return "paInt16"
        if self.audio_format == pyaudio.paInt32:
            return "paInt32"
        if self.audio_format == pyaudio.paFloat32:
            return "paFloat32"
        return str(self.audio_format)

    def _bytes_per_sample(self) -> int:
        if self.audio_interface is not None:
            return int(self.audio_interface.get_sample_size(self.audio_format))
        if self.audio_format == pyaudio.paInt24:
            return 3
        if self.audio_format == pyaudio.paInt16:
            return 2
        if self.audio_format in (pyaudio.paInt32, pyaudio.paFloat32):
            return 4
        raise AudioError(f"Unsupported audio format: {self.audio_format}")

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
            "Full diagnostic test completed: success=%s device_present=%s device=%s audio=%s filesystem=%s",
            success,
            report.get("device_present"),
            report.get("details", {}).get("device"),
            report.get("details", {}).get("audio"),
            report.get("details", {}).get("filesystem"),
        )

    def _find_device(self, audio: pyaudio.PyAudio) -> Tuple[int, Dict[str, Any], List[Dict[str, Any]]]:
        devices: List[Dict[str, Any]] = []
        compatible_candidates: List[Tuple[int, Dict[str, Any]]] = []

        num_devices = audio.get_device_count()

        for i in range(num_devices):
            try:
                info = audio.get_device_info_by_index(i)
                name = str(info.get("name", ""))
                max_input_channels = int(info.get("maxInputChannels", 0))
                default_sample_rate = float(info.get("defaultSampleRate", 0.0))

                name_matches = any(token.lower() in name.lower() for token in self.device_name_filters)
                has_enough_channels = max_input_channels >= self.channels

                format_supported = False
                format_error = None

                if name_matches and has_enough_channels:
                    try:
                        format_supported = bool(
                            audio.is_format_supported(
                                self.sample_rate,
                                input_device=i,
                                input_channels=self.channels,
                                input_format=self.audio_format,
                            )
                        )
                    except Exception as exc:
                        format_error = str(exc)

                entry = {
                    "index": i,
                    "name": name,
                    "max_input_channels": max_input_channels,
                    "default_sample_rate": default_sample_rate,
                    "name_matches": name_matches,
                    "has_enough_channels": has_enough_channels,
                    "format_supported": format_supported,
                    "format_error": format_error,
                }
                devices.append(entry)

                self.logger.info("PyAudio device candidate: %s", entry)

                if name_matches and has_enough_channels:
                    compatible_candidates.append((i, dict(info)))

            except Exception as exc:
                devices.append({"index": i, "error": str(exc)})
                self.logger.warning("Could not inspect PyAudio device %s: %s", i, exc)

        if compatible_candidates:
            device_index, device_info = compatible_candidates[0]
            self.logger.info(
                "Selected PyAudio device: index=%s name=%s",
                device_index,
                device_info.get("name"),
            )
            return device_index, device_info, devices

        raise NotFound(
            "No compatible Behringer/USB input device found for "
            f"rate={self.sample_rate}, channels={self.channels}, format={self._format_name()}. "
            f"filters={self.device_name_filters}. Inspected devices={devices}"
        )

    def _open_stream(self, max_attempts: int = 3, retry_delay_s: float = 1.0) -> bool:
        if self.audio_interface is None or self.device_index is None:
            self._set_error("Audio interface is not open")
            self.logger.error(self.last_error)
            return False

        if self.stream is not None:
            self._close_stream()

        last_exc = None

        for attempt in range(1, max_attempts + 1):
            try:
                self.logger.info(
                    "Opening audio stream attempt %s/%s: device_index=%s channels=%s rate=%s format=%s frames_per_buffer=%s",
                    attempt,
                    max_attempts,
                    self.device_index,
                    self.channels,
                    self.sample_rate,
                    self._format_name(),
                    self.frames_per_buffer,
                )

                self.stream = self.audio_interface.open(
                    format=self.audio_format,
                    channels=self.channels,
                    rate=self.sample_rate,
                    input=True,
                    frames_per_buffer=self.frames_per_buffer,
                    input_device_index=self.device_index,
                    start=False,
                )

                time.sleep(0.2)
                self.stream.start_stream()

                self.logger.info(
                    "Audio stream opened successfully: attempt=%s device_index=%s channels=%s rate=%s format=%s frames_per_buffer=%s",
                    attempt,
                    self.device_index,
                    self.channels,
                    self.sample_rate,
                    self._format_name(),
                    self.frames_per_buffer,
                )
                self._clear_error()
                return True

            except Exception as exc:
                last_exc = exc
                self.logger.warning(
                    "Audio stream open attempt %s/%s failed: %s",
                    attempt,
                    max_attempts,
                    exc,
                )
                self._close_stream()
                time.sleep(retry_delay_s)

        self.stream = None
        self._set_error(f"Failed to open audio stream after {max_attempts} attempts: {last_exc}")
        self.logger.error(self.last_error)
        return False    

    def _close_stream(self) -> bool:
        try:
            stream = self.stream
            self.stream = None

            if stream is not None:
                try:
                    if stream.is_active():
                        stream.stop_stream()
                except Exception as exc:
                    self.logger.debug("Ignoring stop_stream warning: %s", exc)

                try:
                    stream.close()
                except Exception as exc:
                    self.logger.debug("Ignoring stream close warning: %s", exc)

                time.sleep(0.2)

            return True

        except Exception as exc:
            self.stream = None
            self._set_error(f"Failed to close audio stream: {exc}")
            self.logger.exception("Failed to close audio stream: %s", exc)
            return False

    def _make_output_path(self) -> str:
        os.makedirs(self.recordings_dir, exist_ok=True)
        timestamp = compact_utc_minus_3_timestamp()
        return os.path.join(self.recordings_dir, f"recording_{timestamp}.wav")

    def _estimate_recording_size(self, duration: int | float):
        return estimate_recording_size(
            sample_rate_hz=self.sample_rate,
            bits_per_sample=self.bits_per_sample,
            channels=self.output_channels,
            duration_s=duration,
            margin_factor=self.storage_guard_file_margin_factor,
        )

    def _evaluate_storage_admission(self, duration: int | float):
        try:
            size = self._estimate_recording_size(duration)
        except Exception as exc:
            self.logger.error("Invalid audio sizing configuration: %s", exc)
            return evaluate_storage_admission(
                recordings_dir_used_bytes=0,
                free_bytes=0,
                expected_size_bytes=0,
                max_file_size_bytes=0,
                max_recordings_dir_bytes=self.storage_guard_max_recordings_dir_bytes,
                min_free_warning_bytes=self.storage_guard_min_free_warning_bytes,
                min_free_critical_bytes=self.storage_guard_min_free_critical_bytes,
                hard_reserve_bytes=self.storage_guard_hard_reserve_bytes,
                directory_errors=[RECORDING_SKIPPED_INVALID_AUDIO_CONFIG],
            )

        validation = validate_recordings_dir(self.recordings_dir, create=True)
        if not validation.ok:
            return evaluate_storage_admission(
                recordings_dir_used_bytes=0,
                free_bytes=0,
                expected_size_bytes=size.expected_wav_bytes,
                max_file_size_bytes=size.max_file_size_bytes,
                max_recordings_dir_bytes=self.storage_guard_max_recordings_dir_bytes,
                min_free_warning_bytes=self.storage_guard_min_free_warning_bytes,
                min_free_critical_bytes=self.storage_guard_min_free_critical_bytes,
                hard_reserve_bytes=self.storage_guard_hard_reserve_bytes,
                directory_errors=validation.errors,
            )

        used_bytes = get_directory_size_bytes(self.recordings_dir)
        free_bytes = disk_free_bytes(self.recordings_dir)
        return evaluate_storage_admission(
            recordings_dir_used_bytes=used_bytes,
            free_bytes=free_bytes,
            expected_size_bytes=size.expected_wav_bytes,
            max_file_size_bytes=size.max_file_size_bytes,
            max_recordings_dir_bytes=self.storage_guard_max_recordings_dir_bytes,
            min_free_warning_bytes=self.storage_guard_min_free_warning_bytes,
            min_free_critical_bytes=self.storage_guard_min_free_critical_bytes,
            hard_reserve_bytes=self.storage_guard_hard_reserve_bytes,
        )

    def _metadata_snapshot(
        self,
        *,
        complete: bool,
        stop_reason: str,
        frames_written: int = 0,
        actual_duration_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        size_bytes = None
        free_after = None
        used_bytes = None
        if self.output_path is not None:
            try:
                size_bytes = os.path.getsize(self.output_path)
            except OSError:
                size_bytes = None
        try:
            if os.path.isdir(self.recordings_dir):
                free_after = disk_free_bytes(self.recordings_dir)
                used_bytes = get_directory_size_bytes(self.recordings_dir)
        except OSError:
            pass

        admission = self.last_storage_admission
        return {
            "path": self.output_path,
            "duration_sec": actual_duration_s,
            "size_bytes": size_bytes,
            "expected_size_bytes": self.expected_size_bytes,
            "max_file_size_bytes": self.max_file_size_bytes,
            "complete": bool(complete),
            "stop_reason": stop_reason,
            "free_bytes_before": getattr(admission, "free_bytes_before", None),
            "free_bytes_after": free_after,
            "recordings_dir_used_bytes": used_bytes,
            "warnings": list(self.recording_warnings),
            "chunks_written": frames_written,
        }

    def _clear_queue(self) -> None:
        try:
            while True:
                self.frames_queue.get_nowait()
        except queue.Empty:
            pass

    def _callback(self, in_data, _frame_count, _time_info, status):
        if status:
            self.logger.warning("Audio stream status: %s", status)
        if not self.is_recording_event.is_set():
            return (b"", pyaudio.paComplete)
        self.frames_queue.put(in_data)
        return (in_data, pyaudio.paContinue)

    def init(
        self,
        sample_rate: Optional[int] = None,
        channels: Optional[int] = None,
        output_channels: Optional[int] = None,
        frames_per_buffer: Optional[int] = None,
        recordings_dir: Optional[str] = None,
    ) -> bool:
        """Prepare configuration and internal state only. Does not access PyAudio hardware."""
        self.logger.info("Initializing module")
        self._clear_error()
        try:
            self._close_stream()

            if self.audio_interface is not None:
                try:
                    self.audio_interface.terminate()
                except Exception as exc:
                    self.logger.debug("Ignoring PyAudio terminate warning during init: %s", exc)

            self.audio_interface = None
            self.device_index = None
            self.device_info = None
            self.stream = None
            self.bus = None
            self.bus_num = None
            self.is_open = False

            if sample_rate is not None:
                self.sample_rate = int(sample_rate)
            if channels is not None:
                self.channels = int(channels)
            if output_channels is not None:
                self.output_channels = int(output_channels)
            if frames_per_buffer is not None:
                self.frames_per_buffer = int(frames_per_buffer)
            if recordings_dir is not None:
                self.recordings_dir = str(recordings_dir)
            if not self.storage_guard_enabled:
                os.makedirs(self.recordings_dir, exist_ok=True)
            self.audio_interface = None
            self.device_index = None
            self.device_info = None
            self.stream = None
            self.bus = None
            self.bus_num = None
            self.address = None
            self.bus_candidates = []
            self.bus_forced = False
            self.is_open = False
            self.is_initialized = True
            self.last_record_ok = False
            self.output_path = None
            self.max_file_size_bytes = None
            self.expected_size_bytes = None
            self.recording_warnings = []
            self.last_recording_metadata = {}
            self.last_storage_admission = None
            self._clear_queue()
            self.logger.info(
                "Module initialized: sample_rate=%s channels=%s output_channels=%s format=%s frames_per_buffer=%s recordings_dir=%s storage_guard_enabled=%s",
                self.sample_rate,
                self.channels,
                self.output_channels,
                self._format_name(),
                self.frames_per_buffer,
                self.recordings_dir,
                self.storage_guard_enabled,
            )
            return True
        except Exception as exc:
            self.is_initialized = False
            self._set_error(f"Initialization failed: {exc}")
            self.logger.exception("Initialization failed: %s", exc)
            return False

    def open(self) -> bool:
        """Open the audio transport by creating PyAudio and selecting the device. Does not start recording."""
        self.logger.info("Opening audio transport")
        self._clear_error()
        if not self.is_initialized:
            self._set_error("Module is not initialized")
            self.logger.error(self.last_error)
            return False
        if self.is_open and self.audio_interface is not None and self.device_index is not None:
            self.logger.info("Audio transport already open: device_index=%s", self.device_index)
            return True
        try:
            audio = pyaudio.PyAudio()
            device_index, device_info, devices = self._find_device(audio)
            self.audio_interface = audio
            self.device_index = device_index
            self.device_info = device_info
            self.bus = audio
            self.bus_num = device_index
            self.bus_candidates = [
                int(d["index"])
                for d in devices
                if "index" in d and isinstance(d["index"], int)
            ]
            self.is_open = True
            self.logger.info(
                "Audio transport opened: device_index=%s name=%s max_input_channels=%s",
                self.device_index,
                self.device_info.get("name"),
                self.device_info.get("maxInputChannels"),
            )
            return True
        except Exception as exc:
            self.audio_interface = None
            self.device_index = None
            self.device_info = None
            self.bus = None
            self.bus_num = None
            self.is_open = False
            self._set_error(f"Open failed: {exc}")
            self.logger.exception("Open failed: %s", exc)
            return False

    def close(self) -> bool:
        """Close stream and audio transport. Idempotent."""
        self.logger.info("Closing audio transport")
        self._clear_error()
        try:
            if self.is_recording_event.is_set():
                self.stop_recording()
            self._close_stream()
            if self.audio_interface is not None:
                try:
                    self.audio_interface.terminate()
                except Exception as exc:
                    self.logger.debug("Ignoring PyAudio terminate warning: %s", exc)
            
            time.sleep(1.0)

            self.audio_interface = None
            self.device_index = None
            self.device_info = None
            self.bus = None
            self.bus_num = None
            self.stream = None
            self.is_open = False
            return True
        except Exception as exc:
            self.audio_interface = None
            self.device_index = None
            self.device_info = None
            self.bus = None
            self.bus_num = None
            self.stream = None
            self.is_open = False
            self._set_error(f"Close failed: {exc}")
            self.logger.exception("Close failed: %s", exc)
            return False

    def deinit(self) -> bool:
        """Total cleanup. Leaves module in a neutral state."""
        self.logger.info("Deinitializing module")
        self._clear_error()
        try:
            ok = self.close()
            self.is_initialized = False
            self.last_record_ok = False
            self.start_time = None
            self.duration = None
            self.output_path = None
            self.bus_candidates = []
            self.bus_forced = False
            self._clear_queue()
            return bool(ok)
        except Exception as exc:
            self._set_error(f"Deinitialization failed: {exc}")
            self.logger.exception("Deinitialization failed: %s", exc)
            return False

    def probe(self) -> bool:
        """Minimal device presence check on the currently open PyAudio transport."""
        self.logger.info("Probing Behringer audio device")
        self._clear_error()
        try:
            if self.audio_interface is None or self.device_index is None:
                raise AudioError("Audio transport is not open")
            info = self.audio_interface.get_device_info_by_index(self.device_index)
            max_input_channels = int(info.get("maxInputChannels", 0))
            name = str(info.get("name", ""))
            name_matches = any(token in name for token in self.device_name_filters)
            present = bool(name_matches and max_input_channels > 0)
            self.logger.info(
                "Probe result: present=%s device_index=%s name=%s max_input_channels=%s",
                present,
                self.device_index,
                name,
                max_input_channels,
            )
            return present
        except Exception as exc:
            self._set_error(f"Probe failed: {exc}")
            self.logger.warning("Probe failed: %s", exc)
            return False

    def test(self) -> bool:
        """Fast smoke test. May open temporarily and restores original state."""
        self.logger.info("Running smoke test")
        self._clear_error()
        was_open = self.is_open and self.audio_interface is not None
        temporarily_opened = False
        try:
            if not was_open:
                if not self.open():
                    return False
                temporarily_opened = True
            if not self.probe():
                return False
            if not self._open_stream():
                return False
            self._close_stream()
            self.logger.info("Smoke test completed: success=True")
            return True
        except Exception as exc:
            self._set_error(f"Test failed: {exc}")
            self.logger.warning("Test failed: %s", exc)
            return False
        finally:
            if temporarily_opened:
                self.close()

    def full_test(self) -> tuple[bool, dict]:
        """Full diagnostic test. Never propagates uncaught exceptions."""
        self.logger.info("Running full diagnostic test")
        self._clear_error()
        report = self._build_full_test_report()
        was_open = self.is_open and self.audio_interface is not None
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

            device_present = self.probe()
            report["device_present"] = bool(device_present)
            device_details: Dict[str, Any] = {}
            if self.audio_interface is not None and self.device_index is not None:
                try:
                    info = self.audio_interface.get_device_info_by_index(self.device_index)
                    device_details = {
                        "index": self.device_index,
                        "name": info.get("name"),
                        "max_input_channels": int(info.get("maxInputChannels", 0)),
                        "default_sample_rate": float(info.get("defaultSampleRate", 0.0)),
                    }
                except Exception as exc:
                    report["errors"].append(f"Device info read failed: {exc}")

            stream_open_ok = False
            try:
                stream_open_ok = self._open_stream(max_attempts=3, retry_delay_s=1.0)
                if not stream_open_ok and self.last_error:
                    report["errors"].append(self.last_error)
            finally:
                self._close_stream()

            fs_write_ok = False
            free_bytes = 0
            try:
                os.makedirs(self.recordings_dir, exist_ok=True)
                testfile = os.path.join(self.recordings_dir, "behringer_test_perm.tmp")
                with open(testfile, "w", encoding="utf-8") as f:
                    f.write("test")
                os.remove(testfile)
                fs_write_ok = True
                statvfs = os.statvfs(self.recordings_dir)
                free_bytes = int(statvfs.f_frsize * statvfs.f_bavail)
            except Exception as exc:
                report["errors"].append(f"Filesystem check failed: {exc}")

            report["details"] = {
                "device": device_details,
                "audio": {
                    "sample_rate": self.sample_rate,
                    "channels": self.channels,
                    "output_channels": self.output_channels,
                    "format": self._format_name(),
                    "frames_per_buffer": self.frames_per_buffer,
                    "stream_open_ok": stream_open_ok,
                },
                "filesystem": {
                    "recordings_dir": self.recordings_dir,
                    "write_ok": fs_write_ok,
                    "free_bytes": free_bytes,
                },
                "recording": {
                    "is_recording": self.is_recording_event.is_set(),
                    "last_record_ok": self.last_record_ok,
                    "output_path": self.output_path,
                },
            }
            success = bool(
                report["initialized"]
                and report["opened"]
                and report["device_present"]
                and stream_open_ok
                and fs_write_ok
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

    def record(self, duration: int | float) -> bool:
        """Start recording audio for a bounded duration."""
        self.logger.info("Starting recording request: duration=%s", duration)
        self._clear_error()
        try:
            if not self.is_initialized:
                self._set_error("Module is not initialized")
                self.logger.error(self.last_error)
                return False
            if self.is_recording_event.is_set():
                self._set_error("Recording is already active")
                self.logger.warning(self.last_error)
                return False
            self.recording_warnings = []
            self.last_storage_admission = None
            self.last_recording_metadata = {}
            self.expected_size_bytes = None
            self.max_file_size_bytes = None

            if self.storage_guard_enabled:
                admission = self._evaluate_storage_admission(duration)
                self.last_storage_admission = admission
                self.recording_warnings.extend(admission.warnings)
                self.expected_size_bytes = admission.expected_size_bytes
                self.max_file_size_bytes = admission.max_file_size_bytes
                for warning in admission.warnings:
                    self.logger.warning("Storage guard warning before recording: %s", warning)
                if not admission.ok:
                    self._set_error(",".join(admission.errors))
                    self.last_recording_metadata = self._metadata_snapshot(
                        complete=False,
                        stop_reason=admission.errors[0] if admission.errors else RECORDING_STOPPED_AUDIO_ERROR,
                    )
                    self.logger.error("Recording skipped by storage guard: errors=%s", admission.errors)
                    return False
            else:
                size = self._estimate_recording_size(duration)
                self.expected_size_bytes = size.expected_wav_bytes
                self.max_file_size_bytes = size.max_file_size_bytes

            if not self.is_open:
                if not self.open():
                    self.last_recording_metadata = self._metadata_snapshot(
                        complete=False,
                        stop_reason=RECORDING_STOPPED_AUDIO_ERROR,
                    )
                    return False
            if self.audio_interface is None or self.device_index is None:
                self._set_error("Audio transport is not open")
                self.last_recording_metadata = self._metadata_snapshot(
                    complete=False,
                    stop_reason=RECORDING_STOPPED_AUDIO_ERROR,
                )
                self.logger.error(self.last_error)
                return False

            self.output_path = self._make_output_path()
            self.duration = float(duration)
            self.start_time = time.time()
            self.last_record_ok = False
            self._clear_queue()
            self.is_recording_event.set()
            if not self._open_stream(max_attempts=3, retry_delay_s=1.0):
                self.is_recording_event.clear()
                return False
            self.recording_thread = threading.Thread(target=self._write_audio, daemon=True)
            self.recording_thread.start()
            self.logger.info("Recording started: path=%s duration=%s", self.output_path, self.duration)
            return True
        except Exception as exc:
            self.is_recording_event.clear()
            self.last_record_ok = False
            self._set_error(f"Record failed: {exc}")
            self.last_recording_metadata = self._metadata_snapshot(
                complete=False,
                stop_reason=RECORDING_STOPPED_AUDIO_ERROR,
            )
            self.logger.exception("Record failed: %s", exc)
            self._close_stream()
            return False

    def _write_audio(self) -> None:
        if self.audio_interface is None:
            self.logger.error("Audio interface is not open. Aborting recording.")
            self.last_record_ok = False
            self.is_recording_event.clear()
            self._close_stream()
            return

        if self.stream is None:
            self.logger.error("Audio stream is not open. Aborting recording.")
            self.last_record_ok = False
            self.is_recording_event.clear()
            self._close_stream()
            return

        if self.output_path is None:
            self.logger.error("Output path is not defined. Aborting recording.")
            self.last_record_ok = False
            self.is_recording_event.clear()
            self._close_stream()
            return

        frames_written = 0
        audio_bytes_written = 0
        stop_reason = RECORDING_INTERRUPTED
        complete = False

        try:
            sample_width = self._bytes_per_sample()

            with wave.open(self.output_path, "wb") as wf:
                wf.setnchannels(self.output_channels)
                wf.setsampwidth(sample_width)
                wf.setframerate(self.sample_rate)

                start = time.time()

                while self.is_recording_event.is_set():
                    if self.duration is not None and (time.time() - start) >= self.duration:
                        complete = True
                        stop_reason = RECORDING_STOPPED_MAX_DURATION
                        break

                    try:
                        frame = self.stream.read(
                            self.frames_per_buffer,
                            exception_on_overflow=False,
                        )
                    except Exception as exc:
                        self.logger.warning("Audio stream read failed: %s", exc)
                        stop_reason = RECORDING_STOPPED_AUDIO_ERROR
                        continue

                    if not frame:
                        continue

                    if self.output_channels == self.channels:
                        output_frame = frame

                    elif self.output_channels == 1 and self.channels >= 1:
                        frame_width = self.channels * sample_width

                        if len(frame) % frame_width == 0:
                            mono = bytearray()

                            for i in range(0, len(frame), frame_width):
                                mono.extend(frame[i:i + sample_width])

                            output_frame = bytes(mono)
                        else:
                            output_frame = frame

                    else:
                        output_frame = frame

                    wf.writeframes(output_frame)

                    frames_written += 1
                    audio_bytes_written += len(output_frame)

                    if self.max_file_size_bytes is not None:
                        estimated_file_size = audio_bytes_written + 44
                        exceeded = evaluate_file_size_limit(
                            estimated_file_size,
                            max_file_size_bytes=self.max_file_size_bytes,
                        )
                        if exceeded:
                            stop_reason = RECORDING_STOPPED_MAX_FILE_SIZE
                            self.recording_warnings.extend(exceeded)
                            self.logger.warning(
                                "Recording reached max file size: estimated_size=%s max=%s",
                                estimated_file_size,
                                self.max_file_size_bytes,
                            )
                            break

                    if self.storage_guard_enabled and os.path.isdir(self.recordings_dir):
                        try:
                            runtime_warnings = evaluate_free_space_warnings(
                                disk_free_bytes(self.recordings_dir),
                                warning_bytes=self.storage_guard_min_free_warning_bytes,
                                critical_bytes=self.storage_guard_min_free_critical_bytes,
                            )
                            for warning in runtime_warnings:
                                if warning == STORAGE_CRITICAL_LOW_FREE_SPACE and warning not in self.recording_warnings:
                                    self.recording_warnings.append(warning)
                                    self.logger.warning("Storage guard warning during recording: %s", warning)
                        except OSError:
                            pass

            self.last_record_ok = bool(complete and frames_written > 0)

            if self.last_record_ok:
                self.logger.info(
                    "Recording completed successfully: chunks=%s path=%s",
                    frames_written,
                    self.output_path,
                )
            elif frames_written > 0:
                self.logger.warning(
                    "Recording completed incomplete: chunks=%s reason=%s path=%s",
                    frames_written,
                    stop_reason,
                    self.output_path,
                )
            else:
                self.logger.error("Recording completed without frames: %s", self.output_path)
            actual_duration = None if self.start_time is None else max(0.0, time.time() - self.start_time)
            self.last_recording_metadata = self._metadata_snapshot(
                complete=self.last_record_ok,
                stop_reason=stop_reason if frames_written > 0 else RECORDING_STOPPED_AUDIO_ERROR,
                frames_written=frames_written,
                actual_duration_s=actual_duration,
            )

        except Exception as exc:
            self.last_record_ok = False
            self._set_error(f"Recording writer failed: {exc}")
            actual_duration = None if self.start_time is None else max(0.0, time.time() - self.start_time)
            self.last_recording_metadata = self._metadata_snapshot(
                complete=False,
                stop_reason=RECORDING_STOPPED_AUDIO_ERROR,
                frames_written=frames_written,
                actual_duration_s=actual_duration,
            )
            self.logger.exception("Recording writer failed: %s", exc)

        finally:
            self.is_recording_event.clear()
            self._close_stream()

    def stop_recording(self) -> bool:
        """Stop an active recording."""
        self.logger.info("Stopping recording")
        try:
            self.is_recording_event.clear()
            if self.recording_thread is not None and self.recording_thread.is_alive():
                self.recording_thread.join(timeout=5.0)
            self._close_stream()
            return True
        except Exception as exc:
            self._set_error(f"Stop recording failed: {exc}")
            self.logger.exception("Stop recording failed: %s", exc)
            return False

    def is_recording_done(self) -> tuple[bool, bool]:
        done = (
            not self.is_recording_event.is_set()
            and (self.recording_thread is None or not self.recording_thread.is_alive())
        )
        return done, bool(self.last_record_ok)

    def list_recordings(self, pattern: str = "*.wav") -> List[str]:
        search_path = os.path.join(self.recordings_dir, "**", pattern)
        return sorted(glob.glob(search_path, recursive=True))

    def delete_old_recordings(self, days: int = 30) -> int:
        cutoff = time.time() - (int(days) * 86400)
        deleted = 0
        for path in self.list_recordings():
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    deleted += 1
            except Exception as exc:
                self.logger.warning("Could not delete old recording %s: %s", path, exc)
        self.logger.info("Deleted %s old recordings older than %s days", deleted, days)
        return deleted


def main(argv=None) -> bool:
    ll = BehringerLowLevel()
    ll.logger.info("Starting Behringer self-test")
    if not ll.init():
        report = {
            "success": False,
            "initialized": False,
            "opened": False,
            "device_present": False,
            "errors": [ll.last_error] if ll.last_error else [],
            "details": {},
        }
        ll.logger.error("Behringer self-test failed: initialization")
        ll.logger.error("Initialization report=%s", json.dumps(report, default=str))
        print(json.dumps(report, indent=2, default=str))
        return False
    ok, report = ll.full_test()
    report["success"] = bool(ok)
    if ok:
        ll.logger.info("Behringer self-test succeeded")
    else:
        ll.logger.error("Behringer self-test failed")
    print(json.dumps(report, indent=2, default=str))
    ll.deinit()
    return bool(ok)


if __name__ == "__main__":
    ok = main()
    raise SystemExit(0 if ok else 1)
