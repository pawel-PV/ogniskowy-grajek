from __future__ import annotations

import csv
import io
import os
import re
import selectors
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ProgressCallback = Callable[[float, str], None]


class AudioProcessingError(RuntimeError):
    pass


@dataclass(frozen=True)
class StemPaths:
    drums: Path
    bass: Path
    other: Path
    vocals: Path


@dataclass(frozen=True)
class RhythmAnalysis:
    bpm: int
    beat_times: np.ndarray
    beat_strengths: np.ndarray


def cuda_preflight(*, minimum_free_mb: int = 8000) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        free_values = [int(value.strip()) for value in result.stdout.splitlines() if value.strip()]
        if not free_values or max(free_values) < minimum_free_mb:
            return False, "Za mało wolnej pamięci VRAM"
        import torch

        if not torch.cuda.is_available():
            return False, "PyTorch nie widzi CUDA"
        tensor = torch.ones((256, 256), device="cuda")
        float((tensor @ tensor).sum().item())
        torch.cuda.synchronize()
        del tensor
        torch.cuda.empty_cache()
        return True, str(torch.cuda.get_device_name(0))
    except Exception as exc:
        return False, f"CUDA niedostępna: {exc.__class__.__name__}"


def run_demucs(
    input_wav: Path,
    output_root: Path,
    *,
    device: str,
    timeout_seconds: int,
    progress: ProgressCallback,
) -> StemPaths:
    output_root.mkdir(parents=True, exist_ok=True)
    command = _demucs_command(input_wav, output_root, device=device)
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    _monitor_demucs(
        process,
        command,
        device=device,
        timeout_seconds=timeout_seconds,
        progress=progress,
        started=started,
    )
    stem_dir = output_root / "htdemucs" / input_wav.stem
    required = {name: stem_dir / f"{name}.wav" for name in ("drums", "bass", "other", "vocals")}
    if not all(path.is_file() and path.stat().st_size > 0 for path in required.values()):
        raise AudioProcessingError("Demucs did not produce required stems")
    progress(1.0, f"Separacja Demucs zakończona ({device.upper()})")
    return StemPaths(**required)


def _demucs_command(input_wav: Path, output_root: Path, *, device: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "demucs.separate",
        "-n",
        "htdemucs",
        "-d",
        device,
        "--shifts",
        "1",
        "--overlap",
        "0.1",
        "--segment",
        # Demucs 4.0.1 exposes this CLI option as an integer even though the
        # htdemucs architecture limit is 7.8 seconds.
        "7",
        "--float32",
        "--out",
        os.fspath(output_root),
        os.fspath(input_wav),
    ]


def _monitor_demucs(
    process: subprocess.Popen[bytes],
    command: list[str],
    *,
    device: str,
    timeout_seconds: int,
    progress: ProgressCallback,
    started: float,
) -> None:
    percent_re = re.compile(rb"(\d{1,3})%")
    selector = selectors.DefaultSelector()
    try:
        assert process.stdout is not None
        selector.register(process.stdout, selectors.EVENT_READ)
        eof = False
        while not eof or process.poll() is None:
            if time.monotonic() - started > timeout_seconds:
                raise subprocess.TimeoutExpired(command, timeout_seconds)
            for key, _events in selector.select(timeout=0.5):
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    eof = True
                    selector.unregister(key.fileobj)
                    break
                matches = list(percent_re.finditer(chunk))
                if matches:
                    percent = int(matches[-1].group(1))
                    progress(
                        min(0.99, percent / 100),
                        f"Separacja Demucs ({device.upper()})",
                    )
        return_code = process.wait(timeout=max(1, timeout_seconds - int(time.monotonic() - started)))
        if return_code != 0:
            raise AudioProcessingError(f"Demucs exited with code {return_code}")
    except Exception:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        raise
    finally:
        selector.close()


def _ffmpeg(command: list[str], *, timeout: int = 180) -> None:
    try:
        subprocess.run(command, check=True, timeout=timeout, capture_output=True)
    except (subprocess.SubprocessError, OSError) as exc:
        raise AudioProcessingError("FFmpeg processing failed") from exc


