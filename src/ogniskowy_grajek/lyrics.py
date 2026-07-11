"""Automatic lyrics acquisition and static songbook alignment.

Lyrics are sourced only from original YouTube captions or a local ASR subprocess.
No function in this module fetches third-party lyrics websites or sends text/audio to
an LLM provider.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from .models import (
    LyricsSource,
    Songbook,
    SongbookAnchor,
    SongbookLine,
    SongbookLineKind,
    SongbookSyllable,
    TimelineEvent,
)

SUPPORTED_LANGUAGES = {"pl", "en"}
MIN_LEXICAL_WORDS = 8
MIN_LANGUAGE_PROBABILITY = 0.60
MIN_ASR_WORD_PROBABILITY = 0.40
_ANNOTATION_RE = re.compile(
    r"^\s*[\[(].*(?:music|muzyka|instrumental|applause|oklaski).*[\])]\s*$",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"\S+", re.UNICODE)


class LyricsError(RuntimeError):
    """Raised for optional lyrics failures; callers must keep the chord result."""


@dataclass(frozen=True, slots=True)
class SubtitleTrack:
    language: str
    track_key: str
    source: LyricsSource


@dataclass(frozen=True, slots=True)
class TimedWord:
    text: str
    start_seconds: float
    end_seconds: float
    probability: float = 1.0
    line_break: bool = False


@dataclass(frozen=True, slots=True)
class Transcript:
    words: tuple[TimedWord, ...]
    language: str
    confidence: float
    source: LyricsSource
    model: str | None = None


def _language_code(value: Any) -> str | None:
    candidate = str(value or "").strip().lower().replace("_", "-")
    prefix = candidate.split("-", 1)[0]
    return prefix if prefix in SUPPORTED_LANGUAGES else None


def select_original_subtitle(info: Mapping[str, Any]) -> SubtitleTrack | None:
    """Select a Polish/English original caption without translated auto tracks."""

    metadata_language = _language_code(info.get("language"))
    manual = info.get("subtitles")
    manual = manual if isinstance(manual, Mapping) else {}
    manual_candidates: list[tuple[str, str]] = []
    for key, formats in manual.items():
        language = _language_code(key)
        if (
            language
            and isinstance(formats, list)
            and any(isinstance(item, Mapping) and item.get("ext") == "json3" for item in formats)
        ):
            manual_candidates.append((str(key), language))
    if metadata_language:
        matching = [item for item in manual_candidates if item[1] == metadata_language]
        if matching:
            key, language = sorted(matching, key=lambda item: (item[0] != metadata_language, item[0]))[0]
            return SubtitleTrack(language, key, LyricsSource.YOUTUBE_MANUAL)
    unique_manual_languages = {language for _key, language in manual_candidates}
    if len(unique_manual_languages) == 1:
        language = unique_manual_languages.pop()
        key = sorted(key for key, candidate in manual_candidates if candidate == language)[0]
        return SubtitleTrack(language, key, LyricsSource.YOUTUBE_MANUAL)

    automatic = info.get("automatic_captions")
    automatic = automatic if isinstance(automatic, Mapping) else {}
    auto_candidates: list[tuple[str, str]] = []
    for language in sorted(SUPPORTED_LANGUAGES):
        key = f"{language}-orig"
        formats = automatic.get(key)
        if isinstance(formats, list) and any(
            isinstance(item, Mapping) and item.get("ext") == "json3" for item in formats
        ):
            auto_candidates.append((key, language))
    if metadata_language:
        matching = [item for item in auto_candidates if item[1] == metadata_language]
        if matching:
            key, language = matching[0]
            return SubtitleTrack(language, key, LyricsSource.YOUTUBE_AUTO)
    if len(auto_candidates) == 1:
        key, language = auto_candidates[0]
        return SubtitleTrack(language, key, LyricsSource.YOUTUBE_AUTO)
    return None


def download_subtitle_json3(
    source_url: str,
    track: SubtitleTrack,
    workspace: Path,
    *,
    max_bytes: int = 2 * 1024 * 1024,
) -> Path:
    """Download exactly one selected caption through yt-dlp's networking stack."""

    workspace.mkdir(parents=True, exist_ok=True)
    output = workspace / "captions"
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "cachedir": False,
        "socket_timeout": 20,
        "retries": 1,
        "extractor_retries": 1,
        "writesubtitles": track.source is LyricsSource.YOUTUBE_MANUAL,
        "writeautomaticsub": track.source is LyricsSource.YOUTUBE_AUTO,
        "subtitleslangs": [track.track_key],
        "subtitlesformat": "json3",
        "outtmpl": os.fspath(output),
        "overwrites": True,
    }
    try:
        import yt_dlp

        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([source_url])
    except Exception as exc:
        raise LyricsError("Nie udało się pobrać oryginalnych napisów.") from exc
    candidates = sorted(workspace.glob("captions*.json3"))
    if len(candidates) != 1 or not candidates[0].is_file():
        raise LyricsError("YouTube nie zwrócił wybranych napisów JSON3.")
    if candidates[0].stat().st_size <= 0 or candidates[0].stat().st_size > max_bytes:
        raise LyricsError("Plik napisów jest pusty albo przekracza limit 2 MB.")
    return candidates[0]


