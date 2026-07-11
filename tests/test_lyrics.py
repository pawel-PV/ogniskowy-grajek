from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from ogniskowy_grajek.lyrics import (
    LyricsError,
    SubtitleTrack,
    TimedWord,
    Transcript,
    build_chordpro,
    build_songbook,
    download_subtitle_json3,
    parse_youtube_json3,
    render_songbook_line,
    run_local_asr,
    select_original_subtitle,
    ultimate_guitar_search_url,
)
from ogniskowy_grajek.models import LyricsSource, SongbookLineKind, TimelineEvent

JSON3_FORMATS = [{"ext": "json3", "url": "https://www.youtube.com/api/timedtext"}]


def test_manual_original_subtitle_wins_over_translated_tracks() -> None:
    selected = select_original_subtitle(
        {
            "language": "en",
            "subtitles": {"pl": JSON3_FORMATS, "en": JSON3_FORMATS},
            "automatic_captions": {"en-orig": JSON3_FORMATS},
        }
    )

    assert selected == SubtitleTrack("en", "en", LyricsSource.YOUTUBE_MANUAL)


def test_only_original_automatic_caption_is_selected() -> None:
    selected = select_original_subtitle(
        {
            "language": "en-US",
            "subtitles": {},
            "automatic_captions": {
                "en-orig": JSON3_FORMATS,
                "pl": JSON3_FORMATS,
                "en": JSON3_FORMATS,
            },
        }
    )

    assert selected == SubtitleTrack("en", "en-orig", LyricsSource.YOUTUBE_AUTO)
    assert (
        select_original_subtitle({"automatic_captions": {"en": JSON3_FORMATS, "pl": JSON3_FORMATS}}) is None
    )


