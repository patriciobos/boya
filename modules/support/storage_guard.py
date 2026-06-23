from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

GIB_BYTES = 1024**3
WAV_HEADER_BYTES = 44

STORAGE_WARNING_LOW_FREE_SPACE = "STORAGE_WARNING_LOW_FREE_SPACE"
STORAGE_CRITICAL_LOW_FREE_SPACE = "STORAGE_CRITICAL_LOW_FREE_SPACE"
STORAGE_WARNING_INVALID_DURATION_USING_DEFAULT = (
    "STORAGE_WARNING_INVALID_DURATION_USING_DEFAULT"
)
STORAGE_ERROR_RECORDINGS_DIR_UNAVAILABLE = "STORAGE_ERROR_RECORDINGS_DIR_UNAVAILABLE"
STORAGE_ERROR_RECORDINGS_DIR_NOT_ON_STORAGE = (
    "STORAGE_ERROR_RECORDINGS_DIR_NOT_ON_STORAGE"
)
STORAGE_ERROR_RECORDINGS_DIR_NOT_WRITABLE = "STORAGE_ERROR_RECORDINGS_DIR_NOT_WRITABLE"
RECORDING_SKIPPED_LOW_STORAGE = "RECORDING_SKIPPED_LOW_STORAGE"
RECORDING_SKIPPED_RECORDINGS_QUOTA_EXCEEDED = (
    "RECORDING_SKIPPED_RECORDINGS_QUOTA_EXCEEDED"
)
RECORDING_SKIPPED_INSUFFICIENT_SPACE_FOR_EXPECTED_FILE = (
    "RECORDING_SKIPPED_INSUFFICIENT_SPACE_FOR_EXPECTED_FILE"
)
RECORDING_SKIPPED_INVALID_AUDIO_CONFIG = "RECORDING_SKIPPED_INVALID_AUDIO_CONFIG"
RECORDING_STOPPED_MAX_DURATION = "RECORDING_STOPPED_MAX_DURATION"
RECORDING_STOPPED_MAX_FILE_SIZE = "RECORDING_STOPPED_MAX_FILE_SIZE"
RECORDING_STOPPED_AUDIO_ERROR = "RECORDING_STOPPED_AUDIO_ERROR"
RECORDING_INTERRUPTED = "RECORDING_INTERRUPTED"


@dataclass(frozen=True)
class RecordingSizeEstimate:
    expected_audio_bytes: int
    expected_wav_bytes: int
    max_file_size_bytes: int


@dataclass(frozen=True)
class DirectoryValidation:
    ok: bool
    path: Path
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StorageAdmission:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    expected_size_bytes: int = 0
    max_file_size_bytes: int = 0
    free_bytes_before: int | None = None
    free_bytes_after_reservation: int | None = None
    recordings_dir_used_bytes: int | None = None


def gib_to_bytes(value: int | float) -> int:
    return int(float(value) * GIB_BYTES)


def estimate_wav_pcm_size_bytes(
    *,
    sample_rate_hz: int | float,
    bits_per_sample: int,
    channels: int,
    duration_s: int | float,
) -> int:
    sample_rate = float(sample_rate_hz)
    duration = float(duration_s)
    bits = int(bits_per_sample)
    channel_count = int(channels)
    if (
        sample_rate <= 0
        or duration <= 0
        or bits <= 0
        or bits % 8 != 0
        or channel_count <= 0
    ):
        raise ValueError("Invalid WAV PCM sizing parameters")
    bytes_per_sample = bits // 8
    expected_audio_bytes = int(
        sample_rate * channel_count * bytes_per_sample * duration
    )
    return expected_audio_bytes + WAV_HEADER_BYTES


def estimate_recording_size(
    *,
    sample_rate_hz: int | float,
    bits_per_sample: int,
    channels: int,
    duration_s: int | float,
    margin_factor: int | float,
) -> RecordingSizeEstimate:
    expected_wav_bytes = estimate_wav_pcm_size_bytes(
        sample_rate_hz=sample_rate_hz,
        bits_per_sample=bits_per_sample,
        channels=channels,
        duration_s=duration_s,
    )
    expected_audio_bytes = expected_wav_bytes - WAV_HEADER_BYTES
    margin = float(margin_factor)
    if margin < 1.0:
        raise ValueError("margin_factor must be >= 1.0")
    return RecordingSizeEstimate(
        expected_audio_bytes=expected_audio_bytes,
        expected_wav_bytes=expected_wav_bytes,
        max_file_size_bytes=int(expected_wav_bytes * margin),
    )


