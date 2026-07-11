from __future__ import annotations

import pytest

from ogniskowy_grajek.ingest import IngestError, validate_youtube_url


@pytest.mark.parametrize(
    "url",
    [
        "https://youtu.be/abc123xyz",
        "https://www.youtube.com/watch?v=abc123xyz",
        "https://music.youtube.com/watch?v=abc123xyz",
        "https://youtube.com/shorts/abc123xyz",
    ],
)
def test_accepts_single_youtube_video(url: str) -> None:
    assert validate_youtube_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://youtu.be/abc",
        "https://example.com/watch?v=abc",
        "https://www.youtube.com/watch?v=abc&list=PL123",
        "https://www.youtube.com/playlist?list=PL123",
        "https://127.0.0.1/watch?v=abc",
    ],
)
def test_rejects_unsafe_or_non_video_url(url: str) -> None:
    with pytest.raises(IngestError):
        validate_youtube_url(url)
