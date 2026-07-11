from __future__ import annotations

import ipaddress
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be"}
ProgressCallback = Callable[[float, str], None]


class IngestError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message
        self.retryable = retryable


@dataclass(frozen=True)
class VideoMetadata:
    video_id: str
    title: str
    duration_seconds: float
    webpage_url: str


def validate_youtube_url(url: str) -> str:
    candidate = url.strip()
    if not candidate or len(candidate) > 512:
        raise IngestError("INVALID_URL", "Link jest pusty albo zbyt długi.")
    parsed = urlparse(candidate)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise IngestError("INVALID_URL", "Dozwolone są wyłącznie bezpieczne linki HTTPS.")
    host = (parsed.hostname or "").rstrip(".").lower()
    if host not in ALLOWED_HOSTS:
        raise IngestError("UNSUPPORTED_SOURCE", "Obsługiwane są tylko pojedyncze filmy z YouTube.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise IngestError("INVALID_URL", "Adres IP nie jest dozwolonym źródłem.")
    query = parse_qs(parsed.query)
    if "list" in query:
        raise IngestError("PLAYLIST_NOT_ALLOWED", "Playlisty nie są obsługiwane.")
    if host == "youtu.be" and not parsed.path.strip("/"):
        raise IngestError("INVALID_URL", "Link nie zawiera identyfikatora filmu.")
    if (
        host != "youtu.be"
        and parsed.path not in {"/watch", "/shorts", "/embed"}
        and not parsed.path.startswith(("/shorts/", "/embed/"))
    ):
        raise IngestError("INVALID_URL", "Link nie wskazuje pojedynczego filmu YouTube.")
    return candidate


def _ydl_options() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "cachedir": False,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 1,
    }


def probe_video(url: str, *, max_duration_seconds: int = 600) -> VideoMetadata:
    safe_url = validate_youtube_url(url)
    try:
        import yt_dlp

        with yt_dlp.YoutubeDL(_ydl_options()) as ydl:
            info = ydl.extract_info(safe_url, download=False)
    except IngestError:
        raise
    except Exception as exc:
        raise IngestError(
            "PROBE_FAILED", "Nie udało się odczytać informacji o filmie.", retryable=True
        ) from exc
    if not isinstance(info, dict) or info.get("_type") == "playlist" or info.get("entries"):
        raise IngestError("PLAYLIST_NOT_ALLOWED", "Playlisty nie są obsługiwane.")
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise IngestError("LIVE_NOT_ALLOWED", "Transmisje na żywo nie są obsługiwane.")
    if info.get("availability") in {"private", "premium_only", "subscriber_only"}:
        raise IngestError("VIDEO_UNAVAILABLE", "Film prywatny lub płatny nie jest obsługiwany.")
    if int(info.get("age_limit") or 0) > 0:
        raise IngestError("AGE_RESTRICTED", "Materiały z ograniczeniem wiekowym nie są obsługiwane.")
    duration = float(info.get("duration") or 0)
    if duration <= 0:
        raise IngestError("DURATION_UNKNOWN", "Nie udało się ustalić długości filmu.")
    if duration > max_duration_seconds:
        raise IngestError("DURATION_LIMIT", "Film może mieć maksymalnie 10 minut.")
    video_id = str(info.get("id") or "")
    if not video_id:
        raise IngestError("PROBE_FAILED", "YouTube nie zwrócił identyfikatora filmu.")
    return VideoMetadata(
        video_id=video_id[:32],
        title=str(info.get("title") or "Bez tytułu")[:300],
        duration_seconds=duration,
        webpage_url=str(info.get("webpage_url") or safe_url)[:512],
    )


def download_wav(
    url: str,
    workspace: Path,
    *,
    progress: ProgressCallback,
    max_bytes: int = 100 * 1024 * 1024,
) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    safe_url = validate_youtube_url(url)

    def hook(data: dict[str, Any]) -> None:
        if data.get("status") == "downloading":
            total = float(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
            downloaded = float(data.get("downloaded_bytes") or 0)
            fraction = downloaded / total if total > 0 else 0.0
            progress(max(0.0, min(1.0, fraction)), "Pobieranie audio z YouTube")

    options = _ydl_options() | {
        "skip_download": False,
        "format": "bestaudio/best",
        "outtmpl": str(workspace / "source.%(ext)s"),
        "max_filesize": max_bytes,
        "progress_hooks": [hook],
        "overwrites": True,
    }
    try:
        import yt_dlp

        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([safe_url])
    except Exception as exc:
        raise IngestError("DOWNLOAD_FAILED", "Nie udało się pobrać audio.", retryable=True) from exc

    candidates = [
        path for path in workspace.glob("source.*") if path.suffix not in {".part", ".ytdl", ".wav"}
    ]
    if len(candidates) != 1:
        raise IngestError("DOWNLOAD_FAILED", "Pobrany plik audio jest niekompletny.", retryable=True)
    source = candidates[0]
    if source.stat().st_size > max_bytes:
        raise IngestError("SIZE_LIMIT", "Plik źródłowy przekracza limit 100 MB.")
    wav = workspace / "input.wav"
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        os.fspath(source),
        "-vn",
        "-ac",
        "2",
        "-ar",
        "44100",
        "-c:a",
        "pcm_s16le",
        os.fspath(wav),
    ]
    try:
        subprocess.run(command, check=True, timeout=180, capture_output=True)
    except (subprocess.SubprocessError, OSError) as exc:
        raise IngestError("TRANSCODE_FAILED", "Nie udało się przygotować pliku WAV.") from exc
    source.unlink(missing_ok=True)
    progress(1.0, "Audio przygotowane")
    return wav
