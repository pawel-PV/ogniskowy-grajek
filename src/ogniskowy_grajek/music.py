"""Deterministic music-analysis helpers used by Ogniskowy Grajek.

The functions in this module deliberately do not perform any file or network I/O.
They accept Chordino-like events or already-computed chroma/beat arrays, making the
fallback analysis straightforward to test and safe to run inside the worker.
"""

from __future__ import annotations

import csv
import io
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

__all__ = [
    "CapoCoverage",
    "ChordSegment",
    "HarmonicSection",
    "MeterEstimate",
    "capo_coverage",
    "chord_shape_for_capo",
    "choose_capo",
    "detect_chords_from_chroma",
    "group_four_bar_sections",
    "infer_meter",
    "minimum_chord_duration",
    "normalize_chord",
    "parse_chordino_csv",
    "parse_chordino_rows",
    "smooth_chord_segments",
    "strumming_pattern",
    "transpose_chord",
]


_NO_CHORD = {"", "n", "no chord", "nochord", "none", "x"}
_NOTE_NAMES = ("C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")
_NATURAL_PITCHES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_ROOT_RE = re.compile(r"^([A-Ga-g])([#b]?)(.*)$")
_CONTIGUOUS_EPSILON = 1e-6


@dataclass(frozen=True, slots=True)
class ChordSegment:
    """A half-open chord interval in seconds: ``[start_seconds, end_seconds)``."""

    start_seconds: float
    end_seconds: float
    chord: str

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds


@dataclass(frozen=True, slots=True)
class MeterEstimate:
    """Result of the phase-invariant 3/4 versus 4/4 accent heuristic."""

    meter: str
    confidence: float
    score_3: float
    score_4: float
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class HarmonicSection:
    """One four-bar harmonic window and its non-semantic A/B/C label."""

    id: str
    label: str
    occurrence: int
    start_seconds: float
    end_seconds: float
    chords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CapoCoverage:
    """How much recognized chord material is playable for a capo position."""

    capo_fret: int
    covered_seconds: float
    total_seconds: float

    @property
    def ratio(self) -> float:
        if self.total_seconds <= 0:
            return 0.0
        return self.covered_seconds / self.total_seconds


def _root_pitch(letter: str, accidental: str) -> int:
    pitch = _NATURAL_PITCHES[letter.upper()]
    if accidental == "#":
        pitch += 1
    elif accidental == "b":
        pitch -= 1
    return pitch % 12


def _normalize_quality(raw_quality: str) -> str:
    quality = raw_quality.strip().replace(" ", "")
    if quality.startswith(":"):
        quality = quality[1:]
    # Inversions do not change the chord used by a beginning guitarist.
    quality = quality.split("/", maxsplit=1)[0]
    # Harte degree lists such as C:maj(9) are detail beyond the open shape.
    quality = re.sub(r"\([^)]*\)", "", quality)
    lowered = quality.lower()

    if lowered in {"", "maj", "major"}:
        return ""
    if lowered in {"min", "minor", "mi", "-"}:
        return "m"
    if lowered.startswith("minor"):
        return "m" + lowered[len("minor") :]
    if lowered.startswith("min"):
        return "m" + lowered[len("min") :]
    if lowered.startswith("mi"):
        return "m" + lowered[len("mi") :]
    if lowered.startswith("major"):
        return "maj" + lowered[len("major") :]
    if lowered.startswith("maj"):
        return "maj" + lowered[len("maj") :]
    return lowered


def _split_normalized_chord(chord: str) -> tuple[int, str] | None:
    normalized = normalize_chord(chord)
    if normalized == "N":
        return None
    match = _ROOT_RE.match(normalized)
    if match is None:  # pragma: no cover - normalize_chord guarantees this
        return None
    return _root_pitch(match.group(1), match.group(2)), match.group(3)


