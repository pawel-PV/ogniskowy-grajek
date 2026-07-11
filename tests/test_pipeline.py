from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from ogniskowy_grajek import pipeline as pipeline_module
from ogniskowy_grajek.audio import RhythmAnalysis, StemPaths
from ogniskowy_grajek.ingest import IngestError, VideoMetadata
from ogniskowy_grajek.llm import TransformOutcome
from ogniskowy_grajek.models import AnalysisMode, ChordDetector, SimplificationMode
from ogniskowy_grajek.music import ChordSegment
from ogniskowy_grajek.pipeline import (
    PipelineError,
    PipelineSettings,
    SongPipeline,
    format_timestamp,
    purge_orphan_workspaces,
)


def test_format_timestamp() -> None:
    assert format_timestamp(0) == "00:00"
    assert format_timestamp(65.2) == "01:05"


def test_settings_read_documented_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APP_DATABASE_PATH", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.setenv("APP_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("AUDIO_DEVICE", "cpu")
    settings = PipelineSettings.from_env()
    assert settings.database_path == tmp_path / "jobs.sqlite3"
    assert settings.work_root == tmp_path / "work"
    assert settings.audio_device == "cpu"


def test_sweeper_removes_only_old_directories(tmp_path: Path) -> None:
    old = tmp_path / "old"
    fresh = tmp_path / "fresh"
    old.mkdir()
    fresh.mkdir()
    timestamp = time.time() - 7200
    os.utime(old, (timestamp, timestamp))
    assert purge_orphan_workspaces(tmp_path, older_than_seconds=3600) == 1
    assert not old.exists()
    assert fresh.exists()


def _mock_successful_analysis(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        pipeline_module,
        "probe_video",
        lambda *_args, **_kwargs: VideoMetadata(
            video_id="dQw4w9WgXcQ",
            title="Próbka",
            duration_seconds=16,
            webpage_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
    )

    def download(_url, workspace, **_kwargs):
        path = workspace / "input.wav"
        path.write_bytes(b"wav")
        return path

    def tracks(_stems, workspace):
        harmonic = workspace / "harmonic.wav"
        drums = workspace / "drums.wav"
        harmonic.write_bytes(b"wav")
        drums.write_bytes(b"wav")
        return harmonic, drums

    monkeypatch.setattr(pipeline_module, "download_wav", download)
    monkeypatch.setattr(pipeline_module, "prepare_analysis_tracks", tracks)
    monkeypatch.setattr(
        pipeline_module,
        "analyze_rhythm",
        lambda _path: RhythmAnalysis(
            bpm=120,
            beat_times=np.arange(16, dtype=float) * 0.5,
            beat_strengths=np.tile(np.array([4.0, 1.0, 1.0, 1.0]), 4),
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "run_chordino",
        lambda *_args, **_kwargs: [(0, "C"), (4, "G"), (8, "Am"), (12, "F")],
    )
    monkeypatch.setattr(
        pipeline_module,
        "transform",
        lambda **kwargs: TransformOutcome(
            kwargs["deterministic"],
            SimplificationMode.DETERMINISTIC,
            "deterministic:v1",
        ),
    )


def test_pipeline_retries_cuda_once_on_cpu_and_cleans_workspace(monkeypatch, tmp_path: Path) -> None:
    _mock_successful_analysis(monkeypatch, tmp_path)
    devices: list[str] = []
    monkeypatch.setattr(pipeline_module, "cuda_preflight", lambda: (True, "test gpu"))

    timeouts: list[int] = []

    def demucs(_input, workspace, *, device, timeout_seconds, **_kwargs):
        devices.append(device)
        timeouts.append(timeout_seconds)
        if device == "cuda":
            raise RuntimeError("simulated CUDA OOM")
        return StemPaths(workspace / "drums.wav", workspace / "bass.wav", workspace / "other.wav")

    monkeypatch.setattr(pipeline_module, "run_demucs", demucs)
    settings = PipelineSettings(
        work_root=tmp_path / "work",
        database_path=tmp_path / "jobs.sqlite3",
        audio_device="auto",
    )

    result = SongPipeline(settings).run(
        job_id="job-cuda-cpu",
        source_url="https://youtu.be/dQw4w9WgXcQ",
        progress=lambda *_args: None,
    )

    assert devices == ["cuda", "cpu"]
    assert timeouts[0] <= 600
    assert timeouts[1] <= 1500
    assert result.processing.analysis_mode is AnalysisMode.DEMUCS_CPU
    assert any("CUDA" in warning for warning in result.processing.warnings)
    assert not (settings.work_root / "job-cuda-cpu").exists()


def test_pipeline_falls_back_to_hpss_and_chroma_with_unconditional_cleanup(
    monkeypatch, tmp_path: Path
) -> None:
    _mock_successful_analysis(monkeypatch, tmp_path)
    monkeypatch.setattr(
        pipeline_module,
        "run_demucs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("demucs", 1)),
    )

    def approximate(_input, workspace):
        return (workspace / "harmonic-approx.wav", workspace / "drums-approx.wav")

    monkeypatch.setattr(pipeline_module, "prepare_approximate_tracks", approximate)
    monkeypatch.setattr(
        pipeline_module,
        "run_chordino",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("no chordino")),
    )
    monkeypatch.setattr(
        pipeline_module,
        "chroma_fallback",
        lambda _path: (np.ones((12, 4)), np.array([0.0, 4.0, 8.0, 16.2])),
    )
    observed_times: list[float] = []

    def detected(_chroma, times, **_kwargs):
        observed_times.extend(times.tolist())
        return [
            ChordSegment(0, 4, "C"),
            ChordSegment(4, 8, "G"),
            ChordSegment(8, 12, "Am"),
            ChordSegment(12, 16, "F"),
        ]

    monkeypatch.setattr(
        pipeline_module,
        "detect_chords_from_chroma",
        detected,
    )
    settings = PipelineSettings(
        work_root=tmp_path / "work",
        database_path=tmp_path / "jobs.sqlite3",
        audio_device="cpu",
    )

    def progress(stage, _percent, _message):
        if stage == "CLEANING_UP":
            raise RuntimeError("job was cancelled")

    result = SongPipeline(settings).run(
        job_id="job-approx",
        source_url="https://youtu.be/dQw4w9WgXcQ",
        progress=progress,
    )

    assert result.processing.analysis_mode is AnalysisMode.MIX_APPROXIMATE
    assert result.processing.chord_detector is ChordDetector.LIBROSA_TEMPLATE
    assert observed_times == [0.0, 4.0, 8.0]
    assert not (settings.work_root / "job-approx").exists()


def test_pipeline_sanitizes_ingest_failure_and_removes_workspace(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        pipeline_module,
        "probe_video",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(IngestError("AGE_RESTRICTED", "safe message")),
    )
    settings = PipelineSettings(
        work_root=tmp_path / "work",
        database_path=tmp_path / "jobs.sqlite3",
    )

    with pytest.raises(PipelineError) as captured:
        SongPipeline(settings).run(
            job_id="job-invalid",
            source_url="https://youtu.be/dQw4w9WgXcQ",
            progress=lambda *_args: None,
        )

    assert captured.value.code == "AGE_RESTRICTED"
    assert not (settings.work_root / "job-invalid").exists()
