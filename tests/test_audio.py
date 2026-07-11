from __future__ import annotations

import sys
from types import SimpleNamespace

import numpy as np

from ogniskowy_grajek.audio import _demucs_command, analyze_rhythm


def test_demucs_uses_integer_segment_supported_by_v4(tmp_path) -> None:
    command = _demucs_command(tmp_path / "input.wav", tmp_path / "out", device="cpu")

    segment_index = command.index("--segment")
    assert command[segment_index + 1] == "7"
    assert command[command.index("--overlap") + 1] == "0.1"
    assert command[command.index("--shifts") + 1] == "1"


def test_silent_audio_uses_finite_tempo_fallback(monkeypatch, tmp_path) -> None:
    fake_librosa = SimpleNamespace(
        load=lambda *_args, **_kwargs: (np.zeros(64), 22_050),
        onset=SimpleNamespace(onset_strength=lambda **_kwargs: np.zeros(4)),
        beat=SimpleNamespace(beat_track=lambda **_kwargs: (np.array([0.0]), np.array([], dtype=int))),
        frames_to_time=lambda frames, **_kwargs: np.asarray(frames, dtype=float),
    )
    monkeypatch.setitem(sys.modules, "librosa", fake_librosa)

    result = analyze_rhythm(tmp_path / "silence.wav")

    assert result.bpm == 120
    assert result.beat_times.size == 0
    assert result.beat_strengths.size == 0