def normalize_chord(chord: object) -> str:
    """Normalize a Chordino/Harte-style chord label.

    Enharmonic roots use a stable, mostly-flat spelling. Inversion/bass notes are
    discarded because the MVP presents a single beginner-friendly open shape.
    Unknown/no-chord values normalize to ``"N"``.

    Raises:
        ValueError: if a non-empty label is not a recognizable chord.
    """

    if chord is None:
        return "N"
    value = str(chord).strip().strip('"').replace("♯", "#").replace("♭", "b")
    if value.lower() in _NO_CHORD:
        return "N"

    match = _ROOT_RE.match(value)
    if match is None:
        raise ValueError(f"Unrecognized chord label: {value!r}")
    pitch = _root_pitch(match.group(1), match.group(2))
    quality = _normalize_quality(match.group(3))
    return f"{_NOTE_NAMES[pitch]}{quality}"


def transpose_chord(chord: str, semitones: int) -> str:
    """Transpose a normalized chord while retaining its quality suffix."""

    split = _split_normalized_chord(chord)
    if split is None:
        return "N"
    pitch, quality = split
    return f"{_NOTE_NAMES[(pitch + int(semitones)) % 12]}{quality}"


def parse_chordino_rows(
    rows: Iterable[Sequence[object] | Mapping[str, object]],
    *,
    duration_seconds: float,
) -> list[ChordSegment]:
    """Convert timestamped Chordino rows into contiguous chord intervals.

    Accepted sequence layouts are ``(timestamp, chord)``,
    ``(source, timestamp, chord)`` and Sonic Annotator's longer rows, for which
    the first numeric field is treated as the timestamp and the final field as
    the chord label. Mappings may use ``timestamp``/``time``/``start_seconds``
    and ``chord``/``label``. Duplicate timestamps use the last label.
    """

    duration = float(duration_seconds)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("duration_seconds must be a positive finite number")

    events: list[tuple[float, str]] = []
    for row_number, row in enumerate(rows, start=1):
        timestamp_value: object | None = None
        chord_value: object | None = None

        if isinstance(row, Mapping):
            for key in ("timestamp", "time", "start_seconds"):
                if key in row:
                    timestamp_value = row[key]
                    break
            for key in ("chord", "label"):
                if key in row:
                    chord_value = row[key]
                    break
        elif isinstance(row, Sequence) and not isinstance(row, (str, bytes)):
            if len(row) >= 2:
                chord_value = row[-1]
                for candidate in row[:-1]:
                    try:
                        timestamp_value = float(candidate)
                    except (TypeError, ValueError):
                        continue
                    else:
                        break
        else:
            raise ValueError(f"Invalid Chordino row {row_number}")

        if timestamp_value is None or chord_value is None:
            # CSV headers are harmless; malformed data rows are not.
            if row_number == 1:
                continue
            raise ValueError(f"Missing timestamp or chord in row {row_number}")
        try:
            timestamp = float(timestamp_value)
        except (TypeError, ValueError) as exc:
            if row_number == 1:
                continue
            raise ValueError(f"Invalid timestamp in row {row_number}") from exc
        if not math.isfinite(timestamp) or timestamp < 0 or timestamp >= duration:
            if math.isclose(timestamp, duration, abs_tol=_CONTIGUOUS_EPSILON):
                continue
            raise ValueError(f"Timestamp outside track duration in row {row_number}")
        events.append((timestamp, normalize_chord(chord_value)))

    if not events:
        return []

    # Stable sorting plus dict assignment makes the final event at a duplicate
    # timestamp authoritative, which matches a sequence of state-change events.
    events.sort(key=lambda item: item[0])
    deduplicated: dict[float, str] = {}
    for timestamp, chord in events:
        deduplicated[timestamp] = chord
    ordered = sorted(deduplicated.items())

    segments = [
        ChordSegment(start, ordered[index + 1][0] if index + 1 < len(ordered) else duration, chord)
        for index, (start, chord) in enumerate(ordered)
    ]
    return _merge_adjacent(segments)


