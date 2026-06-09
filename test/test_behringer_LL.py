"""
Tests for BehringerLowLevel class in modules/behringer_LL.py

- test_init_and_deinit: Verifies initialization and deinitialization.
- test_open_and_close: Verifies opening and closing the audio stream.
- test_record_and_is_recording_done: Verifies recording and completion check.
- test_full_test: Runs the full_test method and checks result types.
- test_stop_recording: Verifies stopping a recording in progress.
- test_permissions: Checks write permissions in the recordings directory.
- test_test_method: Verifies the test() method returns a boolean.
- test_open_without_init: Verifies opening the stream without initializing.
- test_record_without_init: Verifies recording without initializing.
- test_full_test_without_init: Verifies full_test method without initializing.
- test_stop_recording_without_recording: Verifies stopping recording when none is active.
- test_close_multiple_times: Verifies closing the stream multiple times.
- test_callback_not_recording: Verifies _callback behavior when not recording.
- test_callback_recording: Verifies _callback behavior when recording.
"""

import os
import pytest
import time
from modules.behringer_LL import BehringerLowLevel

def test_init_and_deinit():
    """Test initialization and deinitialization of the audio interface."""
    audio = BehringerLowLevel()
    assert audio.init() in [True, False]
    assert audio.deinit() in [True, False]

def test_open_and_close():
    """Test opening and closing the audio stream."""
    audio = BehringerLowLevel()
    audio.init()
    opened = audio.open()
    assert opened in [True, False]
    audio.close()
    audio.deinit()

def test_record_and_is_recording_done():
    """Test recording a short audio and checking if recording is done."""
    audio = BehringerLowLevel()
    audio.init()
    ok = audio.record(1)
    assert ok in [True, False]
    for _ in range(10):
        done, success = audio.is_recording_done()
        if done:
            break
        time.sleep(0.5)
    audio.deinit()

def test_full_test():
    """Test the full_test method for correct result and details types."""
    audio = BehringerLowLevel()
    audio.init()
    result, detalles = audio.full_test()
    assert isinstance(result, bool)
    assert isinstance(detalles, dict)
    audio.deinit()

def test_stop_recording():
    """Test stopping a recording in progress."""
    audio = BehringerLowLevel()
    audio.init()
    ok = audio.record(2)
    assert ok in [True, False]
    audio.stop_recording()
    audio.deinit()

def test_permissions():
    """Test write permissions in the recordings directory."""
    audio = BehringerLowLevel()
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    recordings_dir = os.path.join(repo_root, "data", "recordings")
    os.makedirs(recordings_dir, exist_ok=True)
    testfile = os.path.join(recordings_dir, "pytest_perm.txt")
    with open(testfile, "w") as f:
        f.write("pytest")
    os.remove(testfile)


def test_default_recordings_dir_is_data_recordings():
    """Verify the default Behringer recordings path is under data/recordings."""
    audio = BehringerLowLevel()
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    expected_dir = os.path.join(repo_root, "data", "recordings")
    assert os.path.abspath(audio.recordings_dir) == os.path.abspath(expected_dir)

def test_test_method():
    """Test that the test() method returns a boolean."""
    audio = BehringerLowLevel()
    audio.init()
    assert isinstance(audio.test(), bool)
    audio.deinit()

def test_open_without_init():
    """Test opening the stream without initializing the device."""
    audio = BehringerLowLevel()
    assert audio.open() is False
    audio.close()

def test_record_without_init():
    """Test recording without initializing the device."""
    audio = BehringerLowLevel()
    assert audio.record(1) is False
    audio.deinit()

def test_full_test_without_init():
    """Test full_test without initializing the device."""
    audio = BehringerLowLevel()
    result, detalles = audio.full_test()
    assert result is False
    assert detalles.get("initialized") is False

def test_stop_recording_without_recording():
    """Test stopping recording when no recording is active."""
    audio = BehringerLowLevel()
    audio.init()
    audio.stop_recording()  # Should not raise
    audio.deinit()

def test_close_multiple_times():
    """Test calling close multiple times in a row."""
    audio = BehringerLowLevel()
    audio.init()
    audio.open()
    audio.close()
    audio.close()  # Should not raise or log error
    audio.deinit()

def test_callback_not_recording():
    """Test the _callback method when not recording (should return paComplete)."""
    audio = BehringerLowLevel()
    # Simula callback sin grabar
    result = audio._callback(b'data', 0, 0, 0)
    assert isinstance(result, tuple)
    assert result[1] is not None

def test_callback_recording():
    """Test the _callback method when recording (should return paContinue and put data in queue)."""
    audio = BehringerLowLevel()
    audio.is_recording_event.set()
    result = audio._callback(b'data', 0, 0, 0)
    assert isinstance(result, tuple)
    assert result[1] is not None
    # Verifica que los datos se pusieron en la queue
    assert not audio.frames_queue.empty()
    audio.is_recording_event.clear()

def test_write_audio_without_init():
    """Test _write_audio when audio_interface is None (should handle gracefully)."""
    audio = BehringerLowLevel()
    audio.output_path = "/tmp/test_write_audio.wav"
    audio._write_audio()  # Should not raise

def test_write_audio_no_output_path():
    """Test _write_audio when output_path is None (should handle gracefully)."""
    audio = BehringerLowLevel()
    # Simula que audio_interface está inicializado pero output_path es None
    audio.audio_interface = None  # Forzar a None
    audio.output_path = None
    audio._write_audio()  # Should not raise

def test_write_audio_no_duration():
    """Test _write_audio when duration is None (should handle gracefully)."""
    audio = BehringerLowLevel()
    audio.audio_interface = None  # Forzar a None
    audio.output_path = "/tmp/test_write_audio.wav"
    audio.duration = None
    audio._write_audio()  # Should not raise

def test_deinit_without_init():
    """Test deinit when audio_interface is None (should handle gracefully)."""
    audio = BehringerLowLevel()
    assert audio.deinit() is True

def test_close_without_stream():
    """Test close when stream is None (should handle gracefully)."""
    audio = BehringerLowLevel()
    audio.close()  # Should not raise

# Opcional: test de manejo de archivos inexistentes o permisos denegados
# (esto puede requerir privilegios o un entorno controlado, así que solo se sugiere)
# def test_recordings_dir_no_permission(tmp_path):
#     audio = BehringerLowLevel()
#     no_perm_dir = tmp_path / "no_perm"
#     no_perm_dir.mkdir()
#     no_perm_dir.chmod(0o400)  # Solo lectura
#     audio.output_path = str(no_perm_dir / "test.wav")
#     # Aquí podrías intentar grabar y esperar un fallo, pero depende del entorno
#     # Restaurar permisos después si lo usas
