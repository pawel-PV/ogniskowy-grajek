from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import queue
import shutil
import signal
import socket
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from .config import AppConfig
from .jobs import JobStatus, JobStore
from .pipeline import PipelineError, PipelineSettings, SongPipeline, purge_orphan_workspaces


def doctor() -> int:
    from .audio import cuda_preflight

    settings = PipelineSettings.from_env()
    store = JobStore(settings.database_path, pipeline_version=settings.pipeline_version)
    pragma = store.pragma_state()
    database_ok = pragma["journal_mode"] == "wal"
    ffmpeg_ok = os.system("ffmpeg -version >/dev/null 2>&1") == 0
    sonic_ok = os.system("sonic-annotator -l >/dev/null 2>&1") == 0
    print(f"database_journal={pragma['journal_mode']}")
    print(f"ffmpeg={ffmpeg_ok}")
    print(f"sonic_annotator={sonic_ok}")
    cuda_ok, cuda_detail = cuda_preflight()
    print(f"cuda={cuda_ok} detail={cuda_detail}")
    return 0 if database_ok and ffmpeg_ok and sonic_ok else 1


def audio_smoke() -> int:
    """Exercise the real librosa/Numba path on a synthetic 120 BPM click track."""

    try:
        import librosa
        import numpy as np
        import soundfile as sf

        from .audio import analyze_rhythm

        sample_rate = 22_050
        signal_data = librosa.clicks(
            times=np.arange(0.0, 8.0, 0.5),
            sr=sample_rate,
            length=sample_rate * 8,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clicks.wav"
            sf.write(path, signal_data, sample_rate)
            bpm = analyze_rhythm(path).bpm
        print(f"synthetic_bpm={bpm}")
        return 0 if abs(bpm - 120) <= 3 else 1
    except Exception as exc:
        print(f"audio_smoke=False detail={exc.__class__.__name__}")
        return 1


def asr_smoke() -> int:
    """Load the pinned local Whisper model without running it in healthchecks."""

    try:
        from faster_whisper import WhisperModel

        settings = PipelineSettings.from_env()
        model_path = Path(settings.asr_model_path)
        if not model_path.is_dir():
            raise FileNotFoundError(model_path)
        WhisperModel(
            str(model_path),
            device=settings.asr_device,
            compute_type="float16" if settings.asr_device == "cuda" else "int8",
            cpu_threads=4,
            num_workers=1,
            local_files_only=True,
        )
        print(f"asr_model=True device={settings.asr_device}")
        return 0
    except Exception as exc:
        print(f"asr_model=False detail={exc.__class__.__name__}")
        return 1


def _pipeline_child(
    settings: PipelineSettings,
    job_id: str,
    source_url: str,
    messages: Any,
) -> None:
    """Run one job in its own process group so the parent can enforce a deadline."""

    with suppress(OSError):
        os.setsid()

    def progress(stage: str, percent: int, message: str) -> None:
        messages.put(("progress", stage, percent, message))

    try:
        result = SongPipeline(settings).run(
            job_id=job_id,
            source_url=source_url,
            progress=progress,
        )
        messages.put(("done", result.model_dump(mode="json")))
    except PipelineError as exc:
        messages.put(("error", exc.code, exc.public_message, exc.retryable))
    except BaseException:
        messages.put(("error", "PROCESSING_FAILED", "", True))


def _stop_process_group(process: mp.Process, *, grace_seconds: float = 10.0) -> None:
    if process.pid is None or not process.is_alive():
        process.join(timeout=1)
        return
    signalled = False
    try:
        os.killpg(process.pid, signal.SIGTERM)
        signalled = True
    except (ProcessLookupError, PermissionError):
        pass
    if not signalled:
        with suppress(Exception):
            process.terminate()
    process.join(timeout=grace_seconds)
    if process.is_alive():
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            with suppress(Exception):
                process.kill()
        process.join(timeout=5)


def _cleanup_workspace(work_root: Path, job_id: str) -> None:
    shutil.rmtree(work_root / job_id, ignore_errors=True)


def _purge_workspaces_when_idle(store: JobStore, work_root: Path) -> int:
    if next(store.iter_jobs(status=JobStatus.RUNNING), None) is not None:
        return 0
    return purge_orphan_workspaces(work_root, older_than_seconds=0)


def _run_claimed_job(
    store: JobStore,
    app_config: AppConfig,
    pipeline_settings: PipelineSettings,
    *,
    worker_id: str,
    job_id: str,
    source_url: str,
    job_started_at: float | None,
    stop_requested: Any,
) -> None:
    elapsed_before_attempt = max(0.0, time.time() - float(job_started_at or time.time()))
    remaining_seconds = app_config.job_timeout_seconds - elapsed_before_attempt
    if remaining_seconds <= 0:
        _cleanup_workspace(pipeline_settings.work_root, job_id)
        with suppress(Exception):
            store.fail(job_id, "PROCESSING_TIMEOUT", retryable=True)
        return
    context = mp.get_context("fork")
    messages = context.Queue()
    process = context.Process(
        target=_pipeline_child,
        args=(pipeline_settings, job_id, source_url, messages),
        name=f"analysis-{job_id}",
    )
    process.start()
    started = time.monotonic()
    deadline = started + remaining_seconds
    next_heartbeat = started + max(5, app_config.lease_seconds // 3)
    next_status_check = started
    terminal_message: tuple[Any, ...] | None = None
    child_exited_at: float | None = None

    try:
        while terminal_message is None:
            if stop_requested.is_set():
                _stop_process_group(process)
                _cleanup_workspace(pipeline_settings.work_root, job_id)
                return
            now = time.monotonic()
            if now >= next_status_check:
                current = store.get_job(job_id)
                if current is not None and current.status is JobStatus.CANCELLED:
                    _stop_process_group(process)
                    _cleanup_workspace(pipeline_settings.work_root, job_id)
                    return
                next_status_check = now + 1
            if now >= deadline:
                _stop_process_group(process)
                _cleanup_workspace(pipeline_settings.work_root, job_id)
                with suppress(Exception):
                    store.fail(job_id, "PROCESSING_TIMEOUT", retryable=True)
                return
            if now >= next_heartbeat:
                if not store.heartbeat(
                    job_id,
                    worker_id,
                    lease_seconds=app_config.lease_seconds,
                ):
                    current = store.get_job(job_id)
                    _stop_process_group(process)
                    _cleanup_workspace(pipeline_settings.work_root, job_id)
                    if current is not None and current.status is JobStatus.CANCELLED:
                        return
                    return
                next_heartbeat = now + max(5, app_config.lease_seconds // 3)

            try:
                item = messages.get(timeout=0.5)
            except queue.Empty:
                if not process.is_alive():
                    child_exited_at = child_exited_at or time.monotonic()
                    if time.monotonic() - child_exited_at >= 1:
                        break
                continue
            if not item:
                continue
            if item[0] == "progress":
                try:
                    store.update_progress(job_id, item[1], item[2], item[3])
                except Exception:
                    current = store.get_job(job_id)
                    if current is not None and current.status is JobStatus.CANCELLED:
                        _stop_process_group(process)
                        _cleanup_workspace(pipeline_settings.work_root, job_id)
                        return
                    raise
            elif item[0] in {"done", "error"}:
                terminal_message = item

        process.join(timeout=5)
        if process.is_alive():
            _stop_process_group(process)
        # SongPipeline normally cleans in its finally block. Repeat the removal
        # idempotently for native crashes, OOM kills and abrupt os._exit calls.
        _cleanup_workspace(pipeline_settings.work_root, job_id)
        if terminal_message and terminal_message[0] == "done":
            store.complete(job_id, terminal_message[1])
        elif terminal_message and terminal_message[0] == "error":
            store.fail(
                job_id,
                str(terminal_message[1]),
                str(terminal_message[2]),
                retryable=bool(terminal_message[3]),
            )
        else:
            store.fail(job_id, "PROCESSING_FAILED", retryable=True)
    finally:
        if process.is_alive():
            _stop_process_group(process)
        messages.close()
        messages.join_thread()


def run_forever() -> None:
    app_config = AppConfig.from_env(require_secret=True)
    pipeline_settings = PipelineSettings.from_env()
    store = JobStore.from_config(app_config)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    stop_requested = mp.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    last_maintenance = 0.0
    last_idle_cleanup = 0.0
    while not stop_requested.is_set():
        now = time.monotonic()
        if now - last_maintenance >= 1800:
            store.maintenance()
            purge_orphan_workspaces(pipeline_settings.work_root)
            last_maintenance = now
        job = store.claim_next(worker_id, lease_seconds=app_config.lease_seconds)
        if job is None:
            if now - last_idle_cleanup >= 10:
                _purge_workspaces_when_idle(store, pipeline_settings.work_root)
                last_idle_cleanup = now
            stop_requested.wait(2)
            continue
        # A retry after host/container loss always starts from a clean attempt.
        _cleanup_workspace(pipeline_settings.work_root, job.id)
        try:
            _run_claimed_job(
                store,
                app_config,
                pipeline_settings,
                worker_id=worker_id,
                job_id=job.id,
                source_url=job.source_url,
                job_started_at=job.started_at,
                stop_requested=stop_requested,
            )
        except Exception:
            _cleanup_workspace(pipeline_settings.work_root, job.id)
            with suppress(Exception):
                store.fail(job.id, "PROCESSING_FAILED", retryable=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Worker Ogniskowego Grajka")
    parser.add_argument("--doctor", action="store_true")
    parser.add_argument("--audio-smoke", action="store_true")
    parser.add_argument("--asr-smoke", action="store_true")
    args = parser.parse_args()
    if args.doctor:
        raise SystemExit(doctor())
    if args.audio_smoke:
        raise SystemExit(audio_smoke())
    if args.asr_smoke:
        raise SystemExit(asr_smoke())
    run_forever()


if __name__ == "__main__":
    main()