def parse_chordino_csv(csv_text: str, *, duration_seconds: float) -> list[ChordSegment]:
    """Parse Sonic Annotator CSV text without performing file I/O."""

    return parse_chordino_rows(csv.reader(io.StringIO(csv_text)), duration_seconds=duration_seconds)


def _meter_numerator(meter: str | int) -> int:
    if meter in (3, "3/4"):
        return 3
    if meter in (4, "4/4"):
        return 4
    raise ValueError("meter must be '3/4', '4/4', 3, or 4")


def minimum_chord_duration(bpm: float, meter: str | int = "4/4") -> float:
    """Return ``max(1 second, half a bar)`` for the supplied tempo/meter."""

    tempo = float(bpm)
    if not math.isfinite(tempo) or tempo <= 0:
        raise ValueError("bpm must be a positive finite number")
    half_bar = (60.0 / tempo) * (_meter_numerator(meter) / 2.0)
    return max(1.0, half_bar)


def _validate_segments(segments: Iterable[ChordSegment]) -> list[ChordSegment]:
    normalized: list[ChordSegment] = []
    for segment in segments:
        start = float(segment.start_seconds)
        end = float(segment.end_seconds)
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError("Chord segments must have finite, positive, ordered intervals")
        normalized.append(ChordSegment(start, end, normalize_chord(segment.chord)))
    normalized.sort(key=lambda item: (item.start_seconds, item.end_seconds))
    for previous, current in zip(normalized, normalized[1:], strict=False):
        if current.start_seconds < previous.end_seconds - _CONTIGUOUS_EPSILON:
            raise ValueError("Chord segments must not overlap")
    return normalized


def _are_contiguous(left: ChordSegment, right: ChordSegment) -> bool:
    return math.isclose(
        left.end_seconds,
        right.start_seconds,
        abs_tol=_CONTIGUOUS_EPSILON,
        rel_tol=0.0,
    )


def _merge_adjacent(segments: Iterable[ChordSegment]) -> list[ChordSegment]:
    merged: list[ChordSegment] = []
    for segment in segments:
        if merged and merged[-1].chord == segment.chord and _are_contiguous(merged[-1], segment):
            merged[-1] = ChordSegment(
                merged[-1].start_seconds,
                segment.end_seconds,
                segment.chord,
            )
        else:
            merged.append(segment)
    return merged


def smooth_chord_segments(
    segments: Iterable[ChordSegment],
    *,
    bpm: float,
    meter: str | int = "4/4",
) -> list[ChordSegment]:
    """Remove no-chord spans and absorb transition artifacts into a neighbour.

    A segment is an artifact when it is shorter than ``max(1 s, half a bar)``.
    It adopts the chord of the longer contiguous neighbour; ties prefer the left
    neighbour so the operation is deterministic. An isolated short event is
    discarded. The operation repeats after each merge to handle noisy runs.
    Silence gaps are never filled or bridged.
    """

    threshold = minimum_chord_duration(bpm, meter)
    current = _merge_adjacent(segment for segment in _validate_segments(segments) if segment.chord != "N")

    while True:
        artifact_index = next(
            (
                index
                for index, segment in enumerate(current)
                if segment.duration_seconds < threshold - _CONTIGUOUS_EPSILON
            ),
            None,
        )
        if artifact_index is None:
            return current

        artifact = current[artifact_index]
        left = current[artifact_index - 1] if artifact_index > 0 else None
        right = current[artifact_index + 1] if artifact_index + 1 < len(current) else None
        if left is not None and not _are_contiguous(left, artifact):
            left = None
        if right is not None and not _are_contiguous(artifact, right):
            right = None

        if left is None and right is None:
            del current[artifact_index]
            continue
        if left is None:
            replacement_chord = right.chord  # type: ignore[union-attr]
        elif right is None or left.duration_seconds >= right.duration_seconds:
            replacement_chord = left.chord
        else:
            replacement_chord = right.chord

        current[artifact_index] = ChordSegment(
            artifact.start_seconds,
            artifact.end_seconds,
            replacement_chord,
        )
        current = _merge_adjacent(current)


