from __future__ import annotations

import numpy as np
import pytest

from ogniskowy_grajek.music import (
    ChordSegment,
    capo_coverage,
    choose_capo,
    chord_shape_for_capo,
    detect_chords_from_chroma,
    group_four_bar_sections,
    infer_meter,
    minimum_chord_duration,
    normalize_chord,
    parse_chordino_csv,
    parse_chordino_rows,
    smooth_chord_segments,
    strumming_pattern,
    transpose_chord,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "N"),
        ("no chord", "N"),
        (' "C:maj" ', "C"),
        ("c:min7", "Cm7"),
        ("A#", "Bb"),
        ("B#", "C"),
        ("G♭:minor", "F#m"),
        ("D:major7/F#", "Dmaj7"),
        ("E:(3,5)", "E"),
    ],
)
def test_normalize_chordino_and_common_chord_spellings(raw: object, expected: str) -> None:
    assert normalize_chord(raw) == expected


def test_normalize_chord_rejects_malformed_label() -> None:
    with pytest.raises(ValueError, match="Unrecognized chord"):
        normalize_chord("H7")


def test_transpose_chord_preserves_quality_and_no_chord() -> None:
    assert transpose_chord("Bb:min7", 2) == "Cm7"
    assert transpose_chord("B", 1) == "C"
    assert transpose_chord("N", 7) == "N"


def test_parse_chordino_csv_builds_intervals_merges_duplicates_and_honours_duration() -> None:
    csv_text = """file,timestamp,label
song.wav,0.0,N
song.wav,1.0,C:maj
song.wav,3.0,C:maj
song.wav,4.0,A:min
song.wav,8.0,N
"""

    assert parse_chordino_csv(csv_text, duration_seconds=8.0) == [
        ChordSegment(0.0, 1.0, "N"),
        ChordSegment(1.0, 4.0, "C"),
        ChordSegment(4.0, 8.0, "Am"),
    ]


def test_parse_chordino_rows_accepts_mappings_and_last_duplicate_wins() -> None:
    rows = [
        {"timestamp": 0, "chord": "C"},
        {"time": 2, "label": "G"},
        {"start_seconds": 2, "chord": "A:min"},
    ]

    assert parse_chordino_rows(rows, duration_seconds=5) == [
        ChordSegment(0.0, 2.0, "C"),
        ChordSegment(2.0, 5.0, "Am"),
    ]


@pytest.mark.parametrize("bad_duration", [0, -1, float("inf"), float("nan")])
def test_parse_chordino_rejects_invalid_duration(bad_duration: float) -> None:
    with pytest.raises(ValueError, match="duration_seconds"):
        parse_chordino_rows([], duration_seconds=bad_duration)


def test_minimum_chord_duration_is_half_bar_but_never_below_one_second() -> None:
    assert minimum_chord_duration(60, "4/4") == pytest.approx(2.0)
    assert minimum_chord_duration(60, "3/4") == pytest.approx(1.5)
    assert minimum_chord_duration(180, "4/4") == pytest.approx(1.0)


def test_smoothing_absorbs_short_transition_into_dominant_neighbour_and_drops_island() -> None:
    raw = [
        ChordSegment(0.0, 4.0, "C:maj"),
        ChordSegment(4.0, 4.5, "G"),
        ChordSegment(4.5, 8.5, "A:min"),
        ChordSegment(8.5, 10.0, "N"),
        ChordSegment(10.0, 10.4, "E"),
    ]

    assert smooth_chord_segments(raw, bpm=120, meter="4/4") == [
        ChordSegment(0.0, 4.5, "C"),
        ChordSegment(4.5, 8.5, "Am"),
    ]


def test_smoothing_preserves_silence_gap_and_does_not_merge_across_it() -> None:
    raw = [
        ChordSegment(0, 2, "C"),
        ChordSegment(2, 3, "N"),
        ChordSegment(3, 5, "C"),
    ]

    assert smooth_chord_segments(raw, bpm=120) == [
        ChordSegment(0.0, 2.0, "C"),
        ChordSegment(3.0, 5.0, "C"),
    ]


def test_smoothing_keeps_segment_exactly_at_threshold() -> None:
    raw = [ChordSegment(0, 2, "C"), ChordSegment(2, 3, "G"), ChordSegment(3, 5, "Am")]
    assert smooth_chord_segments(raw, bpm=120) == [
        ChordSegment(0.0, 2.0, "C"),
        ChordSegment(2.0, 3.0, "G"),
        ChordSegment(3.0, 5.0, "Am"),
    ]


def test_smoothing_rejects_overlapping_segments() -> None:
    with pytest.raises(ValueError, match="must not overlap"):
        smooth_chord_segments(
            [ChordSegment(0, 2, "C"), ChordSegment(1, 3, "G")],
            bpm=120,
        )


def test_choose_capo_maximizes_coverage_and_ties_choose_lower_fret() -> None:
    assert choose_capo(["B", "E", "F#"]) == 2
    assert choose_capo(["C", "G"]) == 0


