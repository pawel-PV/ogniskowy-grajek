from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ogniskowy_grajek.config import client_hash
from ogniskowy_grajek.jobs import (
    ActiveJobLimit,
    GlobalDailyLimit,
    HourlyRateLimit,
    InvalidJobRequest,
    JobStage,
    JobStatus,
    JobStore,
    QueueFull,
    validate_youtube_url,
)

VIDEO_1 = "dQw4w9WgXcQ"
VIDEO_2 = "9bZkp7q19f0"


class Clock:
    def __init__(self, value: float = 1_752_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def identity(number: int) -> str:
    return f"{number:064x}"


def make_store(path: Path, clock: Clock | None = None, **kwargs: object) -> JobStore:
    return JobStore(path, clock=clock or Clock(), **kwargs)


@pytest.mark.parametrize(
    ("url", "video_id"),
    [
        (f"https://youtu.be/{VIDEO_1}", VIDEO_1),
        (f"https://www.youtube.com/watch?v={VIDEO_1}", VIDEO_1),
        (f"https://music.youtube.com/watch?v={VIDEO_1}", VIDEO_1),
        (f"https://youtube.com/shorts/{VIDEO_1}", VIDEO_1),
    ],
)
def test_validate_url_returns_canonical_single_video(url: str, video_id: str) -> None:
    assert validate_youtube_url(url) == (
        f"https://www.youtube.com/watch?v={video_id}",
        video_id,
    )


@pytest.mark.parametrize(
    "url",
    [
        f"http://youtu.be/{VIDEO_1}",
        f"https://example.com/watch?v={VIDEO_1}",
        f"https://youtube.com/watch?v={VIDEO_1}&list=PL123",
        "https://youtube.com/playlist?list=PL123",
        "https://127.0.0.1/watch?v=dQw4w9WgXcQ",
        "https://youtube.com/watch?v=short",
    ],
)
def test_validate_url_rejects_non_video_playlist_and_ssrf(url: str) -> None:
    with pytest.raises(InvalidJobRequest):
        validate_youtube_url(url)


def test_database_uses_schema_v1_wal_and_busy_timeout(tmp_path: Path) -> None:
    store = make_store(tmp_path / "jobs.sqlite3")

    assert store.pragma_state() == {
        "user_version": 1,
        "journal_mode": "wal",
        "busy_timeout": 5_000,
    }


def test_raw_ip_is_never_written_to_database(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    raw_ip = "2001:db8::1234"
    hashed = client_hash(raw_ip, "s" * 32)
    store = make_store(database)
    store.enqueue(f"https://youtu.be/{VIDEO_1}", client_hash=hashed)

    connection = sqlite3.connect(database)
    row = connection.execute("SELECT client_hash, source_url FROM jobs").fetchone()
    connection.close()
    assert row[0] == hashed
    assert raw_ip not in str(row)


def test_only_one_active_job_per_client(tmp_path: Path) -> None:
    store = make_store(tmp_path / "jobs.sqlite3")
    client = identity(1)
    store.enqueue(f"https://youtu.be/{VIDEO_1}", client_hash=client)

    with pytest.raises(ActiveJobLimit):
        store.enqueue(f"https://youtu.be/{VIDEO_2}", client_hash=client)


def test_queue_capacity_is_checked_atomically(tmp_path: Path) -> None:
    store = make_store(tmp_path / "jobs.sqlite3", queue_max=1)
    store.enqueue(f"https://youtu.be/{VIDEO_1}", client_hash=identity(1))

    with pytest.raises(QueueFull):
        store.enqueue(f"https://youtu.be/{VIDEO_2}", client_hash=identity(2))


def test_concurrent_workers_claim_exactly_one_job(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    store = make_store(database)
    queued = store.enqueue(f"https://youtu.be/{VIDEO_1}", client_hash=identity(1))

    def claim(worker_number: int) -> str | None:
        claimed = store.claim_next(f"worker-{worker_number}")
        return claimed.id if claimed else None

    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed_ids = list(pool.map(claim, range(8)))

    assert [value for value in claimed_ids if value is not None] == [queued.id]
    assert store.get_job(queued.id).status is JobStatus.RUNNING  # type: ignore[union-attr]


def test_progress_and_stage_are_monotonic(tmp_path: Path) -> None:
    store = make_store(tmp_path / "jobs.sqlite3")
    queued = store.enqueue(f"https://youtu.be/{VIDEO_1}", client_hash=identity(1))
    store.claim_next("worker")
    store.update_progress(queued.id, JobStage.DOWNLOADING, 35, "Pobieranie")

    updated = store.update_progress(queued.id, JobStage.VALIDATING, 5, "stary etap")

    assert updated.progress == 35
    assert updated.stage is JobStage.DOWNLOADING
    assert updated.status_message == "stary etap"


def test_lease_heartbeat_and_recovery_keep_progress(tmp_path: Path) -> None:
    clock = Clock()
    store = make_store(
        tmp_path / "jobs.sqlite3",
        clock,
        lease_seconds=10,
        job_timeout_seconds=100,
        max_attempts=2,
    )
    queued = store.enqueue(f"https://youtu.be/{VIDEO_1}", client_hash=identity(1))
    store.claim_next("worker-1", lease_seconds=10)
    store.update_progress(queued.id, JobStage.SEPARATING, 50)
    clock.advance(5)
    assert store.heartbeat(queued.id, "worker-1", lease_seconds=10)
    clock.advance(6)
    assert store.recover_expired() == 0
    clock.advance(5)

    assert store.recover_expired() == 1
    recovered = store.get_job(queued.id)
    assert recovered is not None
    assert recovered.status is JobStatus.PENDING
    assert recovered.progress == 50
    assert recovered.stage is JobStage.SEPARATING

    store.claim_next("worker-2", lease_seconds=10)
    clock.advance(11)
    assert store.recover_expired() == 1
    exhausted = store.get_job(queued.id)
    assert exhausted is not None
    assert exhausted.status is JobStatus.FAILED
    assert exhausted.error_code == "WORKER_LOST"


def test_job_timeout_is_a_safe_public_error(tmp_path: Path) -> None:
    clock = Clock()
    store = make_store(
        tmp_path / "jobs.sqlite3",
        clock,
        lease_seconds=5,
        job_timeout_seconds=10,
    )
    queued = store.enqueue(f"https://youtu.be/{VIDEO_1}", client_hash=identity(1))
    store.claim_next("worker", lease_seconds=5)
    clock.advance(11)

    store.recover_expired()
    failed = store.get_job(queued.id)
    assert failed is not None
    assert failed.error_code == "PROCESSING_TIMEOUT"
    assert failed.error_message == "Analiza przekroczyła limit czasu."
    assert failed.retryable


def test_unknown_worker_error_never_exposes_message(tmp_path: Path) -> None:
    store = make_store(tmp_path / "jobs.sqlite3")
    queued = store.enqueue(f"https://youtu.be/{VIDEO_1}", client_hash=identity(1))
    store.claim_next("worker")

    failed = store.fail(
        queued.id,
        "SOME_PROVIDER_ERROR",
        "/data/work/private: token=super-secret traceback...",
    )

    assert failed.error_code == "PROCESSING_FAILED"
    assert failed.error_message == "Analiza nie powiodła się. Spróbuj ponownie."
    assert "secret" not in repr(failed.to_dict())


@pytest.mark.parametrize(
    ("upstream_code", "public_code", "retryable"),
    [
        ("DURATION_LIMIT", "VIDEO_TOO_LONG", False),
        ("SIZE_LIMIT", "VIDEO_TOO_LARGE", False),
        ("AGE_RESTRICTED", "UNSUPPORTED_VIDEO", False),
        ("PROBE_FAILED", "DOWNLOAD_FAILED", True),
    ],
)
def test_ingest_errors_are_mapped_to_safe_public_contract(
    tmp_path: Path,
    upstream_code: str,
    public_code: str,
    retryable: bool,
) -> None:
    store = make_store(tmp_path / f"{upstream_code}.sqlite3")
    queued = store.enqueue(f"https://youtu.be/{VIDEO_1}", client_hash=identity(1))
    store.claim_next("worker")

    failed = store.fail(queued.id, upstream_code, "private upstream response")

    assert failed.error_code == public_code
    assert failed.retryable is retryable
    assert "upstream" not in (failed.error_message or "")


def test_cached_result_skips_queue_and_global_uncached_limit(tmp_path: Path) -> None:
    clock = Clock()
    store = make_store(
        tmp_path / "jobs.sqlite3",
        clock,
        global_daily_uncached_limit=1,
        result_ttl_seconds=60,
    )
    first = store.enqueue(f"https://youtu.be/{VIDEO_1}", client_hash=identity(1))
    store.claim_next("worker")
    completed = store.complete(first.id, {"schema_version": "1.0", "answer": 42})
    cached = store.enqueue(f"https://youtu.be/{VIDEO_1}", client_hash=identity(2))

    assert cached.status is JobStatus.DONE
    assert cached.cache_hit
    assert cached.result is not None
    assert cached.result["answer"] == completed.result["answer"]  # type: ignore[index]
    assert cached.result["job_id"] == cached.id
    assert cached.result["expires_at"].endswith("Z")
    assert cached.expires_at == completed.expires_at

    with pytest.raises(GlobalDailyLimit):
        store.enqueue(f"https://youtu.be/{VIDEO_2}", client_hash=identity(3))

    clock.advance(61)
    assert store.get_job(cached.id).result is None  # type: ignore[union-attr]
    assert store.purge_expired_results() == 1


def test_hourly_rate_limit_counts_terminal_and_cached_jobs(tmp_path: Path) -> None:
    store = make_store(tmp_path / "jobs.sqlite3", client_hourly_limit=3)
    client = identity(1)
    urls = [
        f"https://youtube.com/watch?v={VIDEO_1}",
        f"https://youtube.com/watch?v={VIDEO_2}",
        "https://youtube.com/watch?v=J---aiyznGQ",
    ]
    for url in urls:
        queued = store.enqueue(url, client_hash=client)
        store.claim_next("worker")
        store.fail(queued.id, "PROCESSING_FAILED")

    with pytest.raises(HourlyRateLimit):
        store.enqueue("https://youtube.com/watch?v=kJQP7kiw5Fk", client_hash=client)


def test_complete_sets_contract_terminal_fields(tmp_path: Path) -> None:
    store = make_store(tmp_path / "jobs.sqlite3", result_ttl_seconds=60)
    queued = store.enqueue(f"https://youtu.be/{VIDEO_1}", client_hash=identity(1))
    store.claim_next("worker")
    completed = store.complete(queued.id, {"schema_version": "1.0"})

    assert completed.status is JobStatus.DONE
    assert completed.stage is JobStage.COMPLETE
    assert completed.progress == 100
    assert completed.lease_owner is None
    assert completed.finished_at is not None
    assert completed.expires_at == pytest.approx(completed.finished_at + 60)
