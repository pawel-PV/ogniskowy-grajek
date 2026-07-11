from __future__ import annotations

import multiprocessing as mp
import os
import time
from pathlib import Path

from ogniskowy_grajek import worker as worker_module
from ogniskowy_grajek.config import AppConfig
from ogniskowy_grajek.jobs import JobStatus, JobStore
from ogniskowy_grajek.pipeline import PipelineSettings

VIDEO = "dQw4w9WgXcQ"
IDENTITY = "a" * 64


def _claimed_job(tmp_path: Path, *, timeout: int = 5):
    config = AppConfig(
        database_path=tmp_path / "jobs.sqlite3",
        work_dir=tmp_path / "work",
        pipeline_version="test",
        client_hmac_secret="s" * 32,
        lease_seconds=2,
        job_timeout_seconds=timeout,
    )
    store = JobStore.from_config(config)
    queued = store.enqueue(f"https://youtu.be/{VIDEO}", client_hash=IDENTITY)
    claimed = store.claim_next("test-worker", lease_seconds=config.lease_seconds)
    assert claimed is not None and claimed.id == queued.id
    settings = PipelineSettings(
        work_root=config.work_dir,
        database_path=config.database_path,
        timeout_seconds=timeout,
    )
    return config, store, claimed, settings


def test_supervisor_persists_done_message_without_queue_empty_race(monkeypatch, tmp_path: Path) -> None:
    config, store, job, settings = _claimed_job(tmp_path)

    def quick_child(_settings, _job_id, _source_url, messages):
        messages.put(("done", {"schema_version": "1.0", "ok": True}))

    monkeypatch.setattr(worker_module, "_pipeline_child", quick_child)
    worker_module._run_claimed_job(
        store,
        config,
        settings,
        worker_id="test-worker",
        job_id=job.id,
        source_url=job.source_url,
        job_started_at=job.started_at,
        stop_requested=mp.Event(),
    )

    completed = store.get_job(job.id)
    assert completed is not None
    assert completed.status is JobStatus.DONE
    assert completed.progress == 100
    assert completed.result == {"schema_version": "1.0", "ok": True}


def test_supervisor_kills_hung_child_at_cumulative_deadline_and_cleans(monkeypatch, tmp_path: Path) -> None:
    config, store, job, settings = _claimed_job(tmp_path, timeout=1)

    def hung_child(child_settings, child_job_id, _source_url, _messages):
        (child_settings.work_root / child_job_id).mkdir(parents=True)
        (child_settings.work_root / child_job_id / "audio.wav").write_bytes(b"private")
        time.sleep(60)

    monkeypatch.setattr(worker_module, "_pipeline_child", hung_child)
    worker_module._run_claimed_job(
        store,
        config,
        settings,
        worker_id="test-worker",
        job_id=job.id,
        source_url=job.source_url,
        job_started_at=job.started_at,
        stop_requested=mp.Event(),
    )

    failed = store.get_job(job.id)
    assert failed is not None
    assert failed.status is JobStatus.FAILED
    assert failed.error_code == "PROCESSING_TIMEOUT"
    assert not (settings.work_root / job.id).exists()


def test_supervisor_cleans_workspace_after_abrupt_native_exit(monkeypatch, tmp_path: Path) -> None:
    config, store, job, settings = _claimed_job(tmp_path)

    def crashing_child(child_settings, child_job_id, _source_url, _messages):
        workspace = child_settings.work_root / child_job_id
        workspace.mkdir(parents=True)
        (workspace / "audio.wav").write_bytes(b"private")
        os._exit(7)

    monkeypatch.setattr(worker_module, "_pipeline_child", crashing_child)
    worker_module._run_claimed_job(
        store,
        config,
        settings,
        worker_id="test-worker",
        job_id=job.id,
        source_url=job.source_url,
        job_started_at=job.started_at,
        stop_requested=mp.Event(),
    )

    failed = store.get_job(job.id)
    assert failed is not None
    assert failed.status is JobStatus.FAILED
    assert failed.error_code == "PROCESSING_FAILED"
    assert not (settings.work_root / job.id).exists()


def test_idle_startup_sweep_preserves_running_but_removes_terminal_workspace(tmp_path: Path) -> None:
    _config, store, job, settings = _claimed_job(tmp_path)
    workspace = settings.work_root / job.id
    workspace.mkdir(parents=True)
    (workspace / "audio.wav").write_bytes(b"private")

    assert worker_module._purge_workspaces_when_idle(store, settings.work_root) == 0
    assert workspace.exists()

    store.fail(job.id, "WORKER_LOST", retryable=True)
    assert worker_module._purge_workspaces_when_idle(store, settings.work_root) == 1
    assert not workspace.exists()
