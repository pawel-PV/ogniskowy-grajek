from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnalysisMode(StrEnum):
    DEMUCS_CUDA = "DEMUCS_CUDA"
    DEMUCS_CPU = "DEMUCS_CPU"
    MIX_APPROXIMATE = "MIX_APPROXIMATE"


class ChordDetector(StrEnum):
    CHORDINO = "CHORDINO"
    LIBROSA_TEMPLATE = "LIBROSA_TEMPLATE"


class SimplificationMode(StrEnum):
    OLLAMA = "OLLAMA"
    GEMINI = "GEMINI"
    DETERMINISTIC = "DETERMINISTIC"


class CleanupStatus(StrEnum):
    DONE = "DONE"
    DEFERRED = "DEFERRED"


class LyricsSource(StrEnum):
    YOUTUBE_MANUAL = "YOUTUBE_MANUAL"
    YOUTUBE_AUTO = "YOUTUBE_AUTO"
    LOCAL_ASR = "LOCAL_ASR"
    UNAVAILABLE = "UNAVAILABLE"


class SongbookLineKind(StrEnum):
    LYRIC = "LYRIC"
    INSTRUMENTAL = "INSTRUMENTAL"


class SourceInfo(StrictModel):
    platform: Literal["youtube"] = "youtube"
    video_id: str = Field(min_length=3, max_length=32)
    title: str = Field(min_length=1, max_length=300)
    duration_seconds: float = Field(gt=0, le=600)
    webpage_url: str = Field(max_length=512)


class TimelineEvent(StrictModel):
    event_id: str
    section_id: str | None = None
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    timestamp: str
    concert_chord: str
    played_chord: str
    difficult: bool = False

    @model_validator(mode="after")
    def validate_time_order(self) -> TimelineEvent:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


class SongSection(StrictModel):
    id: str
    label: str
    occurrence: int = Field(ge=1)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    start_display: str
    end_display: str
    chords: list[str]

    @model_validator(mode="after")
    def validate_time_order(self) -> SongSection:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


class SongbookSyllable(StrictModel):
    text: str = Field(min_length=1, max_length=64)
    trailing: str = Field(default="", max_length=8)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_time_order(self) -> SongbookSyllable:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


class SongbookAnchor(StrictModel):
    event_id: str
    chord: str
    syllable_index: int | None = Field(default=None, ge=0)


class SongbookLine(StrictModel):
    kind: SongbookLineKind
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    syllables: list[SongbookSyllable] = Field(default_factory=list)
    anchors: list[SongbookAnchor] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_line(self) -> SongbookLine:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        if self.kind is SongbookLineKind.LYRIC and not self.syllables:
            raise ValueError("lyric lines require syllables")
        if self.kind is SongbookLineKind.INSTRUMENTAL and self.syllables:
            raise ValueError("instrumental lines cannot contain syllables")
        for anchor in self.anchors:
            if anchor.syllable_index is not None and anchor.syllable_index >= len(self.syllables):
                raise ValueError("songbook anchor points outside the syllable list")
            if self.kind is SongbookLineKind.INSTRUMENTAL and anchor.syllable_index is not None:
                raise ValueError("instrumental anchors cannot point to syllables")
        return self


class Songbook(StrictModel):
    source: Literal[
        LyricsSource.YOUTUBE_MANUAL,
        LyricsSource.YOUTUBE_AUTO,
        LyricsSource.LOCAL_ASR,
    ]
    language: Literal["pl", "en"]
    confidence: float = Field(ge=0, le=1)
    alignment_mode: Literal["APPROXIMATE_SYLLABLE"] = "APPROXIMATE_SYLLABLE"
    lines: list[SongbookLine] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_chronology(self) -> Songbook:
        previous = -1.0
        for line in self.lines:
            if line.start_seconds < previous:
                raise ValueError("songbook lines must be chronological")
            previous = line.start_seconds
        return self


class Arrangement(StrictModel):
    bpm: int = Field(ge=40, le=240)
    meter: Literal["3/4", "4/4"]
    meter_confidence: float = Field(ge=0, le=1)
    capo_fret: int = Field(ge=0, le=7)
    strumming_pattern: str
    sections: list[SongSection]
    timeline: list[TimelineEvent]
    songbook: Songbook | None = None

    @model_validator(mode="after")
    def validate_chronology(self) -> Arrangement:
        previous = -1.0
        for event in self.timeline:
            if event.start_seconds < previous:
                raise ValueError("timeline must be chronological")
            previous = event.end_seconds
        previous = -1.0
        for section in self.sections:
            if section.start_seconds < previous:
                raise ValueError("sections must be chronological and non-overlapping")
            previous = section.end_seconds
        if self.songbook is not None:
            timeline_chords = {event.event_id: event.played_chord for event in self.timeline}
            for line in self.songbook.lines:
                for anchor in line.anchors:
                    if anchor.event_id not in timeline_chords:
                        raise ValueError("songbook anchor references an unknown timeline event")
                    if anchor.chord != timeline_chords[anchor.event_id]:
                        raise ValueError("songbook anchor chord differs from the authoritative timeline")
        return self


class ProcessingInfo(StrictModel):
    analysis_mode: AnalysisMode
    chord_detector: ChordDetector
    simplification_mode: SimplificationMode
    llm_model: str
    approximate: bool
    warnings: list[str] = Field(default_factory=list)
    cleanup_status: CleanupStatus = CleanupStatus.DONE
    estimated_cost_usd: float = Field(default=0.0, ge=0)
    lyrics_source: LyricsSource = LyricsSource.UNAVAILABLE
    transcription_model: str | None = Field(default=None, max_length=128)


class AnalysisResult(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    pipeline_version: str
    job_id: str
    source: SourceInfo
    arrangement: Arrangement
    processing: ProcessingInfo
    generated_at: datetime
    expires_at: datetime


class SafePublicError(StrictModel):
    code: str
    message: str
    retryable: bool = False


class LLMChord(StrictModel):
    event_id: str
    chord: str


class LLMTransformation(StrictModel):
    capo_fret: int = Field(ge=0, le=7)
    strumming_pattern: str = Field(min_length=1, max_length=64)
    chords: list[LLMChord]
