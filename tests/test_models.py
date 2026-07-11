from __future__ import annotations

import pytest
from pydantic import ValidationError

from ogniskowy_grajek.models import Arrangement, SongSection, TimelineEvent


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
