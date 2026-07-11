"""Isolated faster-whisper entrypoint used by the worker timeout boundary."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def transcribe(audio: Path, output: Path, model_path: str, device: str) -> None:
    from faster_whisper import WhisperModel

    compute_type = "float16" if device == "cuda" else "int8"
    model = WhisperModel(
        model_path,
        device=device,
        compute_type=compute_type,
        cpu_threads=4,
        num_workers=1,
        local_files_only=True,
    )
    segments, info = model.transcribe(
        str(audio),
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    words: list[dict[str, Any]] = []
    for segment in segments:
        segment_words = list(segment.words or [])
        for index, word in enumerate(segment_words):
            words.append(
                {
                    "text": word.word.strip(),
                    "start": word.start,
                    "end": word.end,
                    "probability": word.probability,
                    "line_break": index == len(segment_words) - 1,
                }
            )
    payload = {
        "language": info.language,
        "language_probability": info.language_probability,
        "words": words,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    args = parser.parse_args()
    transcribe(args.audio, args.output, args.model, args.device)


if __name__ == "__main__":
    main()
