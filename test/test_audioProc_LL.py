import json
from pathlib import Path

import pytest

from modules.audioProc_LL import AudioProcLowLevel, TEST_WAV_PATH


def test_audio_proc_processes_real_fixture_wav():
    if TEST_WAV_PATH is None:
        pytest.skip("real AudioProc fixture WAV is not available")

    ll = AudioProcLowLevel()
    assert ll.init() is True

    output_path = Path(ll.process(TEST_WAV_PATH))

    assert output_path.parent.name == "audio_proc"
    assert output_path.name.startswith("audioProc_")
    assert output_path.suffix == ".json"

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert set(payload) == {"timestamp", "relative_band_power_db"}
    assert payload["timestamp"].endswith("Z")
    assert isinstance(payload["relative_band_power_db"], list)
    assert len(payload["relative_band_power_db"]) > 0
    assert any(
        value is not None
        for row in payload["relative_band_power_db"]
        for value in row
    )