def _triad_kind(quality: str) -> str | None:
    lowered = quality.lower()
    if any(marker in lowered for marker in ("dim", "aug", "b5", "#5", "ø", "°", "+")):
        return None
    if lowered.startswith("m") and not lowered.startswith("maj"):
        return "minor"
    # Dominant, major extensions, suspended chords, add chords and power chords
    # all retain their root and can safely use the plain open major shape.
    return "major"


_MAJOR_OPEN_SHAPES = {0: "C", 2: "D", 4: "E", 5: "Fmaj7", 7: "G", 9: "A"}
_MINOR_OPEN_SHAPES = {2: "Dm", 4: "Em", 9: "Am"}


def chord_shape_for_capo(chord: str, capo_fret: int) -> str | None:
    """Return the open shape sounding as ``chord`` at ``capo_fret``.

    ``None`` marks a difficult chord that the deterministic palette cannot
    represent without changing the harmony.
    """

    fret = int(capo_fret)
    if fret < 0 or fret > 7:
        raise ValueError("capo_fret must be between 0 and 7")
    split = _split_normalized_chord(chord)
    if split is None:
        return None
    pitch, quality = split
    kind = _triad_kind(quality)
    if kind is None:
        return None
    shape_pitch = (pitch - fret) % 12
    return (_MINOR_OPEN_SHAPES if kind == "minor" else _MAJOR_OPEN_SHAPES).get(shape_pitch)


def _weighted_chords(chords: Iterable[str | ChordSegment]) -> list[tuple[str, float]]:
    weighted: list[tuple[str, float]] = []
    for value in chords:
        if isinstance(value, ChordSegment):
            weight = max(0.0, float(value.duration_seconds))
            chord = normalize_chord(value.chord)
        else:
            weight = 1.0
            chord = normalize_chord(value)
        if chord != "N" and weight > 0:
            weighted.append((chord, weight))
    return weighted


def capo_coverage(chords: Iterable[str | ChordSegment], capo_fret: int) -> CapoCoverage:
    """Measure duration-weighted coverage of the beginner open-shape palette."""

    fret = int(capo_fret)
    if fret < 0 or fret > 7:
        raise ValueError("capo_fret must be between 0 and 7")
    weighted = _weighted_chords(chords)
    total = sum(weight for _, weight in weighted)
    covered = sum(weight for chord, weight in weighted if chord_shape_for_capo(chord, fret) is not None)
    return CapoCoverage(fret, covered, total)


def choose_capo(chords: Iterable[str | ChordSegment], *, max_fret: int = 7) -> int:
    """Choose capo 0..7 by maximum weighted coverage, preferring lower frets."""

    maximum = int(max_fret)
    if maximum < 0 or maximum > 7:
        raise ValueError("max_fret must be between 0 and 7")
    weighted = list(chords)
    coverages = [capo_coverage(weighted, fret) for fret in range(maximum + 1)]
    return max(coverages, key=lambda result: (result.covered_seconds, -result.capo_fret)).capo_fret


def strumming_pattern(bpm: float, meter: str | int) -> str:
    """Return one deliberately simple, dominant pattern for the whole song."""

    tempo = float(bpm)
    if not math.isfinite(tempo) or tempo <= 0:
        raise ValueError("bpm must be a positive finite number")
    numerator = _meter_numerator(meter)
    if numerator == 3:
        return "D D U D U"
    if tempo > 150:
        return "D D D D"
    return "D D U U D U"


def _accent_periodicity_score(strengths: np.ndarray, period: int) -> float:
    phase_means = np.array(
        [float(np.mean(strengths[phase::period])) for phase in range(period)],
        dtype=float,
    )
    downbeat_phase = int(np.argmax(phase_means))
    downbeat = phase_means[downbeat_phase]
    others = np.delete(phase_means, downbeat_phase)
    contrast = max(0.0, downbeat - float(np.mean(others)))
    scale = float(np.mean(np.abs(strengths))) + float(np.std(strengths)) + 1e-12
    return contrast / scale