def _lexical_word_count(words: Sequence[TimedWord]) -> int:
    return sum(1 for word in words if any(character.isalpha() for character in word.text))


def _weighted_words(
    tokens: Sequence[str],
    *,
    start: float,
    end: float,
    probability: float,
) -> list[TimedWord]:
    clean = [token for token in tokens if token.strip()]
    if not clean:
        return []
    duration = max(0.05 * len(clean), end - start)
    weights = [max(1, sum(character.isalnum() for character in token)) for token in clean]
    total = sum(weights)
    cursor = start
    result: list[TimedWord] = []
    for index, (token, weight) in enumerate(zip(clean, weights, strict=True)):
        token_end = end if index == len(clean) - 1 else cursor + duration * weight / total
        token_end = max(cursor + 0.01, token_end)
        result.append(TimedWord(token, cursor, token_end, probability))
        cursor = token_end
    return result


def parse_youtube_json3(path: Path, track: SubtitleTrack) -> Transcript:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise LyricsError("Napisy JSON3 są nieprawidłowe.") from exc
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        raise LyricsError("Napisy nie zawierają listy zdarzeń.")
    result: list[TimedWord] = []
    previous_cue: tuple[str, float] | None = None
    base_probability = 1.0 if track.source is LyricsSource.YOUTUBE_MANUAL else 0.75
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("segs"), list):
            continue
        try:
            start = max(0.0, float(event.get("tStartMs") or 0) / 1000)
            duration = max(0.05, float(event.get("dDurationMs") or 0) / 1000)
        except (TypeError, ValueError):
            continue
        end = start + duration
        segments: list[tuple[float | None, str]] = []
        for segment in event["segs"]:
            if not isinstance(segment, dict):
                continue
            text = html.unescape(str(segment.get("utf8") or "")).replace("\n", " ")
            if not text:
                continue
            offset = segment.get("tOffsetMs")
            try:
                parsed_offset = float(offset) / 1000 if offset is not None else None
            except (TypeError, ValueError):
                parsed_offset = None
            segments.append((parsed_offset, text))
        cue_text = " ".join(text.strip() for _offset, text in segments if text.strip())
        cue_text = re.sub(r"\s+", " ", cue_text).strip()
        if not cue_text or _ANNOTATION_RE.match(cue_text) or cue_text.strip("♪♫ ") == "":
            continue
        normalized_cue = cue_text.casefold()
        if previous_cue and normalized_cue == previous_cue[0] and start - previous_cue[1] < 1.0:
            continue
        previous_cue = (normalized_cue, end)
        cue_words: list[TimedWord] = []
        for index, (offset, text) in enumerate(segments):
            tokens = _WORD_RE.findall(text)
            if not tokens:
                continue
            segment_start = start + (offset or 0.0)
            next_offsets = [candidate for candidate, _text in segments[index + 1 :] if candidate is not None]
            segment_end = min(end, start + next_offsets[0]) if next_offsets else end
            cue_words.extend(
                _weighted_words(
                    tokens,
                    start=max(start, segment_start),
                    end=max(segment_start + 0.05, segment_end),
                    probability=base_probability,
                )
            )
        if not cue_words:
            cue_words = _weighted_words(
                _WORD_RE.findall(cue_text),
                start=start,
                end=end,
                probability=base_probability,
            )
        if cue_words:
            cue_words[-1] = replace(cue_words[-1], line_break=True)
            result.extend(cue_words)
    result.sort(key=lambda word: (word.start_seconds, word.end_seconds))
    if _lexical_word_count(result) < MIN_LEXICAL_WORDS:
        raise LyricsError("Napisy zawierają mniej niż 8 użytecznych słów.")
    return Transcript(tuple(result), track.language, base_probability, track.source)