def get_directory_size_bytes(path: str | os.PathLike[str]) -> int:
    root = Path(path)
    total = 0
    if not root.exists():
        return 0
    for current_root, _, filenames in os.walk(root):
        for filename in filenames:
            file_path = Path(current_root) / filename
            try:
                total += file_path.stat().st_size
            except OSError:
                continue
    return total


def validate_recordings_dir(
    recordings_dir: str | os.PathLike[str],
    *,
    storage_root: str | os.PathLike[str] = "/storage",
    create: bool = True,
) -> DirectoryValidation:
    path = Path(recordings_dir)
    storage_path = Path(storage_root)
    errors: list[str] = []

    try:
        resolved_parent = path.resolve(strict=False)
        resolved_storage = storage_path.resolve(strict=False)
        resolved_parent.relative_to(resolved_storage)
    except ValueError:
        errors.append(STORAGE_ERROR_RECORDINGS_DIR_NOT_ON_STORAGE)

    try:
        if create:
            path.mkdir(parents=True, exist_ok=True)
        if not path.exists() or not path.is_dir():
            errors.append(STORAGE_ERROR_RECORDINGS_DIR_UNAVAILABLE)
    except OSError:
        errors.append(STORAGE_ERROR_RECORDINGS_DIR_UNAVAILABLE)

    if not errors:
        test_path = path / ".storage_guard_write_test"
        try:
            with open(test_path, "w", encoding="utf-8") as handle:
                handle.write("ok")
            test_path.unlink(missing_ok=True)
        except OSError:
            errors.append(STORAGE_ERROR_RECORDINGS_DIR_NOT_WRITABLE)

    return DirectoryValidation(ok=not errors, path=path, errors=errors)


def evaluate_free_space_warnings(
    free_bytes: int,
    *,
    warning_bytes: int,
    critical_bytes: int,
) -> list[str]:
    warnings: list[str] = []
    if free_bytes < int(critical_bytes):
        warnings.append(STORAGE_CRITICAL_LOW_FREE_SPACE)
    elif free_bytes < int(warning_bytes):
        warnings.append(STORAGE_WARNING_LOW_FREE_SPACE)
    return warnings


def evaluate_file_size_limit(
    current_file_size_bytes: int,
    *,
    max_file_size_bytes: int,
) -> list[str]:
    if int(current_file_size_bytes) > int(max_file_size_bytes):
        return [RECORDING_STOPPED_MAX_FILE_SIZE]
    return []


def evaluate_storage_admission(
    *,
    recordings_dir_used_bytes: int,
    free_bytes: int,
    expected_size_bytes: int,
    max_file_size_bytes: int,
    max_recordings_dir_bytes: int,
    min_free_warning_bytes: int,
    min_free_critical_bytes: int,
    hard_reserve_bytes: int,
    directory_errors: Iterable[str] = (),
) -> StorageAdmission:
    errors = list(directory_errors)
    warnings = evaluate_free_space_warnings(
        int(free_bytes),
        warning_bytes=int(min_free_warning_bytes),
        critical_bytes=int(min_free_critical_bytes),
    )
    free_after = int(free_bytes) - int(max_file_size_bytes)

    if int(max_file_size_bytes) <= 0 or int(expected_size_bytes) <= 0:
        errors.append(RECORDING_SKIPPED_INVALID_AUDIO_CONFIG)

    if int(recordings_dir_used_bytes) + int(max_file_size_bytes) > int(
        max_recordings_dir_bytes
    ):
        errors.append(RECORDING_SKIPPED_RECORDINGS_QUOTA_EXCEEDED)

    if free_after < int(hard_reserve_bytes):
        errors.append(RECORDING_SKIPPED_LOW_STORAGE)
        if int(free_bytes) < int(max_file_size_bytes):
            errors.append(RECORDING_SKIPPED_INSUFFICIENT_SPACE_FOR_EXPECTED_FILE)

    return StorageAdmission(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        expected_size_bytes=int(expected_size_bytes),
        max_file_size_bytes=int(max_file_size_bytes),
        free_bytes_before=int(free_bytes),
        free_bytes_after_reservation=free_after,
        recordings_dir_used_bytes=int(recordings_dir_used_bytes),
    )


def disk_free_bytes(path: str | os.PathLike[str]) -> int:
    return int(shutil.disk_usage(path).free)
