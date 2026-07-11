from __future__ import annotations

import pytest
from pydantic import ValidationError

from ogniskowy_grajek.models import (
    Arrangement,
    LyricsSource,
    Songbook,
    SongbookAnchor,
    SongbookLine,
    SongbookLineKind,
    SongbookSyllable,
    SongSection,
    TimelineEvent,
)


def test_timeline_must_be_chronological() -> None:
    with pytest.raises(ValidationError):
        Arrangement(
            bpm=100,
            meter="4/4",
            meter_confidence=0.8,
            capo_fret=0,
            strumming_pattern="D D U U D U",
            sections=[],
            timeline=[
                TimelineEvent(
                    event_id="e1",
                    start_seconds=5,
                    end_seconds=7,
                    timestamp="00:05",
                    concert_chord="C",
                    played_chord="C",
                ),
                TimelineEvent(
                    event_id="e2",
                    start_seconds=6,
                    end_seconds=8,
                    timestamp="00:06",
                    concert_chord="G",
                    played_chord="G",
                ),
            ],
        )


def test_section_rejects_non_positive_duration() -> None:
    with pytest.raises(ValidationError):
        SongSection(
            id="A1",
            label="A",
            occurrence=1,
            start_seconds=2,
            end_seconds=2,
            start_display="00:02",
            end_display="00:02",
            chords=["C"],
        )


def test_songbook_anchor_must_reference_existing_syllable() -> None:
    with pytest.raises(ValidationError):
        Songbook(
            source=LyricsSource.LOCAL_ASR,
            language="pl",
            confidence=0.8,
            lines=[
                SongbookLine(
                    kind=SongbookLineKind.LYRIC,
                    start_seconds=0,
                    end_seconds=1,
                    syllables=[SongbookSyllable(text="la", start_seconds=0, end_seconds=1)],
                    anchors=[SongbookAnchor(event_id="e1", chord="C", syllable_index=2)],
                )
            ],
        )


def test_arrangement_rejects_songbook_anchor_outside_timeline() -> None:
    songbook = Songbook(
        source=LyricsSource.LOCAL_ASR,
        language="pl",
        confidence=0.8,
        lines=[
            SongbookLine(
                kind=SongbookLineKind.LYRIC,
                start_seconds=0,
                end_seconds=1,
                syllables=[SongbookSyllable(text="la", start_seconds=0, end_seconds=1)],
                anchors=[SongbookAnchor(event_id="unknown", chord="C", syllable_index=0)],
            )
        ],
    )

    with pytest.raises(ValidationError):
        Arrangement(
            bpm=100,
            meter="4/4",
            meter_confidence=0.8,
            capo_fret=0,
            strumming_pattern="D D U U D U",
            sections=[],
            timeline=[],
            songbook=songbook,
        )