def run_local_asr(
    audio_path: Path,
    workspace: Path,
    *,
    model_path: str,
    model_name: str,
    timeout_seconds: int,
    device: str = "cpu",
) -> Transcript:
    output = workspace / "asr-result.json"
    command = [
        sys.executable,
        "-m",
        "ogniskowy_grajek.transcribe_cli",
        "--audio",
        os.fspath(audio_path),
        "--output",
        os.fspath(output),
        "--model",
        model_path,
        "--device",
        device,
    ]
    try:
        subprocess.run(command, check=True, timeout=max(1, timeout_seconds), capture_output=True)
        payload = json.loads(output.read_text(encoding="utf-8"))
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        raise LyricsError("Lokalna transkrypcja nie zakończyła się poprawnie.") from exc
    language = _language_code(payload.get("language"))
    try:
        language_probability = float(payload.get("language_probability") or 0)
    except (TypeError, ValueError) as exc:
        raise LyricsError("Transkrypcja nie zawiera poprawnej pewności języka.") from exc
    raw_words = payload.get("words")
    if language not in SUPPORTED_LANGUAGES or language_probability < MIN_LANGUAGE_PROBABILITY:
        raise LyricsError("Transkrypcja nie rozpoznała polskiego ani angielskiego z wystarczającą pewnością.")
    if not isinstance(raw_words, list):
        raise LyricsError("Transkrypcja nie zawiera timestampów słów.")
    words: list[TimedWord] = []
    for item in raw_words:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        try:
            start = float(item.get("start") or 0)
            end = float(item.get("end") or 0)
            probability = float(item.get("probability") or 0)
        except (TypeError, ValueError):
            continue
        if text and end > start >= 0:
            words.append(TimedWord(text, start, end, probability, bool(item.get("line_break"))))
    lexical = [word for word in words if any(character.isalpha() for character in word.text)]
    average_probability = sum(word.probability for word in lexical) / len(lexical) if lexical else 0.0
    if len(lexical) < MIN_LEXICAL_WORDS or average_probability < MIN_ASR_WORD_PROBABILITY:
        raise LyricsError("Lokalna transkrypcja ma zbyt mało pewnych słów.")
    confidence = min(language_probability, average_probability)
    return Transcript(tuple(words), language, confidence, LyricsSource.LOCAL_ASR, model_name)


def _split_lines(words: Sequence[TimedWord]) -> list[list[TimedWord]]:
    lines: list[list[TimedWord]] = []
    current: list[TimedWord] = []
    characters = 0
    for word in words:
        gap = word.start_seconds - current[-1].end_seconds if current else 0.0
        added = len(word.text) + (1 if current else 0)
        if current and (gap >= 1.2 or len(current) >= 10 or characters + added > 48):
            lines.append(current)
            current = []
            characters = 0
        current.append(word)
        characters += added
        punctuation_break = word.text.rstrip().endswith((".", "!", "?"))
        if word.line_break or punctuation_break:
            lines.append(current)
            current = []
            characters = 0
    if current:
        lines.append(current)
    return lines


