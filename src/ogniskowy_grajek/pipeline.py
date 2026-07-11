from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .audio import (
    analyze_rhythm,
    chroma_fallback,
    cuda_preflight,
    prepare_analysis_tracks,
    prepare_approximate_tracks,
    run_chordino,
    run_demucs,
)
from .ingest import IngestError, download_wav, probe_video
from .llm import BudgetLedger, transform
from .lyrics import (
    LyricsError,
    Transcript,
    build_songbook,
    download_subtitle_json3,
    parse_youtube_json3,
    run_local_asr,
)
from .models import (
    AnalysisMode,
    AnalysisResult,
    Arrangement,
    ChordDetector,
    CleanupStatus,
    LLMChord,
    LLMTransformation,
    LyricsSource,
    ProcessingInfo,
    SongSection,
    SourceInfo,
    TimelineEvent,
)
from .music import (
    ChordSegment,
    choose_capo,
    chord_shape_for_capo,
    detect_chords_from_chroma,
    group_four_bar_sections,
    infer_meter,
    normalize_chord,
    parse_chordino_rows,
    smooth_chord_segments,
    strumming_pattern,
)

ProgressCallback = Callable[[str, int, str], None]
BEGINNER_CHORDS = {"A", "Am", "C", "D", "Dm", "E", "Em", "Fmaj7", "G"}


class PipelineError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message
        self.retryable = retryable


@dataclass(frozen=True)
class PipelineSettings:
    work_root: Path
    database_path: Path
    pipeline_version: str = "2.0.0"
    audio_device: str = "cpu"
    max_duration_seconds: int = 600
    max_download_bytes: int = 100 * 1024 * 1024
    timeout_seconds: int = 1800
    result_ttl_hours: int = 24
    ollama_base_url: str = "http://host.docker.internal:11434"
    ollama_model: str = "llama3:8b"
    gemini_model: str = "gemini-3.1-flash-lite"
    gemini_api_key: str = ""
    gemini_api_key_paid: str = ""
    gemini_daily_budget_usd: float = 1.0
    subtitle_max_bytes: int = 2 * 1024 * 1024
    asr_device: str = "cpu"
    asr_model_path: str = "/opt/whisper-models/faster-whisper-medium"
    asr_model_name: str = "Systran/faster-whisper-medium"
    asr_timeout_seconds: int = 600

    @classmethod
    def from_env(cls) -> PipelineSettings:
        return cls(
            work_root=Path(os.getenv("APP_WORK_ROOT", os.getenv("OGNISKOWY_WORK_DIR", "/app/data/work"))),
            database_path=Path(
                os.getenv(
                    "APP_DATABASE_PATH",
                    os.getenv("OGNISKOWY_DATABASE_PATH", "/app/data/ogniskowy-grajek.sqlite3"),
                )
            ),
            pipeline_version=os.getenv("PIPELINE_VERSION", "2.0.0"),
            audio_device=os.getenv("AUDIO_DEVICE", "cpu").lower(),
            max_duration_seconds=int(os.getenv("MAX_VIDEO_DURATION_SECONDS", "600")),
            max_download_bytes=int(os.getenv("MAX_DOWNLOAD_BYTES", str(100 * 1024 * 1024))),
            timeout_seconds=int(os.getenv("JOB_TIMEOUT_SECONDS", "1800")),
            result_ttl_hours=int(os.getenv("RESULT_TTL_HOURS", "24")),
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434"),
            ollama_model=os.getenv("OLLAMA_MODEL", "llama3:8b"),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite"),
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            gemini_api_key_paid=os.getenv("GEMINI_API_KEY_PAID", ""),
            gemini_daily_budget_usd=float(os.getenv("GEMINI_DAILY_BUDGET_USD", "1.0")),
            subtitle_max_bytes=int(os.getenv("SUBTITLE_MAX_BYTES", str(2 * 1024 * 1024))),
            asr_device=os.getenv("ASR_DEVICE", "cpu").lower(),
            asr_model_path=os.getenv("ASR_MODEL_PATH", "/opt/whisper-models/faster-whisper-medium"),
            asr_model_name=os.getenv("ASR_MODEL_NAME", "Systran/faster-whisper-medium"),
            asr_timeout_seconds=int(os.getenv("ASR_TIMEOUT_SECONDS", "600")),
        )


