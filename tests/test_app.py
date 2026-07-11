from __future__ import annotations

import tomllib
from pathlib import Path

from ogniskowy_grajek.jobs import JobStage
from streamlit_app import result_view, stage_label


def test_streamlit_uses_subdomain_root_path() -> None:
    config = tomllib.loads(Path(".streamlit/config.toml").read_text(encoding="utf-8"))

    assert "baseUrlPath" not in config["server"]


def test_polish_stage_labels_cover_contract() -> None:
    assert {stage_label(stage) for stage in JobStage}
    assert stage_label(JobStage.DOWNLOADING) == "Pobieranie dźwięku"
    assert stage_label(JobStage.COMPLETE) == "Gotowe"


def test_result_view_maps_analysis_result_v1() -> None:
    result = {
        "source": {
            "video_id": "dQw4w9WgXcQ",
            "title": "Próbka",
            "duration_seconds": 20,
        },
        "arrangement": {
            "bpm": 120,
            "meter": "4/4",
            "meter_confidence": 0.88,
            "capo_fret": 2,
            "strumming_pattern": "D D U U D U",
            "sections": [
                {
                    "id": "A1",
                    "label": "Sekcja A1",
                    "start_seconds": 0,
                    "end_seconds": 16,
                    "start_display": "00:00",
                    "end_display": "00:16",
                    "chords": ["C", "G", "Am", "Fmaj7"],
                }
            ],
            "timeline": [
                {
                    "event_id": "e1",
                    "section_id": "A1",
                    "start_seconds": 0,
                    "timestamp": "00:00",
                    "concert_chord": "D",
                    "played_chord": "C",
                    "difficult": False,
                }
            ],
        },
        "processing": {
            "analysis_mode": "DEMUCS_CPU",
            "chord_detector": "CHORDINO",
            "simplification_mode": "DETERMINISTIC",
            "warnings": ["Niska pewność metrum; użyto 4/4."],
        },
    }

    view = result_view(result)

    assert view["capo"] == 2
    assert view["bpm"] == 120
    assert view["meter"] == "4/4"
    assert view["strumming"] == "D D U U D U"
    assert view["sections"] == [
        {
            "Sekcja": "A1",
            "Od": "00:00",
            "Do": "00:16",
            "Chwyty": "C | G | Am | Fmaj7",
        }
    ]
    assert view["timeline"][0] == {
        "Czas": "00:00",
        "Sekcja": "A1",
        "Chwyt": "C",
        "Oryginał": "D",
        "Trudny": "",
    }
    assert view["analysis_mode"] == "DEMUCS_CPU"
    assert view["warnings"] == ["Niska pewność metrum; użyto 4/4."]


def test_result_view_ignores_invalid_or_audio_fields() -> None:
    view = result_view(
        {
            "arrangement": {"sections": ["bad"], "timeline": [None]},
            "processing": {"warnings": "not-a-list"},
            "audio_path": "/data/private/source.wav",
            "stems": ["drums.wav"],
        }
    )

    assert view["sections"] == []
    assert view["timeline"] == []
    assert view["warnings"] == []
    assert "audio" not in view
    assert "stems" not in view


def test_result_view_accepts_songbook_v2_but_old_result_stays_compatible() -> None:
    view = result_view(
        {
            "arrangement": {
                "sections": [],
                "timeline": [],
                "songbook": {
                    "source": "YOUTUBE_MANUAL",
                    "language": "pl",
                    "confidence": 1.0,
                    "alignment_mode": "APPROXIMATE_SYLLABLE",
                    "lines": [
                        {
                            "kind": "LYRIC",
                            "start_seconds": 0,
                            "end_seconds": 1,
                            "syllables": [
                                {
                                    "text": "Pło",
                                    "trailing": "",
                                    "start_seconds": 0,
                                    "end_seconds": 0.5,
                                },
                                {
                                    "text": "nie",
                                    "trailing": "",
                                    "start_seconds": 0.5,
                                    "end_seconds": 1,
                                },
                            ],
                            "anchors": [{"event_id": "e1", "chord": "C", "syllable_index": 0}],
                        }
                    ],
                },
            },
            "processing": {"warnings": [], "lyrics_source": "YOUTUBE_MANUAL"},
        }
    )

    assert view["songbook"] is not None
    assert view["songbook"].language == "pl"
    assert view["lyrics_source"] == "YOUTUBE_MANUAL"