def _syllable_parts(word: str, language: str) -> list[str]:
    letters = [index for index, character in enumerate(word) if character.isalpha()]
    if not letters:
        return [word]
    first, last = letters[0], letters[-1]
    leading, core, trailing = word[:first], word[first : last + 1], word[last + 1 :]
    try:
        import pyphen

        dictionary = pyphen.Pyphen(lang="pl_PL" if language == "pl" else "en_US")
        pieces = [piece for piece in dictionary.inserted(core, hyphen="-").split("-") if piece]
    except Exception:
        pieces = [core]
    if not pieces:
        pieces = [core]
    pieces[0] = leading + pieces[0]
    pieces[-1] = pieces[-1] + trailing
    return pieces


def _line_syllables(words: Sequence[TimedWord], language: str) -> list[SongbookSyllable]:
    result: list[SongbookSyllable] = []
    for word_index, word in enumerate(words):
        parts = _syllable_parts(word.text, language)
        weights = [max(1, sum(character.isalpha() for character in part)) for part in parts]
        total = sum(weights)
        cursor = word.start_seconds
        for index, (part, weight) in enumerate(zip(parts, weights, strict=True)):
            end = (
                word.end_seconds
                if index == len(parts) - 1
                else cursor + (word.end_seconds - word.start_seconds) * weight / total
            )
            result.append(
                SongbookSyllable(
                    text=part,
                    trailing=" " if index == len(parts) - 1 and word_index < len(words) - 1 else "",
                    start_seconds=cursor,
                    end_seconds=max(cursor + 0.001, end),
                )
            )
            cursor = end
    return result


def build_songbook(transcript: Transcript, timeline: Sequence[TimelineEvent]) -> Songbook:
    lyric_lines = [
        SongbookLine(
            kind=SongbookLineKind.LYRIC,
            start_seconds=words[0].start_seconds,
            end_seconds=words[-1].end_seconds,
            syllables=_line_syllables(words, transcript.language),
            anchors=[],
        )
        for words in _split_lines(transcript.words)
        if words
    ]
    assigned: dict[tuple[int, int], tuple[TimelineEvent, float]] = {}
    unmatched: list[TimelineEvent] = []
    previous_chord: str | None = None
    for event in timeline:
        if event.played_chord == previous_chord:
            continue
        previous_chord = event.played_chord
        match: tuple[int, int] | None = None
        for line_index, line in enumerate(lyric_lines):
            for syllable_index, syllable in enumerate(line.syllables):
                if syllable.start_seconds <= event.start_seconds < syllable.end_seconds:
                    match = (line_index, syllable_index)
                    break
            if match:
                break
        if match is None:
            future: list[tuple[float, int, int]] = []
            for line_index, line in enumerate(lyric_lines):
                for syllable_index, syllable in enumerate(line.syllables):
                    delta = syllable.start_seconds - event.start_seconds
                    if 0 <= delta <= 1.5:
                        future.append((delta, line_index, syllable_index))
            if future:
                _delta, line_index, syllable_index = min(future)
                match = (line_index, syllable_index)
        if match is None:
            unmatched.append(event)
            continue
        line_index, syllable_index = match
        distance = abs(event.start_seconds - lyric_lines[line_index].syllables[syllable_index].start_seconds)
        previous = assigned.get(match)
        if previous is None or distance < previous[1]:
            if previous is not None:
                unmatched.append(previous[0])
            assigned[match] = (event, distance)
        else:
            unmatched.append(event)
    for (line_index, syllable_index), (event, _distance) in assigned.items():
        line = lyric_lines[line_index]
        anchors = list(line.anchors)
        anchors.append(
            SongbookAnchor(
                event_id=event.event_id,
                chord=event.played_chord,
                syllable_index=syllable_index,
            )
        )
        anchors.sort(key=lambda anchor: anchor.syllable_index if anchor.syllable_index is not None else -1)
        lyric_lines[line_index] = line.model_copy(update={"anchors": anchors})
    instrumental: list[SongbookLine] = []
    group: list[TimelineEvent] = []
    for event in sorted(unmatched, key=lambda item: item.start_seconds):
        if group and event.start_seconds - group[-1].end_seconds > 2.0:
            instrumental.append(_instrumental_line(group))
            group = []
        group.append(event)
    if group:
        instrumental.append(_instrumental_line(group))
    lines = sorted([*lyric_lines, *instrumental], key=lambda line: (line.start_seconds, line.kind.value))
    return Songbook(
        source=transcript.source,
        language=transcript.language,  # type: ignore[arg-type]
        confidence=transcript.confidence,
        lines=lines,
    )