def format_timestamp(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    return f"{total // 60:02d}:{total % 60:02d}"


class SongPipeline:
    def __init__(self, settings: PipelineSettings) -> None:
        self.settings = settings
        self.ledger = BudgetLedger(settings.database_path, settings.gemini_daily_budget_usd)

    def _remaining(self, started: float) -> int:
        elapsed = time.monotonic() - started
        remaining = int(self.settings.timeout_seconds - elapsed)
        if remaining <= 0:
            raise PipelineError("PROCESSING_TIMEOUT", "Analiza przekroczyła limit czasu.", retryable=True)
        return remaining

    def _separate(
        self,
        input_wav: Path,
        workspace: Path,
        *,
        started: float,
        progress: ProgressCallback,
        warnings: list[str],
    ) -> tuple[Path, Path, Path | None, AnalysisMode]:
        requested = self.settings.audio_device
        if requested not in {"cpu", "cuda", "auto"}:
            raise PipelineError("PROCESSING_FAILED", "Nieprawidłowy tryb urządzenia audio.")
        attempts: list[str] = []
        if requested in {"cuda", "auto"}:
            healthy, detail = cuda_preflight()
            if healthy:
                attempts.append("cuda")
            else:
                warnings.append(f"CUDA niedostępna ({detail}); użyto CPU.")
        attempts.append("cpu")
        tried: set[str] = set()
        for device in attempts:
            if device in tried:
                continue
            tried.add(device)
            try:
                remaining = self._remaining(started)
                reserve = 600 if device == "cuda" else 300
                attempt_timeout = max(1, min(600 if device == "cuda" else remaining, remaining - reserve))
                stems = run_demucs(
                    input_wav,
                    workspace / f"separated-{device}",
                    device=device,
                    timeout_seconds=attempt_timeout,
                    progress=lambda fraction, message: progress(
                        "SEPARATING", 25 + int(45 * fraction), message
                    ),
                )
                harmonic, drums = prepare_analysis_tracks(stems, workspace)
                return (
                    harmonic,
                    drums,
                    stems.vocals,
                    (AnalysisMode.DEMUCS_CUDA if device == "cuda" else AnalysisMode.DEMUCS_CPU),
                )
            except Exception as exc:
                warnings.append(f"Demucs {device.upper()} nie zakończył analizy ({exc.__class__.__name__}).")
                if device == "cuda":
                    continue
                break
        progress("PREPROCESSING", 65, "Uruchamianie szybkiego trybu przybliżonego")
        try:
            harmonic, drums = prepare_approximate_tracks(input_wav, workspace)
        except Exception as exc:
            raise PipelineError(
                "PROCESSING_FAILED", "Nie udało się przeanalizować audio.", retryable=True
            ) from exc
        warnings.append("Separacja Demucs była niedostępna; wynik harmoniczny jest przybliżony.")
        return harmonic, drums, None, AnalysisMode.MIX_APPROXIMATE

    def _analyze_chords(
        self,
        harmonic: Path,
        *,
        duration: float,
        bpm: int,
        meter: str,
        timeout_seconds: int,
        warnings: list[str],
    ) -> tuple[list[ChordSegment], ChordDetector]:
        try:
            raw_events = run_chordino(harmonic, timeout_seconds=max(1, min(300, timeout_seconds)))
            bounded_events = [event for event in raw_events if 0 <= float(event[0]) < duration]
            segments = parse_chordino_rows(bounded_events, duration_seconds=duration)
            if not any(segment.chord != "N" for segment in segments):
                raise ValueError("Chordino returned no bounded chord events")
            detector = ChordDetector.CHORDINO
        except Exception:
            warnings.append("Chordino było niedostępne; użyto awaryjnych szablonów chroma.")
            try:
                chroma, times = chroma_fallback(harmonic)
                # YouTube metadata is often rounded while the decoded WAV has a
                # few extra frames. Keep the public timeline within video duration.
                valid = times < duration
                chroma = chroma[:, valid]
                times = times[valid]
                if times.size == 0:
                    raise ValueError("chroma timeline is outside the video duration")
                segments = detect_chords_from_chroma(
                    chroma,
                    times,
                    duration_seconds=duration,
                    min_similarity=0.55,
                )
                detector = ChordDetector.LIBROSA_TEMPLATE
            except Exception as exc:
                raise PipelineError("PROCESSING_FAILED", "Nie udało się wykryć akordów.") from exc
        smoothed = smooth_chord_segments(segments, bpm=bpm, meter=meter)
        if not smoothed:
            raise PipelineError("PROCESSING_FAILED", "Nie wykryto stabilnych akordów w utworze.")
        return smoothed, detector

    def run(self, *, job_id: str, source_url: str, progress: ProgressCallback) -> AnalysisResult:
        started = time.monotonic()
        workspace = self.settings.work_root / job_id
        warnings: list[str] = []
        result: AnalysisResult | None = None
        cleanup_status = CleanupStatus.DONE
        workspace.mkdir(parents=True, exist_ok=True)
        try:
            progress("VALIDATING", 2, "Sprawdzanie linku i metadanych")
            metadata = probe_video(source_url, max_duration_seconds=self.settings.max_duration_seconds)
            transcript: Transcript | None = None
            if metadata.subtitle_track is not None:
                progress("VALIDATING", 4, "Pobieranie oryginalnych napisów")
                try:
                    subtitle_path = download_subtitle_json3(
                        source_url,
                        metadata.subtitle_track,
                        workspace,
                        max_bytes=self.settings.subtitle_max_bytes,
                    )
                    transcript = parse_youtube_json3(subtitle_path, metadata.subtitle_track)
                except LyricsError:
                    warnings.append("Oryginalne napisy były niedostępne; użyto lokalnej transkrypcji.")
            self._remaining(started)
            progress("DOWNLOADING", 8, "Pobieranie audio")
            input_wav = download_wav(
                source_url,
                workspace,
                max_bytes=self.settings.max_download_bytes,
                progress=lambda fraction, message: progress("DOWNLOADING", 8 + int(12 * fraction), message),
            )
            self._remaining(started)
            progress("PREPROCESSING", 22, "Przygotowanie separacji")
            harmonic, drums, vocals, analysis_mode = self._separate(
                input_wav,
                workspace,
                started=started,
                progress=progress,
                warnings=warnings,
            )
            self._remaining(started)
            progress("ANALYZING", 72, "Analiza rytmu")
            rhythm = analyze_rhythm(drums)
            meter_estimate = infer_meter(rhythm.beat_strengths)
            if meter_estimate.warning:
                warnings.append(meter_estimate.warning)
            meter = meter_estimate.meter
            if transcript is None:
                progress("ANALYZING", 74, "Transkrypcja wokalu")
                remaining = self._remaining(started)
                asr_budget = min(self.settings.asr_timeout_seconds, max(0, remaining - 240))
                if asr_budget >= 60:
                    try:
                        transcript = run_local_asr(
                            vocals or input_wav,
                            workspace,
                            model_path=self.settings.asr_model_path,
                            model_name=self.settings.asr_model_name,
                            timeout_seconds=asr_budget,
                            device=self.settings.asr_device,
                        )
                    except LyricsError:
                        warnings.append("Nie udało się uzyskać pewnego tekstu; wynik zawiera same akordy.")
                else:
                    warnings.append("Pominięto transkrypcję, aby nie przekroczyć limitu czasu joba.")
            progress("ANALYZING", 78, "Analiza harmonii")
            # Keep two minutes for chroma fallback, simplification and cleanup.
            chordino_budget = max(1, self._remaining(started) - 120)
            concert_segments, chord_detector = self._analyze_chords(
                harmonic,
                duration=metadata.duration_seconds,
                bpm=rhythm.bpm,
                meter=meter,
                timeout_seconds=chordino_budget,
                warnings=warnings,
            )
            self._remaining(started)
            progress("SIMPLIFYING", 88, "Dobór łatwych chwytów i kapodastra")
            capo = choose_capo(concert_segments)
            played_segments: list[ChordSegment] = []
            difficult_flags: list[bool] = []
            for segment in concert_segments:
                normalized = normalize_chord(segment.chord)
                played = chord_shape_for_capo(normalized, capo)
                difficult = played is None
                played_segments.append(
                    ChordSegment(segment.start_seconds, segment.end_seconds, played or normalized)
                )
                difficult_flags.append(difficult)
            pattern = strumming_pattern(rhythm.bpm, meter)
            sections_raw = group_four_bar_sections(
                played_segments,
                bpm=rhythm.bpm,
                meter=meter,
                end_seconds=metadata.duration_seconds,
            )
            section_models = [
                SongSection(
                    id=section.id,
                    label=section.label,
                    occurrence=section.occurrence,
                    start_seconds=section.start_seconds,
                    end_seconds=section.end_seconds,
                    start_display=format_timestamp(section.start_seconds),
                    end_display=format_timestamp(section.end_seconds),
                    chords=list(section.chords),
                )
                for section in sections_raw
            ]
            timeline: list[TimelineEvent] = []
            for index, (concert, played, difficult) in enumerate(
                zip(concert_segments, played_segments, difficult_flags, strict=True), start=1
            ):
                section_id = next(
                    (
                        section.id
                        for section in sections_raw
                        if section.start_seconds <= concert.start_seconds < section.end_seconds
                    ),
                    None,
                )
                timeline.append(
                    TimelineEvent(
                        event_id=f"e{index:04d}",
                        section_id=section_id,
                        start_seconds=concert.start_seconds,
                        end_seconds=concert.end_seconds,
                        timestamp=format_timestamp(concert.start_seconds),
                        concert_chord=normalize_chord(concert.chord),
                        played_chord=played.chord,
                        difficult=difficult,
                    )
                )
            deterministic = LLMTransformation(
                capo_fret=capo,
                strumming_pattern=pattern,
                chords=[LLMChord(event_id=event.event_id, chord=event.played_chord) for event in timeline],
            )
            allowed = BEGINNER_CHORDS | {event.played_chord for event in timeline}
            outcome = transform(
                bpm=rhythm.bpm,
                meter=meter,
                deterministic=deterministic,
                allowed=allowed,
                ollama_base_url=self.settings.ollama_base_url,
                ollama_model=self.settings.ollama_model,
                gemini_model=self.settings.gemini_model,
                gemini_api_key=self.settings.gemini_api_key,
                gemini_api_key_paid=self.settings.gemini_api_key_paid,
                ledger=self.ledger,
            )
            if outcome.warning:
                warnings.append(outcome.warning)
            # Times, capo, strumming and deterministic shapes remain authoritative.
            progress("FINALIZING", 94, "Układanie śpiewnika")
            songbook = None
            if transcript is not None:
                try:
                    songbook = build_songbook(transcript, timeline)
                except Exception:
                    warnings.append("Nie udało się ułożyć tekstu; wynik zawiera same akordy.")
            progress("FINALIZING", 96, "Budowanie wyniku")
            generated_at = datetime.now(UTC)
            result = AnalysisResult(
                pipeline_version=self.settings.pipeline_version,
                job_id=job_id,
                source=SourceInfo(
                    video_id=metadata.video_id,
                    title=metadata.title,
                    duration_seconds=metadata.duration_seconds,
                    webpage_url=metadata.webpage_url,
                ),
                arrangement=Arrangement(
                    bpm=rhythm.bpm,
                    meter=meter,
                    meter_confidence=meter_estimate.confidence,
                    capo_fret=capo,
                    strumming_pattern=pattern,
                    sections=section_models,
                    timeline=timeline,
                    songbook=songbook,
                ),
                processing=ProcessingInfo(
                    analysis_mode=analysis_mode,
                    chord_detector=chord_detector,
                    simplification_mode=outcome.mode,
                    llm_model=outcome.model,
                    approximate=analysis_mode == AnalysisMode.MIX_APPROXIMATE,
                    warnings=warnings,
                    estimated_cost_usd=outcome.estimated_cost_usd,
                    lyrics_source=transcript.source if transcript else LyricsSource.UNAVAILABLE,
                    transcription_model=transcript.model if transcript else None,
                ),
                generated_at=generated_at,
                expires_at=generated_at + timedelta(hours=self.settings.result_ttl_hours),
            )
        except IngestError as exc:
            raise PipelineError(exc.code, exc.public_message, retryable=exc.retryable) from exc
        except PipelineError:
            raise
        except TimeoutError as exc:
            raise PipelineError(
                "PROCESSING_TIMEOUT", "Analiza przekroczyła limit czasu.", retryable=True
            ) from exc
        except Exception as exc:
            raise PipelineError("PROCESSING_FAILED", "Analiza nie powiodła się.", retryable=True) from exc
        finally:
            # A cancellation or a temporary database failure may make the status
            # callback raise. Cleanup must remain unconditional in every terminal
            # path, so status reporting is intentionally best-effort here.
            with suppress(Exception):
                progress("CLEANING_UP", 99, "Usuwanie plików tymczasowych")
            try:
                shutil.rmtree(workspace, ignore_errors=False)
            except FileNotFoundError:
                pass
            except OSError:
                cleanup_status = CleanupStatus.DEFERRED
        if result is None:  # pragma: no cover - all failure paths raise
            raise PipelineError("PROCESSING_FAILED", "Analiza nie powiodła się.")
        result.processing.cleanup_status = cleanup_status
        if cleanup_status == CleanupStatus.DEFERRED:
            result.processing.warnings.append("Czyszczenie plików zostanie ponowione przez sweeper.")
        return result


def purge_orphan_workspaces(root: Path, *, older_than_seconds: int = 3600) -> int:
    root.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - older_than_seconds
    removed = 0
    for path in root.iterdir():
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path)
                removed += 1
        except OSError:
            continue
    return removed