def infer_meter(
    beat_strengths: Sequence[float] | np.ndarray,
    *,
    min_confidence: float = 0.2,
) -> MeterEstimate:
    """Infer 3/4 or 4/4 from phase-invariant beat accent periodicity.

    Low-information or ambiguous data deliberately falls back to 4/4 and carries
    a warning, rather than presenting a fragile 3/4 guess as certain.
    """

    values = np.asarray(beat_strengths, dtype=float).reshape(-1)
    values = np.where(np.isfinite(values), values, 0.0)
    values = np.clip(values, 0.0, None)
    threshold = float(min_confidence)
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("min_confidence must be between 0 and 1")

    if values.size < 6 or np.allclose(values, values[0] if values.size else 0.0):
        return MeterEstimate(
            meter="4/4",
            confidence=0.0,
            score_3=0.0,
            score_4=0.0,
            warning="Niska pewność metrum; przyjęto 4/4.",
        )

    score_3 = _accent_periodicity_score(values, 3)
    score_4 = _accent_periodicity_score(values, 4)
    confidence = abs(score_3 - score_4) / (score_3 + score_4 + 1e-12)
    winner = "3/4" if score_3 > score_4 else "4/4"
    warning = None
    if confidence < threshold:
        winner = "4/4"
        warning = "Niska pewność metrum; przyjęto 4/4."
    return MeterEstimate(
        meter=winner,
        confidence=float(np.clip(confidence, 0.0, 1.0)),
        score_3=score_3,
        score_4=score_4,
        warning=warning,
    )


def _alphabetic_label(index: int) -> str:
    """Return A..Z, AA..AZ, BA... for a zero-based pattern index."""

    label = ""
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        label = chr(ord("A") + remainder) + label
    return label


def _window_chords(
    segments: Sequence[ChordSegment],
    start_seconds: float,
    end_seconds: float,
) -> tuple[str, ...]:
    sequence: list[str] = []
    for segment in segments:
        if segment.end_seconds <= start_seconds + _CONTIGUOUS_EPSILON:
            continue
        if segment.start_seconds >= end_seconds - _CONTIGUOUS_EPSILON:
            break
        if segment.chord == "N":
            continue
        if not sequence or sequence[-1] != segment.chord:
            sequence.append(segment.chord)
    return tuple(sequence)


def group_four_bar_sections(
    segments: Iterable[ChordSegment],
    *,
    bpm: float,
    meter: str | int,
    start_seconds: float = 0.0,
    end_seconds: float | None = None,
) -> list[HarmonicSection]:
    """Label exact recurring four-bar chord patterns as A1/B1/A2, etc."""

    tempo = float(bpm)
    if not math.isfinite(tempo) or tempo <= 0:
        raise ValueError("bpm must be a positive finite number")
    numerator = _meter_numerator(meter)
    normalized = _validate_segments(segments)
    if not normalized:
        return []
    start = float(start_seconds)
    end = float(end_seconds) if end_seconds is not None else normalized[-1].end_seconds
    if not math.isfinite(start) or start < 0 or not math.isfinite(end) or end <= start:
        raise ValueError("Invalid section time range")

    four_bars = 4.0 * numerator * 60.0 / tempo
    pattern_labels: dict[tuple[str, ...], str] = {}
    occurrences: dict[str, int] = {}
    sections: list[HarmonicSection] = []
    window_start = start
    while window_start < end - _CONTIGUOUS_EPSILON:
        window_end = min(end, window_start + four_bars)
        pattern = _window_chords(normalized, window_start, window_end)
        if pattern:
            if pattern not in pattern_labels:
                pattern_labels[pattern] = _alphabetic_label(len(pattern_labels))
            label = pattern_labels[pattern]
            occurrence = occurrences.get(label, 0) + 1
            occurrences[label] = occurrence
            sections.append(
                HarmonicSection(
                    id=f"{label}{occurrence}",
                    label=label,
                    occurrence=occurrence,
                    start_seconds=window_start,
                    end_seconds=window_end,
                    chords=pattern,
                )
            )
        window_start = window_end
    return sections