def prepare_analysis_tracks(stems: StemPaths, workspace: Path) -> tuple[Path, Path]:
    harmonic = workspace / "harmonic.wav"
    drums_mono = workspace / "drums_mono.wav"
    _ffmpeg(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            os.fspath(stems.bass),
            "-i",
            os.fspath(stems.other),
            "-filter_complex",
            "amix=inputs=2:normalize=0",
            "-ac",
            "1",
            "-ar",
            "44100",
            "-c:a",
            "pcm_f32le",
            os.fspath(harmonic),
        ]
    )
    _ffmpeg(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            os.fspath(stems.drums),
            "-ac",
            "1",
            "-ar",
            "44100",
            "-c:a",
            "pcm_f32le",
            os.fspath(drums_mono),
        ]
    )
    return harmonic, drums_mono


def prepare_approximate_tracks(input_wav: Path, workspace: Path) -> tuple[Path, Path]:
    import librosa
    import soundfile as sf

    signal, sample_rate = librosa.load(input_wav, sr=44100, mono=True)
    harmonic_signal, percussive_signal = librosa.effects.hpss(signal)
    harmonic = workspace / "harmonic_approx.wav"
    drums = workspace / "drums_approx.wav"
    sf.write(harmonic, harmonic_signal, sample_rate, subtype="FLOAT")
    sf.write(drums, percussive_signal, sample_rate, subtype="FLOAT")
    return harmonic, drums


def analyze_rhythm(drums_wav: Path) -> RhythmAnalysis:
    import librosa

    signal, sample_rate = librosa.load(drums_wav, sr=22050, mono=True)
    onset = librosa.onset.onset_strength(y=signal, sr=sample_rate)
    tempo, beat_frames = librosa.beat.beat_track(onset_envelope=onset, sr=sample_rate, units="frames")
    tempo_value = float(np.asarray(tempo).reshape(-1)[0]) if np.asarray(tempo).size else 120.0
    if not np.isfinite(tempo_value) or tempo_value <= 0:
        tempo_value = 120.0
    while tempo_value < 40:
        tempo_value *= 2
    while tempo_value > 240:
        tempo_value /= 2
    beat_frames = np.asarray(beat_frames, dtype=int)
    beat_times = librosa.frames_to_time(beat_frames, sr=sample_rate)
    safe_frames = beat_frames[(beat_frames >= 0) & (beat_frames < len(onset))]
    strengths = onset[safe_frames] if safe_frames.size else np.array([], dtype=float)
    return RhythmAnalysis(int(round(tempo_value)), np.asarray(beat_times), np.asarray(strengths))


def run_chordino(harmonic_wav: Path, *, timeout_seconds: int = 300) -> list[tuple[float, str]]:
    executable = shutil.which("sonic-annotator")
    if not executable:
        raise AudioProcessingError("sonic-annotator is not installed")
    command = [
        executable,
        "-d",
        "vamp:nnls-chroma:chordino:simplechord",
        os.fspath(harmonic_wav),
        "-w",
        "csv",
        "--csv-stdout",
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout_seconds)
    except (subprocess.SubprocessError, OSError) as exc:
        raise AudioProcessingError("Chordino analysis failed") from exc
    events: list[tuple[float, str]] = []
    for row in csv.reader(io.StringIO(result.stdout)):
        if len(row) < 3:
            continue
        try:
            timestamp = float(row[-2])
        except ValueError:
            continue
        label = row[-1].strip().strip('"')
        if label:
            events.append((timestamp, label))
    if not events:
        raise AudioProcessingError("Chordino returned no chord events")
    return events


def chroma_fallback(harmonic_wav: Path) -> tuple[np.ndarray, np.ndarray]:
    import librosa

    signal, sample_rate = librosa.load(harmonic_wav, sr=22050, mono=True)
    hop_length = 2048
    chroma = librosa.feature.chroma_cqt(y=signal, sr=sample_rate, hop_length=hop_length)
    times = librosa.frames_to_time(np.arange(chroma.shape[1]), sr=sample_rate, hop_length=hop_length)
    return chroma, times
