import json
import math
from pathlib import Path

import pytest

from modules.audioProc_LL import AudioProcLowLevel, PROJECT_ROOT, TEST_WAV_PATH


def test_audio_proc_processes_real_fixture_wav():
    if TEST_WAV_PATH is None:
        pytest.skip("real AudioProc fixture WAV is not available")

    ll = AudioProcLowLevel()
    assert ll.init() is True

    passed, report = ll.full_test()

    assert passed is True, report
    assert report["details"]["comparison"] == "AudioProc JSON output matches expected pattern."

    output_path = Path(report["details"]["output_path"])
    assert output_path == PROJECT_ROOT / "test" / "test_proc" / "audioProc_actual.json"
    assert output_path.suffix == ".json"

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert set(payload) == {"timestamp", "relative_band_power_db"}
    assert payload["timestamp"].endswith("-03:00")
    powers = payload["relative_band_power_db"]
    assert isinstance(powers, list)
    assert len(powers) > 0
    assert all(isinstance(row, list) and row for row in powers)
    assert all(value is not None for row in powers for value in row)
    assert all(math.isfinite(value) for row in powers for value in row)