def test_capo_uses_duration_weighting_and_reports_ratio() -> None:
    segments = [
        ChordSegment(0, 8, "B"),
        ChordSegment(8, 9, "C"),
    ]
    result = capo_coverage(segments, 2)

    assert result.capo_fret == 2
    assert result.covered_seconds == pytest.approx(8)
    assert result.total_seconds == pytest.approx(9)
    assert result.ratio == pytest.approx(8 / 9)


def test_capo_shape_palette_simplifies_extensions_without_changing_root() -> None:
    assert chord_shape_for_capo("F:maj7", 0) == "Fmaj7"
    assert chord_shape_for_capo("F#:min7", 2) == "Em"
    assert chord_shape_for_capo("C:7", 0) == "C"
    assert chord_shape_for_capo("C:dim", 0) is None


@pytest.mark.parametrize(
    ("bpm", "meter", "expected"),
    [
        (90, "4/4", "D D U U D U"),
        (150, "4/4", "D D U U D U"),
        (151, "4/4", "D D D D"),
        (180, "3/4", "D D U D U"),
    ],
)
def test_strumming_patterns_are_deliberately_simple(bpm: int, meter: str, expected: str) -> None:
    assert strumming_pattern(bpm, meter) == expected


def test_infer_meter_detects_phase_shifted_four_four_accents() -> None:
    strengths = np.roll(np.tile([1.0, 0.2, 0.3, 0.2], 6), 2)
    estimate = infer_meter(strengths)

    assert estimate.meter == "4/4"
    assert estimate.confidence > 0.8
    assert estimate.score_4 > estimate.score_3
    assert estimate.warning is None


def test_infer_meter_detects_three_four_accents() -> None:
    strengths = np.tile([1.0, 0.15, 0.25], 8)
    estimate = infer_meter(strengths)

    assert estimate.meter == "3/4"
    assert estimate.confidence > 0.8
    assert estimate.score_3 > estimate.score_4


@pytest.mark.parametrize("strengths", [[], [1, 1, 1, 1, 1, 1], [1, 0.2, 1, 0.2]])
def test_infer_meter_falls_back_to_four_four_for_weak_evidence(strengths: list[float]) -> None:
    estimate = infer_meter(strengths)
    assert estimate.meter == "4/4"
    assert estimate.warning is not None


def _four_bar_progression(start: float, chords: tuple[str, str, str, str]) -> list[ChordSegment]:
    return [
        ChordSegment(start + index * 4, start + (index + 1) * 4, chord) for index, chord in enumerate(chords)
    ]


def test_group_four_bar_sections_labels_recurrence_as_a1_b1_a2() -> None:
    progression_a = ("C", "G", "Am", "F:maj7")
    progression_b = ("Dm", "A", "G", "D")
    segments = [
        *_four_bar_progression(0, progression_a),
        *_four_bar_progression(16, progression_b),
        *_four_bar_progression(32, progression_a),
    ]

    sections = group_four_bar_sections(segments, bpm=60, meter="4/4")

    assert [section.id for section in sections] == ["A1", "B1", "A2"]
    assert [section.label for section in sections] == ["A", "B", "A"]
    assert [section.occurrence for section in sections] == [1, 1, 2]
    assert sections[0].chords == ("C", "G", "Am", "Fmaj7")
    assert [(section.start_seconds, section.end_seconds) for section in sections] == [
        (0.0, 16.0),
        (16.0, 32.0),
        (32.0, 48.0),
    ]


def _set_template_frame(chroma: np.ndarray, frame: int, root: int, minor: bool = False) -> None:
    chroma[root, frame] = 1.0
    chroma[(root + (3 if minor else 4)) % 12, frame] = 0.8
    chroma[(root + 7) % 12, frame] = 0.7


def test_chroma_template_fallback_classifies_major_minor_and_unknown_runs() -> None:
    chroma = np.zeros((12, 8))
    for frame in range(3):
        _set_template_frame(chroma, frame, 0)  # C
    for frame in range(3, 6):
        _set_template_frame(chroma, frame, 9, minor=True)  # Am
    chroma[:, 6] = 1.0  # diffuse energy is not a confident triad
    times = np.arange(8) * 0.5

    assert detect_chords_from_chroma(chroma, times, duration_seconds=4.0) == [
        ChordSegment(0.0, 1.5, "C"),
        ChordSegment(1.5, 3.0, "Am"),
        ChordSegment(3.0, 4.0, "N"),
    ]


def test_chroma_fallback_rejects_bad_shape_and_non_monotonic_times() -> None:
    with pytest.raises(ValueError, match="shape"):
        detect_chords_from_chroma(np.zeros((4, 12)))
    with pytest.raises(ValueError, match="strictly increasing"):
        detect_chords_from_chroma(np.zeros((12, 2)), [0.0, 0.0])