def test_parse_json3_keeps_timestamps_and_drops_music_marker(tmp_path: Path) -> None:
    path = tmp_path / "captions.en-orig.json3"
    path.write_text(
        json.dumps(
            {
                "events": [
                    {"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "[Music]"}]},
                    {
                        "tStartMs": 1000,
                        "dDurationMs": 4000,
                        "segs": [
                            {"utf8": "This ", "tOffsetMs": 0},
                            {"utf8": "is a simple song with eight clear words", "tOffsetMs": 700},
                        ],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    transcript = parse_youtube_json3(path, SubtitleTrack("en", "en-orig", LyricsSource.YOUTUBE_AUTO))

    assert transcript.source is LyricsSource.YOUTUBE_AUTO
    assert transcript.words[0].text == "This"
    assert transcript.words[0].start_seconds == 1.0
    assert transcript.words[-1].line_break is True
    assert all("Music" not in word.text for word in transcript.words)


def test_json3_with_too_few_words_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "captions.json3"
    path.write_text(
        json.dumps({"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "too short"}]}]}),
        encoding="utf-8",
    )

    with pytest.raises(LyricsError):
        parse_youtube_json3(path, SubtitleTrack("en", "en", LyricsSource.YOUTUBE_MANUAL))


def test_subtitle_download_rejects_file_over_limit(monkeypatch, tmp_path: Path) -> None:
    class FakeYoutubeDL:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def download(self, _urls):
            (tmp_path / "captions.en.json3").write_bytes(b"x" * 17)

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYoutubeDL))

    with pytest.raises(LyricsError):
        download_subtitle_json3(
            "https://youtu.be/dQw4w9WgXcQ",
            SubtitleTrack("en", "en", LyricsSource.YOUTUBE_MANUAL),
            tmp_path,
            max_bytes=16,
        )


def test_local_asr_quality_gate_and_private_subprocess_output(monkeypatch, tmp_path: Path) -> None:
    def fake_run(command, **_kwargs):
        output = Path(command[command.index("--output") + 1])
        output.write_text(
            json.dumps(
                {
                    "language": "pl",
                    "language_probability": 0.91,
                    "words": [
                        {"text": f"słowo{i}", "start": i, "end": i + 0.7, "probability": 0.8}
                        for i in range(8)
                    ],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("ogniskowy_grajek.lyrics.subprocess.run", fake_run)
    transcript = run_local_asr(
        tmp_path / "vocals.wav",
        tmp_path,
        model_path="/models/medium",
        model_name="medium",
        timeout_seconds=60,
    )

    assert transcript.language == "pl"
    assert transcript.source is LyricsSource.LOCAL_ASR
    assert transcript.model == "medium"
    assert transcript.confidence == pytest.approx(0.8)


def test_local_asr_rejects_low_word_confidence(monkeypatch, tmp_path: Path) -> None:
    def fake_run(command, **_kwargs):
        output = Path(command[command.index("--output") + 1])
        output.write_text(
            json.dumps(
                {
                    "language": "en",
                    "language_probability": 0.9,
                    "words": [
                        {"text": f"word{i}", "start": i, "end": i + 0.5, "probability": 0.1} for i in range(8)
                    ],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("ogniskowy_grajek.lyrics.subprocess.run", fake_run)
    with pytest.raises(LyricsError):
        run_local_asr(
            tmp_path / "vocals.wav",
            tmp_path,
            model_path="/models/medium",
            model_name="medium",
            timeout_seconds=60,
        )


def _timeline_event(event_id: str, start: float, end: float, chord: str) -> TimelineEvent:
    return TimelineEvent(
        event_id=event_id,
        start_seconds=start,
        end_seconds=end,
        timestamp=f"00:{int(start):02d}",
        concert_chord=chord,
        played_chord=chord,
    )


def test_songbook_maps_chords_to_polish_syllables_and_keeps_instrumental() -> None:
    transcript = Transcript(
        words=(
            TimedWord("Płonie", 5.0, 6.0, 0.9),
            TimedWord("ognisko", 6.0, 7.2, 0.9, True),
            TimedWord("piękna", 8.0, 9.0, 0.9),
            TimedWord("melodia", 9.0, 10.3, 0.9, True),
        ),
        language="pl",
        confidence=0.9,
        source=LyricsSource.LOCAL_ASR,
        model="medium",
    )
    songbook = build_songbook(
        transcript,
        [
            _timeline_event("e1", 0.0, 4.0, "C"),
            _timeline_event("e2", 5.55, 7.0, "G"),
            _timeline_event("e3", 8.2, 10.0, "Am"),
        ],
    )

    assert songbook.lines[0].kind is SongbookLineKind.INSTRUMENTAL
    lyric_lines = [line for line in songbook.lines if line.kind is SongbookLineKind.LYRIC]
    assert lyric_lines
    assert any(len(line.syllables) > 4 for line in lyric_lines)
    assert all(anchor.syllable_index is not None for line in lyric_lines for anchor in line.anchors)
    chord_line, text_line = render_songbook_line(lyric_lines[0])
    assert "G" in chord_line
    assert "Płonie".replace("nie", "") in text_line


def test_songbook_respects_caption_cue_boundaries() -> None:
    transcript = Transcript(
        words=tuple(
            TimedWord(f"word{index}", index, index + 0.8, 1.0, index in {1, 7}) for index in range(8)
        ),
        language="en",
        confidence=1.0,
        source=LyricsSource.YOUTUBE_MANUAL,
    )

    songbook = build_songbook(transcript, [_timeline_event("e1", 0.1, 8.0, "C")])
    lyric_lines = [line for line in songbook.lines if line.kind is SongbookLineKind.LYRIC]

    assert len(lyric_lines) == 2
    assert lyric_lines[0].end_seconds == pytest.approx(1.8)


def test_chordpro_is_utf8_safe_and_escapes_lyrics_brackets() -> None:
    transcript = Transcript(
        words=tuple(
            TimedWord(text, index, index + 0.8, 0.9, index == 7)
            for index, text in enumerate(
                ("This", "[bright]", "fire", "keeps", "our", "song", "alive", "tonight")
            )
        ),
        language="en",
        confidence=0.9,
        source=LyricsSource.YOUTUBE_MANUAL,
    )
    songbook = build_songbook(transcript, [_timeline_event("e1", 0.1, 8.0, "C")])

    output = build_chordpro(songbook, title="Test {Song}\n", capo=2, bpm=100, meter="4/4")

    assert "{capo: 2}" in output
    assert "[C]" in output
    assert "\\[bright\\]" in output
    assert "\n" in output


def test_ultimate_guitar_fallback_is_only_an_encoded_external_link() -> None:
    url = ultimate_guitar_search_url("Płonie ognisko & noc")

    assert url.startswith("https://www.ultimate-guitar.com/search.php?")
    assert "P%C5%82onie+ognisko+%26+noc" in url