def _chord_templates() -> tuple[np.ndarray, tuple[str, ...]]:
    templates: list[np.ndarray] = []
    labels: list[str] = []
    for root in range(12):
        for quality, third in (("", 4), ("m", 3)):
            template = np.zeros(12, dtype=float)
            template[root] = 1.0
            template[(root + third) % 12] = 0.8
            template[(root + 7) % 12] = 0.7
            template /= np.linalg.norm(template)
            templates.append(template)
            labels.append(f"{_NOTE_NAMES[root]}{quality}")
    return np.vstack(templates), tuple(labels)


_CHORD_TEMPLATES, _CHORD_TEMPLATE_LABELS = _chord_templates()


def detect_chords_from_chroma(
    chroma: np.ndarray,
    frame_times: Sequence[float] | np.ndarray | None = None,
    *,
    duration_seconds: float | None = None,
    sample_rate: int = 22_050,
    hop_length: int = 512,
    min_similarity: float = 0.6,
) -> list[ChordSegment]:
    """Classify a librosa-compatible 12xN chromagram with triad templates.

    This is the dependency-light fallback used when Chordino is unavailable.
    Low-energy or diffuse frames become ``N``; callers can subsequently apply
    :func:`smooth_chord_segments` with the track tempo.
    """

    matrix = np.asarray(chroma, dtype=float)
    if matrix.ndim != 2 or matrix.shape[0] != 12:
        raise ValueError("chroma must have shape (12, frame_count)")
    frame_count = matrix.shape[1]
    if frame_count == 0:
        return []
    if sample_rate <= 0 or hop_length <= 0:
        raise ValueError("sample_rate and hop_length must be positive")
    similarity_threshold = float(min_similarity)
    if not 0.0 <= similarity_threshold <= 1.0:
        raise ValueError("min_similarity must be between 0 and 1")

    if frame_times is None:
        step = hop_length / sample_rate
        times = np.arange(frame_count, dtype=float) * step
    else:
        times = np.asarray(frame_times, dtype=float).reshape(-1)
        if times.size != frame_count:
            raise ValueError("frame_times length must equal chroma frame count")
        if not np.all(np.isfinite(times)) or np.any(times < 0) or np.any(np.diff(times) <= 0):
            raise ValueError("frame_times must be finite, non-negative, and strictly increasing")
        step = float(np.median(np.diff(times))) if frame_count > 1 else hop_length / sample_rate

    duration = float(duration_seconds) if duration_seconds is not None else float(times[-1] + step)
    if not math.isfinite(duration) or duration <= times[-1]:
        raise ValueError("duration_seconds must be finite and later than the last frame")

    clean = np.clip(np.where(np.isfinite(matrix), matrix, 0.0), 0.0, None)
    norms = np.linalg.norm(clean, axis=0)
    normalized = np.divide(clean, norms, out=np.zeros_like(clean), where=norms > 1e-12)
    similarities = _CHORD_TEMPLATES @ normalized
    best_indices = np.argmax(similarities, axis=0)
    best_scores = similarities[best_indices, np.arange(frame_count)]
    labels = [
        _CHORD_TEMPLATE_LABELS[index] if norm > 1e-12 and score >= similarity_threshold else "N"
        for index, norm, score in zip(best_indices, norms, best_scores, strict=True)
    ]

    segments: list[ChordSegment] = []
    run_start = 0
    for index in range(1, frame_count + 1):
        if index < frame_count and labels[index] == labels[run_start]:
            continue
        end = float(times[index]) if index < frame_count else duration
        segments.append(ChordSegment(float(times[run_start]), end, labels[run_start]))
        run_start = index
    return segments
