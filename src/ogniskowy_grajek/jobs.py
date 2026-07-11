"""Durable SQLite job queue used by Streamlit and the analysis worker.

Every state transition is a short ``BEGIN IMMEDIATE`` transaction.  That makes
enqueue/claim atomic across processes while WAL keeps status reads responsive.
The database stores HMAC client identifiers only; raw IP addresses never enter it.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .config import AppConfig

SCHEMA_VERSION = 1


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobStage(StrEnum):
    QUEUED = "QUEUED"
    VALIDATING = "VALIDATING"
    DOWNLOADING = "DOWNLOADING"
    PREPROCESSING = "PREPROCESSING"
    SEPARATING = "SEPARATING"
    ANALYZING = "ANALYZING"
    SIMPLIFYING = "SIMPLIFYING"
    FINALIZING = "FINALIZING"
    CLEANING_UP = "CLEANING_UP"
    COMPLETE = "COMPLETE"


_STAGE_ORDER = {stage: index for index, stage in enumerate(JobStage)}
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}


SAFE_ERROR_MESSAGES: Mapping[str, tuple[str, bool]] = {
    "INVALID_REQUEST": ("Nie można przetworzyć tego zgłoszenia.", False),
    "UNSUPPORTED_VIDEO": ("Ten materiał nie jest obsługiwany.", False),
    "VIDEO_TOO_LONG": ("Materiał przekracza limit 10 minut.", False),
    "VIDEO_TOO_LARGE": ("Plik źródłowy przekracza limit 100 MB.", False),
    "DOWNLOAD_FAILED": ("Nie udało się pobrać dźwięku z YouTube.", True),
    "PROCESSING_TIMEOUT": ("Analiza przekroczyła limit czasu.", True),
    "WORKER_LOST": ("Analiza została przerwana. Spróbuj ponownie.", True),
    "PROCESSING_FAILED": ("Analiza nie powiodła się. Spróbuj ponownie.", True),
    "CANCELLED": ("Analiza została anulowana.", True),
}

_ERROR_ALIASES: Mapping[str, str] = {
    "INVALID_URL": "INVALID_REQUEST",
    "UNSUPPORTED_SOURCE": "UNSUPPORTED_VIDEO",
    "PLAYLIST_NOT_ALLOWED": "UNSUPPORTED_VIDEO",
    "LIVE_NOT_ALLOWED": "UNSUPPORTED_VIDEO",
    "VIDEO_UNAVAILABLE": "UNSUPPORTED_VIDEO",
    "AGE_RESTRICTED": "UNSUPPORTED_VIDEO",
    "DURATION_UNKNOWN": "DOWNLOAD_FAILED",
    "DURATION_LIMIT": "VIDEO_TOO_LONG",
    "PROBE_FAILED": "DOWNLOAD_FAILED",
    "SIZE_LIMIT": "VIDEO_TOO_LARGE",
    "TRANSCODE_FAILED": "PROCESSING_FAILED",
}


class JobStoreError(RuntimeError):
    """Base exception safe to render in the public UI."""

    code = "INVALID_REQUEST"
    public_message = "Nie można przetworzyć tego zgłoszenia."
    retryable = False

    def __init__(self, message: str | None = None, *, retry_after: int | None = None):
        super().__init__(message or self.public_message)
        self.retry_after = retry_after

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code": self.code,
            "message": self.public_message,
            "retryable": self.retryable,
        }
        if self.retry_after is not None:
            value["retry_after_seconds"] = max(1, int(self.retry_after))
        return value


class InvalidJobRequest(JobStoreError):
    code = "INVALID_REQUEST"


class ActiveJobLimit(JobStoreError):
    code = "CLIENT_ACTIVE_JOB"
    public_message = "Masz już aktywną analizę. Poczekaj na jej zakończenie."
    retryable = True


class HourlyRateLimit(JobStoreError):
    code = "CLIENT_HOURLY_LIMIT"
    public_message = "Wykorzystano limit 3 analiz na godzinę. Spróbuj później."
    retryable = True


class DailyRateLimit(JobStoreError):
    code = "CLIENT_DAILY_LIMIT"
    public_message = "Wykorzystano dzienny limit 10 analiz."
    retryable = True


class GlobalDailyLimit(JobStoreError):
    code = "DAILY_CAPACITY_REACHED"
    public_message = "Dzisiejszy limit analiz został wykorzystany. Wróć jutro."
    retryable = True


class QueueFull(JobStoreError):
    code = "QUEUE_FULL"
    public_message = "Kolejka jest pełna. Spróbuj ponownie za kilka minut."
    retryable = True


class JobNotFound(JobStoreError):
    code = "JOB_NOT_FOUND"
    public_message = "Nie znaleziono tej analizy lub nie masz do niej dostępu."


class InvalidTransition(JobStoreError):
    code = "INVALID_JOB_STATE"
    public_message = "Analiza nie jest już w stanie pozwalającym na tę operację."


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: str
    source_url: str
    video_id: str
    cache_key: str
    client_hash: str
    pipeline_version: str
    status: JobStatus
    stage: JobStage
    progress: int
    status_message: str | None
    cache_hit: bool
    attempt_count: int
    created_at: float
    updated_at: float
    started_at: float | None
    finished_at: float | None
    expires_at: float | None
    lease_owner: str | None
    lease_expires_at: float | None
    heartbeat_at: float | None
    result: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    retryable: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> JobRecord:
        result: dict[str, Any] | None = None
        if row["result_json"]:
            try:
                decoded = json.loads(row["result_json"])
                if isinstance(decoded, dict):
                    result = decoded
            except (TypeError, ValueError):
                result = None
        return cls(
            id=row["id"],
            source_url=row["source_url"],
            video_id=row["video_id"],
            cache_key=row["cache_key"],
            client_hash=row["client_hash"],
            pipeline_version=row["pipeline_version"],
            status=JobStatus(row["status"]),
            stage=JobStage(row["stage"]),
            progress=int(row["progress"]),
            status_message=row["status_message"],
            cache_hit=bool(row["cache_hit"]),
            attempt_count=int(row["attempt_count"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            started_at=float(row["started_at"]) if row["started_at"] is not None else None,
            finished_at=float(row["finished_at"]) if row["finished_at"] is not None else None,
            expires_at=float(row["expires_at"]) if row["expires_at"] is not None else None,
            lease_owner=row["lease_owner"],
            lease_expires_at=(
                float(row["lease_expires_at"]) if row["lease_expires_at"] is not None else None
            ),
            heartbeat_at=(float(row["heartbeat_at"]) if row["heartbeat_at"] is not None else None),
            result=result,
            error_code=row["error_code"],
            error_message=row["error_message"],
            retryable=bool(row["retryable"]),
        )

    def to_dict(self, *, include_private: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.id,
            "video_id": self.video_id,
            "pipeline_version": self.pipeline_version,
            "status": self.status.value,
            "stage": self.stage.value,
            "progress": self.progress,
            "message": self.status_message,
            "cache_hit": self.cache_hit,
            "attempt_count": self.attempt_count,
            "created_at": _iso_timestamp(self.created_at),
            "updated_at": _iso_timestamp(self.updated_at),
            "started_at": _iso_timestamp(self.started_at),
            "finished_at": _iso_timestamp(self.finished_at),
            "expires_at": _iso_timestamp(self.expires_at),
            "result": self.result,
            "error": (
                {
                    "code": self.error_code,
                    "message": self.error_message,
                    "retryable": self.retryable,
                }
                if self.error_code
                else None
            ),
        }
        if include_private:
            value.update(
                {
                    "source_url": self.source_url,
                    "cache_key": self.cache_key,
                    "client_hash": self.client_hash,
                    "lease_owner": self.lease_owner,
                    "lease_expires_at": _iso_timestamp(self.lease_expires_at),
                    "heartbeat_at": _iso_timestamp(self.heartbeat_at),
                }
            )
        return value


def _iso_timestamp(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")


def validate_youtube_url(source_url: str) -> tuple[str, str]:
    """Validate a single public YouTube HTTPS URL and return canonical URL/id."""

    if not isinstance(source_url, str) or not source_url.strip() or len(source_url) > 2_048:
        raise InvalidJobRequest()
    try:
        parsed = urlsplit(source_url.strip())
    except ValueError as exc:
        raise InvalidJobRequest() from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or host not in _ALLOWED_HOSTS:
        raise InvalidJobRequest()
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise InvalidJobRequest()

    query = parse_qs(parsed.query, keep_blank_values=True)
    if "list" in query or "index" in query:
        raise InvalidJobRequest()

    video_id: str | None = None
    path_parts = [part for part in parsed.path.split("/") if part]
    if host in {"youtu.be", "www.youtu.be"} and path_parts:
        video_id = path_parts[0]
    elif parsed.path.rstrip("/") == "/watch":
        values = query.get("v", [])
        if len(values) == 1:
            video_id = values[0]
    elif len(path_parts) == 2 and path_parts[0] in {"shorts", "embed"}:
        video_id = path_parts[1]

    if not video_id or not _YOUTUBE_ID_RE.fullmatch(video_id):
        raise InvalidJobRequest()
    return f"https://www.youtube.com/watch?v={video_id}", video_id


class JobStore:
    """A process-safe queue with cache, quotas, leases and recovery."""

    def __init__(
        self,
        database: str | Path | AppConfig,
        *,
        pipeline_version: str | None = None,
        queue_max: int | None = None,
        client_hourly_limit: int | None = None,
        client_daily_limit: int | None = None,
        global_daily_uncached_limit: int | None = None,
        result_ttl_seconds: int | None = None,
        lease_seconds: int | None = None,
        job_timeout_seconds: int | None = None,
        max_attempts: int | None = None,
        busy_timeout_ms: int | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        config = database if isinstance(database, AppConfig) else None
        self.database_path = str(config.database_path if config else database)
        self.pipeline_version = pipeline_version or (config.pipeline_version if config else "1")
        self.queue_max = queue_max or (config.queue_max if config else 8)
        self.client_hourly_limit = client_hourly_limit or (config.client_hourly_limit if config else 3)
        self.client_daily_limit = client_daily_limit or (config.client_daily_limit if config else 10)
        self.global_daily_uncached_limit = global_daily_uncached_limit or (
            config.global_daily_uncached_limit if config else 50
        )
        self.result_ttl_seconds = result_ttl_seconds or (config.result_ttl_seconds if config else 86_400)
        self.default_lease_seconds = lease_seconds or (config.lease_seconds if config else 120)
        self.job_timeout_seconds = job_timeout_seconds or (config.job_timeout_seconds if config else 1_800)
        self.max_attempts = max_attempts or (config.max_attempts if config else 2)
        self.busy_timeout_ms = busy_timeout_ms or (config.sqlite_busy_timeout_ms if config else 5_000)
        self._clock = clock
        self._init_lock = threading.Lock()
        self._initialized = False
        self.initialize()

    @classmethod
    def from_config(cls, config: AppConfig, **kwargs: Any) -> JobStore:
        return cls(config, **kwargs)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            if self.database_path != ":memory:":
                path = Path(self.database_path)
                path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version > SCHEMA_VERSION:
                    raise RuntimeError(f"Baza ma nowszy schemat ({version}) niż aplikacja ({SCHEMA_VERSION})")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        source_url TEXT NOT NULL,
                        video_id TEXT NOT NULL,
                        cache_key TEXT NOT NULL,
                        client_hash TEXT NOT NULL,
                        pipeline_version TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN ('PENDING','RUNNING','DONE','FAILED','CANCELLED')
                        ),
                        stage TEXT NOT NULL CHECK (
                            stage IN (
                                'QUEUED','VALIDATING','DOWNLOADING','PREPROCESSING',
                                'SEPARATING','ANALYZING','SIMPLIFYING','FINALIZING',
                                'CLEANING_UP','COMPLETE'
                            )
                        ),
                        progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
                        status_message TEXT,
                        cache_hit INTEGER NOT NULL DEFAULT 0 CHECK (cache_hit IN (0,1)),
                        uncached INTEGER NOT NULL DEFAULT 1 CHECK (uncached IN (0,1)),
                        cache_source_id TEXT,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        started_at REAL,
                        finished_at REAL,
                        expires_at REAL,
                        lease_owner TEXT,
                        lease_expires_at REAL,
                        heartbeat_at REAL,
                        result_json TEXT,
                        error_code TEXT,
                        error_message TEXT,
                        retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0,1))
                    );
                    CREATE INDEX IF NOT EXISTS idx_jobs_queue
                        ON jobs(status, created_at, id);
                    CREATE INDEX IF NOT EXISTS idx_jobs_client_time
                        ON jobs(client_hash, created_at);
                    CREATE INDEX IF NOT EXISTS idx_jobs_cache
                        ON jobs(cache_key, pipeline_version, status, expires_at);
                    CREATE INDEX IF NOT EXISTS idx_jobs_lease
                        ON jobs(status, lease_expires_at);
                    """
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._initialized = True

    def pragma_state(self) -> dict[str, int | str]:
        """Expose non-sensitive SQLite state for health checks and tests."""

        with self._connect() as connection:
            return {
                "user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
                "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
                "busy_timeout": int(connection.execute("PRAGMA busy_timeout").fetchone()[0]),
            }

    def _begin(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    def enqueue(
        self,
        source_url: str,
        *,
        client_hash: str,
        video_id: str | None = None,
        cache_key: str | None = None,
    ) -> JobRecord:
        canonical_url, parsed_video_id = validate_youtube_url(source_url)
        if video_id is not None and video_id != parsed_video_id:
            raise InvalidJobRequest()
        video_id = parsed_video_id
        key = (cache_key or video_id).strip()
        if not key or len(key) > 128 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", key):
            raise InvalidJobRequest()
        if not isinstance(client_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", client_hash):
            raise InvalidJobRequest()

        now = float(self._clock())
        hour_start = now - 3_600
        day_start = _utc_day_start(now)
        job_id = str(uuid4())
        with self._connect() as connection:
            self._begin(connection)
            try:
                self._purge_expired_results_tx(connection, now)
                if connection.execute(
                    "SELECT 1 FROM jobs WHERE client_hash=? AND status IN ('PENDING','RUNNING') LIMIT 1",
                    (client_hash,),
                ).fetchone():
                    raise ActiveJobLimit()

                hourly = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM jobs WHERE client_hash=? AND created_at>=?",
                        (client_hash, hour_start),
                    ).fetchone()[0]
                )
                if hourly >= self.client_hourly_limit:
                    retry_at = connection.execute(
                        "SELECT MIN(created_at)+3600 FROM jobs WHERE client_hash=? AND created_at>=?",
                        (client_hash, hour_start),
                    ).fetchone()[0]
                    raise HourlyRateLimit(retry_after=max(1, int(float(retry_at) - now)))

                daily = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM jobs WHERE client_hash=? AND created_at>=?",
                        (client_hash, day_start),
                    ).fetchone()[0]
                )
                if daily >= self.client_daily_limit:
                    raise DailyRateLimit(retry_after=max(1, int(day_start + 86_400 - now)))

                cached = connection.execute(
                    """
                    SELECT id, result_json, expires_at
                    FROM jobs
                    WHERE cache_key=? AND pipeline_version=? AND status='DONE'
                      AND result_json IS NOT NULL AND expires_at>?
                    ORDER BY finished_at DESC
                    LIMIT 1
                    """,
                    (key, self.pipeline_version, now),
                ).fetchone()

                if cached is None:
                    global_count = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM jobs WHERE uncached=1 AND created_at>=?",
                            (day_start,),
                        ).fetchone()[0]
                    )
                    if global_count >= self.global_daily_uncached_limit:
                        raise GlobalDailyLimit(retry_after=max(1, int(day_start + 86_400 - now)))
                    pending = int(
                        connection.execute("SELECT COUNT(*) FROM jobs WHERE status='PENDING'").fetchone()[0]
                    )
                    if pending >= self.queue_max:
                        raise QueueFull(retry_after=300)

                if cached is not None:
                    cached_result = json.loads(cached["result_json"])
                    if not isinstance(cached_result, dict):
                        raise RuntimeError("Cached result is not a JSON object")
                    # AnalysisResult belongs to this newly accepted job, even though
                    # its musical payload was reused.  Do not extend the original
                    # cache deadline by repeatedly requesting the same video.
                    cached_result["job_id"] = job_id
                    cached_result["pipeline_version"] = self.pipeline_version
                    cached_result["generated_at"] = _iso_timestamp(now)
                    cached_result["expires_at"] = _iso_timestamp(float(cached["expires_at"]))
                    cached_result_json = json.dumps(
                        cached_result,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    values = (
                        job_id,
                        canonical_url,
                        video_id,
                        key,
                        client_hash,
                        self.pipeline_version,
                        JobStatus.DONE.value,
                        JobStage.COMPLETE.value,
                        100,
                        "Wynik pobrany z pamięci podręcznej.",
                        1,
                        0,
                        cached["id"],
                        now,
                        now,
                        now,
                        now,
                        float(cached["expires_at"]),
                        cached_result_json,
                    )
                    connection.execute(
                        """
                        INSERT INTO jobs (
                            id, source_url, video_id, cache_key, client_hash,
                            pipeline_version, status, stage, progress, status_message,
                            cache_hit, uncached, cache_source_id, created_at, updated_at,
                            started_at, finished_at, expires_at, result_json
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        values,
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO jobs (
                            id, source_url, video_id, cache_key, client_hash,
                            pipeline_version, status, stage, progress, status_message,
                            cache_hit, uncached, created_at, updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            job_id,
                            canonical_url,
                            video_id,
                            key,
                            client_hash,
                            self.pipeline_version,
                            JobStatus.PENDING.value,
                            JobStage.QUEUED.value,
                            0,
                            "Oczekiwanie w kolejce.",
                            0,
                            1,
                            now,
                            now,
                        ),
                    )
                row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return JobRecord.from_row(row)

    def claim_next(self, worker_id: str, *, lease_seconds: int | None = None) -> JobRecord | None:
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id musi mieć od 1 do 128 znaków")
        lease = int(lease_seconds or self.default_lease_seconds)
        if lease <= 0:
            raise ValueError("lease_seconds musi być dodatnie")
        now = float(self._clock())
        with self._connect() as connection:
            self._begin(connection)
            try:
                self._recover_expired_tx(connection, now)
                # The deployment intentionally permits only one globally active job.
                if connection.execute("SELECT 1 FROM jobs WHERE status='RUNNING' LIMIT 1").fetchone():
                    connection.commit()
                    return None
                candidate = connection.execute(
                    "SELECT id FROM jobs WHERE status='PENDING' ORDER BY created_at, id LIMIT 1"
                ).fetchone()
                if candidate is None:
                    connection.commit()
                    return None
                job_id = candidate["id"]
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET status='RUNNING', updated_at=?, started_at=COALESCE(started_at, ?),
                        lease_owner=?, lease_expires_at=?, heartbeat_at=?,
                        attempt_count=attempt_count+1
                    WHERE id=? AND status='PENDING'
                    """,
                    (now, now, worker_id, now + lease, now, job_id),
                )
                if updated.rowcount != 1:
                    connection.rollback()
                    return None
                row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return JobRecord.from_row(row)

    def heartbeat(self, job_id: str, worker_id: str, *, lease_seconds: int | None = None) -> bool:
        lease = int(lease_seconds or self.default_lease_seconds)
        if lease <= 0:
            raise ValueError("lease_seconds musi być dodatnie")
        now = float(self._clock())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET heartbeat_at=?, lease_expires_at=?, updated_at=?
                WHERE id=? AND status='RUNNING' AND lease_owner=?
                  AND lease_expires_at>=?
                """,
                (now, now + lease, now, job_id, worker_id, now),
            )
            return cursor.rowcount == 1

    def update_progress(
        self,
        job_id: str,
        stage: JobStage | str,
        percent: int | float,
        message: str | None = None,
    ) -> JobRecord:
        try:
            requested_stage = stage if isinstance(stage, JobStage) else JobStage(str(stage))
        except ValueError as exc:
            raise ValueError(f"Nieznany etap: {stage}") from exc
        if not isinstance(percent, (int, float)) or isinstance(percent, bool):
            raise ValueError("percent musi być liczbą")
        requested_progress = max(0, min(99, int(percent)))
        now = float(self._clock())
        with self._connect() as connection:
            self._begin(connection)
            try:
                row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                if row is None:
                    raise JobNotFound()
                if row["status"] != JobStatus.RUNNING.value:
                    raise InvalidTransition()
                current_stage = JobStage(row["stage"])
                effective_stage = (
                    requested_stage
                    if _STAGE_ORDER[requested_stage] >= _STAGE_ORDER[current_stage]
                    else current_stage
                )
                effective_progress = max(int(row["progress"]), requested_progress)
                safe_message = _safe_status_message(message)
                connection.execute(
                    """
                    UPDATE jobs SET stage=?, progress=?, status_message=COALESCE(?, status_message),
                                    updated_at=?
                    WHERE id=?
                    """,
                    (effective_stage.value, effective_progress, safe_message, now, job_id),
                )
                result = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return JobRecord.from_row(result)

    def complete(self, job_id: str, result_dict: dict[str, Any]) -> JobRecord:
        if not isinstance(result_dict, dict):
            raise ValueError("result_dict musi być obiektem JSON")
        try:
            encoded = json.dumps(result_dict, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("Wynik nie jest poprawnym obiektem JSON") from exc
        now = float(self._clock())
        with self._connect() as connection:
            self._begin(connection)
            try:
                row = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
                if row is None:
                    raise JobNotFound()
                if row["status"] != JobStatus.RUNNING.value:
                    raise InvalidTransition()
                connection.execute(
                    """
                    UPDATE jobs
                    SET status='DONE', stage='COMPLETE', progress=100,
                        status_message='Analiza gotowa.', updated_at=?, finished_at=?,
                        expires_at=?, result_json=?, error_code=NULL, error_message=NULL,
                        retryable=0, lease_owner=NULL, lease_expires_at=NULL,
                        heartbeat_at=NULL
                    WHERE id=?
                    """,
                    (now, now, now + self.result_ttl_seconds, encoded, job_id),
                )
                result = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return JobRecord.from_row(result)

    def fail(
        self,
        job_id: str,
        code: str,
        message: str | None = None,
        retryable: bool = False,
    ) -> JobRecord:
        del message  # Upstream messages may contain paths, responses, tokens or tracebacks.
        safe_code, safe_message, default_retryable = _safe_error(code)
        now = float(self._clock())
        with self._connect() as connection:
            self._begin(connection)
            try:
                row = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
                if row is None:
                    raise JobNotFound()
                if row["status"] not in {JobStatus.RUNNING.value, JobStatus.PENDING.value}:
                    raise InvalidTransition()
                connection.execute(
                    """
                    UPDATE jobs
                    SET status='FAILED', updated_at=?, finished_at=?, error_code=?,
                        error_message=?, retryable=?, lease_owner=NULL,
                        lease_expires_at=NULL, heartbeat_at=NULL, result_json=NULL,
                        expires_at=NULL
                    WHERE id=?
                    """,
                    (
                        now,
                        now,
                        safe_code,
                        safe_message,
                        int(bool(retryable or default_retryable)),
                        job_id,
                    ),
                )
                result = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return JobRecord.from_row(result)

    def cancel(self, job_id: str, *, client_hash: str | None = None) -> JobRecord:
        now = float(self._clock())
        with self._connect() as connection:
            self._begin(connection)
            try:
                params: list[Any] = [job_id]
                predicate = "id=?"
                if client_hash is not None:
                    predicate += " AND client_hash=?"
                    params.append(client_hash)
                row = connection.execute(f"SELECT status FROM jobs WHERE {predicate}", params).fetchone()
                if row is None:
                    raise JobNotFound()
                if row["status"] not in {JobStatus.PENDING.value, JobStatus.RUNNING.value}:
                    raise InvalidTransition()
                safe_message = SAFE_ERROR_MESSAGES["CANCELLED"][0]
                connection.execute(
                    """
                    UPDATE jobs
                    SET status='CANCELLED', updated_at=?, finished_at=?,
                        error_code='CANCELLED', error_message=?, retryable=1,
                        lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL
                    WHERE id=?
                    """,
                    (now, now, safe_message, job_id),
                )
                result = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return JobRecord.from_row(result)

    def recover_expired(self) -> int:
        now = float(self._clock())
        with self._connect() as connection:
            self._begin(connection)
            try:
                count = self._recover_expired_tx(connection, now)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return count

    def _recover_expired_tx(self, connection: sqlite3.Connection, now: float) -> int:
        rows = connection.execute(
            "SELECT * FROM jobs WHERE status='RUNNING' AND lease_expires_at<?",
            (now,),
        ).fetchall()
        changed = 0
        for row in rows:
            started_at = float(row["started_at"] or row["created_at"])
            timed_out = now - started_at >= self.job_timeout_seconds
            exhausted = int(row["attempt_count"]) >= self.max_attempts
            if timed_out or exhausted:
                code = "PROCESSING_TIMEOUT" if timed_out else "WORKER_LOST"
                safe_message, retryable = SAFE_ERROR_MESSAGES[code]
                connection.execute(
                    """
                    UPDATE jobs
                    SET status='FAILED', updated_at=?, finished_at=?, error_code=?,
                        error_message=?, retryable=?, lease_owner=NULL,
                        lease_expires_at=NULL, heartbeat_at=NULL
                    WHERE id=? AND status='RUNNING'
                    """,
                    (now, now, code, safe_message, int(retryable), row["id"]),
                )
            else:
                # Keep progress/stage monotonic; the next worker resumes or safely
                # repeats an idempotent pipeline step using the same job directory.
                connection.execute(
                    """
                    UPDATE jobs
                    SET status='PENDING', updated_at=?, status_message='Wznawianie analizy.',
                        lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL
                    WHERE id=? AND status='RUNNING'
                    """,
                    (now, row["id"]),
                )
            changed += 1
        return changed

    def purge_expired_results(self) -> int:
        now = float(self._clock())
        with self._connect() as connection:
            self._begin(connection)
            try:
                count = self._purge_expired_results_tx(connection, now)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return count

    @staticmethod
    def _purge_expired_results_tx(connection: sqlite3.Connection, now: float) -> int:
        cursor = connection.execute(
            """
            UPDATE jobs SET result_json=NULL, expires_at=NULL
            WHERE status='DONE' AND expires_at IS NOT NULL AND expires_at<=?
            """,
            (now,),
        )
        return int(cursor.rowcount)

    def maintenance(self) -> dict[str, int]:
        return {
            "recovered": self.recover_expired(),
            "expired_results": self.purge_expired_results(),
        }

    def get_job(self, job_id: str, *, client_hash: str | None = None) -> JobRecord | None:
        params: list[Any] = [job_id]
        predicate = "id=?"
        if client_hash is not None:
            predicate += " AND client_hash=?"
            params.append(client_hash)
        with self._connect() as connection:
            row = connection.execute(f"SELECT * FROM jobs WHERE {predicate}", params).fetchone()
            if (
                row is not None
                and row["status"] == JobStatus.DONE.value
                and row["result_json"] is not None
                and row["expires_at"] is not None
                and float(row["expires_at"]) <= float(self._clock())
            ):
                connection.execute(
                    "UPDATE jobs SET result_json=NULL, expires_at=NULL WHERE id=?",
                    (job_id,),
                )
                row = connection.execute(f"SELECT * FROM jobs WHERE {predicate}", params).fetchone()
        return JobRecord.from_row(row) if row is not None else None

    def queue_position(self, job_id: str) -> int | None:
        with self._connect() as connection:
            row = connection.execute("SELECT status, created_at FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] != JobStatus.PENDING.value:
                return None
            ahead = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM jobs
                    WHERE status='PENDING' AND (created_at<? OR (created_at=? AND id<?))
                    """,
                    (row["created_at"], row["created_at"], job_id),
                ).fetchone()[0]
            )
        return ahead + 1

    def iter_jobs(self, *, status: JobStatus | str | None = None) -> Iterator[JobRecord]:
        params: tuple[Any, ...] = ()
        sql = "SELECT * FROM jobs"
        if status is not None:
            normalized = status if isinstance(status, JobStatus) else JobStatus(str(status))
            sql += " WHERE status=?"
            params = (normalized.value,)
        sql += " ORDER BY created_at, id"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        for row in rows:
            yield JobRecord.from_row(row)


def _utc_day_start(timestamp: float) -> float:
    value = datetime.fromtimestamp(timestamp, UTC)
    return datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp()


def _safe_status_message(message: str | None) -> str | None:
    if message is None:
        return None
    normalized = " ".join(str(message).split())[:200]
    return normalized or None


def _safe_error(code: str) -> tuple[str, str, bool]:
    normalized = str(code or "").strip().upper()
    normalized = _ERROR_ALIASES.get(normalized, normalized)
    if normalized not in SAFE_ERROR_MESSAGES:
        normalized = "PROCESSING_FAILED"
    message, retryable = SAFE_ERROR_MESSAGES[normalized]
    return normalized, message, retryable
