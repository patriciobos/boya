from pathlib import Path

from modules.support.storage_guard import (
    RECORDING_SKIPPED_LOW_STORAGE,
    RECORDING_SKIPPED_RECORDINGS_QUOTA_EXCEEDED,
    RECORDING_STOPPED_MAX_FILE_SIZE,
    STORAGE_CRITICAL_LOW_FREE_SPACE,
    STORAGE_ERROR_RECORDINGS_DIR_NOT_ON_STORAGE,
    STORAGE_WARNING_LOW_FREE_SPACE,
    estimate_recording_size,
    evaluate_file_size_limit,
    evaluate_free_space_warnings,
    evaluate_storage_admission,
    get_directory_size_bytes,
    gib_to_bytes,
    validate_recordings_dir,
)


def test_gib_to_bytes_uses_binary_units():
    assert gib_to_bytes(860) == 923417968640
    assert gib_to_bytes(100) == 107374182400
    assert gib_to_bytes(50) == 53687091200
    assert gib_to_bytes(10) == 10737418240


def test_estimate_recording_size_uses_written_channels_and_margin():
    estimate = estimate_recording_size(
        sample_rate_hz=192000,
        bits_per_sample=24,
        channels=2,
        duration_s=60,
        margin_factor=1.10,
    )

    assert estimate.expected_audio_bytes == 69120000
    assert estimate.expected_wav_bytes == 69120044
    assert estimate.max_file_size_bytes == int(69120044 * 1.10)


def test_directory_size_sums_nested_files(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"1234")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "b.bin").write_bytes(b"123456")

    assert get_directory_size_bytes(tmp_path) == 10


def test_validate_recordings_dir_requires_storage_root(tmp_path):
    recordings = tmp_path / "recordings"

    result = validate_recordings_dir(recordings, storage_root="/storage", create=False)

    assert result.ok is False
    assert STORAGE_ERROR_RECORDINGS_DIR_NOT_ON_STORAGE in result.errors


def test_validate_recordings_dir_accepts_writable_storage_root(tmp_path):
    storage_root = tmp_path / "storage"
    recordings = storage_root / "boya" / "recordings"

    result = validate_recordings_dir(recordings, storage_root=storage_root, create=True)

    assert result.ok is True
    assert result.errors == []
    assert recordings.exists()


def test_free_space_warnings_are_non_blocking_levels():
    assert evaluate_free_space_warnings(
        gib_to_bytes(75),
        warning_bytes=gib_to_bytes(100),
        critical_bytes=gib_to_bytes(50),
    ) == [STORAGE_WARNING_LOW_FREE_SPACE]
    assert evaluate_free_space_warnings(
        gib_to_bytes(25),
        warning_bytes=gib_to_bytes(100),
        critical_bytes=gib_to_bytes(50),
    ) == [STORAGE_CRITICAL_LOW_FREE_SPACE]


def test_admission_allows_critical_free_space_when_hard_reserve_survives():
    admission = evaluate_storage_admission(
        recordings_dir_used_bytes=0,
        free_bytes=gib_to_bytes(50) - 1,
        expected_size_bytes=gib_to_bytes(1),
        max_file_size_bytes=gib_to_bytes(1),
        max_recordings_dir_bytes=gib_to_bytes(860),
        min_free_warning_bytes=gib_to_bytes(100),
        min_free_critical_bytes=gib_to_bytes(50),
        hard_reserve_bytes=gib_to_bytes(10),
    )

    assert admission.ok is True
    assert admission.errors == []
    assert admission.warnings == [STORAGE_CRITICAL_LOW_FREE_SPACE]


def test_admission_rejects_when_post_reservation_breaks_hard_reserve():
    admission = evaluate_storage_admission(
        recordings_dir_used_bytes=0,
        free_bytes=gib_to_bytes(11),
        expected_size_bytes=gib_to_bytes(2),
        max_file_size_bytes=gib_to_bytes(2),
        max_recordings_dir_bytes=gib_to_bytes(860),
        min_free_warning_bytes=gib_to_bytes(100),
        min_free_critical_bytes=gib_to_bytes(50),
        hard_reserve_bytes=gib_to_bytes(10),
    )

    assert admission.ok is False
    assert RECORDING_SKIPPED_LOW_STORAGE in admission.errors


def test_admission_rejects_when_recordings_quota_would_be_exceeded():
    admission = evaluate_storage_admission(
        recordings_dir_used_bytes=gib_to_bytes(859),
        free_bytes=gib_to_bytes(100),
        expected_size_bytes=gib_to_bytes(2),
        max_file_size_bytes=gib_to_bytes(2),
        max_recordings_dir_bytes=gib_to_bytes(860),
        min_free_warning_bytes=gib_to_bytes(100),
        min_free_critical_bytes=gib_to_bytes(50),
        hard_reserve_bytes=gib_to_bytes(10),
    )

    assert admission.ok is False
    assert RECORDING_SKIPPED_RECORDINGS_QUOTA_EXCEEDED in admission.errors


def test_file_size_limit_warns_only_after_limit_is_exceeded():
    assert evaluate_file_size_limit(100, max_file_size_bytes=100) == []
    assert evaluate_file_size_limit(101, max_file_size_bytes=100) == [RECORDING_STOPPED_MAX_FILE_SIZE]
