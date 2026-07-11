"""Runtime configuration and privacy-preserving client identification.

The module deliberately has no framework dependencies.  Both Streamlit and the
worker can therefore load exactly the same limits without importing one another.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


class ConfigurationError(RuntimeError):
    """Raised when runtime configuration is unsafe or internally inconsistent."""


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} musi być liczbą całkowitą") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} musi mieć wartość co najmniej {minimum}")
    return value


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _env_int_alias(names: tuple[str, ...], default: int, *, minimum: int = 1) -> int:
    for name in names:
        if os.getenv(name) not in (None, ""):
            return _env_int(name, default, minimum=minimum)
    return default


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} musi być liczbą") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} musi mieć wartość co najmniej {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Shared settings for the web process and the single queue worker."""

    database_path: Path = Path("/data/ogniskowy-grajek.sqlite3")
    work_dir: Path = Path("/data/work")
    pipeline_version: str = "1"
    client_hmac_secret: str = ""
    queue_max: int = 8
    client_hourly_limit: int = 3
    client_daily_limit: int = 10
    global_daily_uncached_limit: int = 50
    result_ttl_seconds: int = 24 * 60 * 60
    lease_seconds: int = 120
    job_timeout_seconds: int = 30 * 60
    max_attempts: int = 2
    sqlite_busy_timeout_ms: int = 5_000
    poll_interval_seconds: float = 1.0

    @classmethod
    def from_env(cls, *, require_secret: bool = True) -> AppConfig:
        """Load settings from environment variables and validate public limits.

        A long random HMAC secret is mandatory for a public deployment.  Tests and
        local tools may pass ``require_secret=False`` but still receive a stable
        development-only value.
        """

        secret = _first_env("RATE_LIMIT_SECRET", "CLIENT_HMAC_SECRET")
        if not secret and not require_secret:
            secret = "development-only-change-me"
        config = cls(
            database_path=Path(
                _first_env(
                    "APP_DATABASE_PATH",
                    "OGNISKOWY_DATABASE_PATH",
                    default="/data/ogniskowy-grajek.sqlite3",
                )
            ),
            work_dir=Path(_first_env("APP_WORK_ROOT", "OGNISKOWY_WORK_DIR", default="/data/work")),
            pipeline_version=os.getenv("PIPELINE_VERSION", "1").strip() or "1",
            client_hmac_secret=secret,
            queue_max=_env_int_alias(("MAX_QUEUE_SIZE", "QUEUE_MAX"), 8),
            client_hourly_limit=_env_int_alias(("MAX_JOBS_PER_HOUR", "CLIENT_HOURLY_LIMIT"), 3),
            client_daily_limit=_env_int_alias(("MAX_JOBS_PER_DAY", "CLIENT_DAILY_LIMIT"), 10),
            global_daily_uncached_limit=_env_int_alias(
                ("MAX_GLOBAL_JOBS_PER_DAY", "GLOBAL_DAILY_UNCACHED_LIMIT"), 50
            ),
            result_ttl_seconds=(
                _env_int("RESULT_TTL_SECONDS", 24 * 60 * 60)
                if os.getenv("RESULT_TTL_SECONDS")
                else _env_int("RESULT_TTL_HOURS", 24) * 60 * 60
            ),
            lease_seconds=_env_int("JOB_LEASE_SECONDS", 120),
            job_timeout_seconds=_env_int("JOB_TIMEOUT_SECONDS", 30 * 60),
            max_attempts=_env_int("JOB_MAX_ATTEMPTS", 2),
            sqlite_busy_timeout_ms=_env_int("SQLITE_BUSY_TIMEOUT_MS", 5_000),
            poll_interval_seconds=_env_float("UI_POLL_INTERVAL_SECONDS", 1.0, minimum=0.2),
        )
        config.validate(require_secret=require_secret)
        return config

    def validate(self, *, require_secret: bool = True) -> None:
        if require_secret and (
            len(self.client_hmac_secret.encode("utf-8")) < 32
            or self.client_hmac_secret.lower().startswith("replace-with")
        ):
            raise ConfigurationError(
                "CLIENT_HMAC_SECRET musi mieć co najmniej 32 bajty; "
                "wygeneruj go np. poleceniem `openssl rand -hex 32`"
            )
        if not self.pipeline_version or len(self.pipeline_version) > 64:
            raise ConfigurationError("PIPELINE_VERSION musi mieć od 1 do 64 znaków")
        if self.lease_seconds >= self.job_timeout_seconds:
            raise ConfigurationError("JOB_LEASE_SECONDS musi być krótszy niż JOB_TIMEOUT_SECONDS")
        if self.client_hourly_limit > self.client_daily_limit:
            raise ConfigurationError("Limit godzinowy klienta nie może przekraczać dziennego")


# A short alias is convenient in worker entrypoints and remains explicit in type hints.
Settings = AppConfig


def canonical_client_ip(value: str | None) -> str:
    """Return a canonical address without ever persisting its textual input.

    All direct/non-Cloudflare connections deliberately share one rate-limit bucket.
    This is fail-closed: forging different forwarding headers outside Cloudflare does
    not create unlimited identities.
    """

    if not value:
        return "direct-connection"
    candidate = value.strip()
    # CF-Connecting-IP is a single value.  Refuse comma-separated forwarding chains.
    if not candidate or "," in candidate:
        return "direct-connection"
    try:
        return ipaddress.ip_address(candidate).compressed
    except ValueError:
        return "direct-connection"


def client_hash(ip_address: str | None, secret: str) -> str:
    """Create the only client identifier that may be stored in SQLite."""

    if not secret:
        raise ConfigurationError("Brak CLIENT_HMAC_SECRET")
    canonical = canonical_client_ip(ip_address)
    return hmac.new(secret.encode("utf-8"), canonical.encode("ascii"), sha256).hexdigest()


def client_hash_from_headers(headers: Mapping[str, str], secret: str) -> str:
    """Hash ``CF-Connecting-IP`` using case-insensitive header lookup."""

    address: str | None = None
    for key, value in headers.items():
        if str(key).lower() == "cf-connecting-ip":
            address = str(value)
            break
    return client_hash(address, secret)