def _instrumental_line(events: Sequence[TimelineEvent]) -> SongbookLine:
    return SongbookLine(
        kind=SongbookLineKind.INSTRUMENTAL,
        start_seconds=events[0].start_seconds,
        end_seconds=max(event.end_seconds for event in events),
        anchors=[
            SongbookAnchor(event_id=event.event_id, chord=event.played_chord, syllable_index=None)
            for event in events
        ],
    )


def render_songbook_line(line: SongbookLine) -> tuple[str, str]:
    if line.kind is SongbookLineKind.INSTRUMENTAL:
        return "", " | ".join(anchor.chord for anchor in line.anchors)
    positions: list[int] = []
    text_parts: list[str] = []
    cursor = 0
    for syllable in line.syllables:
        positions.append(cursor)
        part = syllable.text + syllable.trailing
        text_parts.append(part)
        cursor += len(part)
    text_line = "".join(text_parts).rstrip()
    chord_line = ""
    for anchor in sorted(line.anchors, key=lambda item: item.syllable_index or 0):
        if anchor.syllable_index is None:
            continue
        position = positions[anchor.syllable_index]
        if len(chord_line) > position:
            position = len(chord_line) + 1
        chord_line += " " * (position - len(chord_line)) + anchor.chord
    return chord_line.rstrip(), text_line


def _escape_chordpro_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def build_chordpro(
    songbook: Songbook,
    *,
    title: str,
    capo: int,
    bpm: int,
    meter: str,
) -> str:
    safe_title = re.sub(r"[\r\n{}]", " ", title).strip()[:300] or "Bez tytułu"
    output = [
        f"{{title: {safe_title}}}",
        f"{{capo: {capo}}}",
        f"{{tempo: {bpm}}}",
        f"{{time: {meter}}}",
        "{comment: Automatyczna transkrypcja; wyrównanie sylab jest przybliżone}",
        "",
    ]
    for line in songbook.lines:
        if line.kind is SongbookLineKind.INSTRUMENTAL:
            timestamp = int(line.start_seconds)
            chords = " ".join(f"[{anchor.chord}]" for anchor in line.anchors)
            output.extend((f"{{comment: Instrumental {timestamp // 60:02d}:{timestamp % 60:02d}}}", chords))
            continue
        anchors = {anchor.syllable_index: anchor.chord for anchor in line.anchors}
        rendered = ""
        for index, syllable in enumerate(line.syllables):
            chord = anchors.get(index)
            if chord:
                rendered += f"[{chord}]"
            rendered += _escape_chordpro_text(syllable.text + syllable.trailing)
        output.append(rendered.rstrip())
    return "\n".join(output).rstrip() + "\n"


def ultimate_guitar_search_url(title: str) -> str:
    query = quote_plus(re.sub(r"[\r\n]", " ", title).strip()[:300])
    return f"https://www.ultimate-guitar.com/search.php?search_type=title&value={query}"
