from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from .models import LLMTransformation, SimplificationMode

INPUT_PRICE_PER_MILLION = 0.25
OUTPUT_PRICE_PER_MILLION = 1.50
MAX_CALL_COST_USD = 0.00225
# This is deliberately conservative: a UTF-8 byte cannot expand to more than one
# tokenizer token. The response schema/provider framing still has ample headroom
# below the 3,000-token request ceiling.
MAX_PROMPT_BYTES = 1800
OLLAMA_TIMEOUT_SECONDS = 30
GEMINI_TIMEOUT_MS = 45_000


@dataclass(frozen=True)
class TransformOutcome:
    value: LLMTransformation
    mode: SimplificationMode
    model: str
    estimated_cost_usd: float = 0.0
    warning: str | None = None


class BudgetLedger:
    def __init__(self, database_path: Path, daily_budget_usd: float) -> None:
        self.database_path = database_path
        self.daily_budget_usd = daily_budget_usd

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS llm_daily_costs "
            "(day TEXT PRIMARY KEY, estimated_cost_usd REAL NOT NULL DEFAULT 0)"
        )
        connection.commit()
        return connection

    def available(self, reserve_usd: float = MAX_CALL_COST_USD) -> bool:
        day = datetime.now(UTC).date().isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT estimated_cost_usd FROM llm_daily_costs WHERE day = ?", (day,)
            ).fetchone()
        spent = float(row[0] if row else 0.0)
        return spent + max(0.0, reserve_usd) <= self.daily_budget_usd

    def record(self, cost_usd: float) -> None:
        day = datetime.now(UTC).date().isoformat()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO llm_daily_costs(day, estimated_cost_usd) VALUES (?, ?) "
                "ON CONFLICT(day) DO UPDATE SET estimated_cost_usd = "
                "estimated_cost_usd + excluded.estimated_cost_usd",
                (day, max(0.0, cost_usd)),
            )

    def reserve(self, cost_usd: float = MAX_CALL_COST_USD) -> bool:
        """Atomically reserve a conservative maximum before a billable call."""

        amount = max(0.0, float(cost_usd))
        day = datetime.now(UTC).date().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO llm_daily_costs(day, estimated_cost_usd) VALUES (?, 0)",
                (day,),
            )
            spent = float(
                connection.execute(
                    "SELECT estimated_cost_usd FROM llm_daily_costs WHERE day=?",
                    (day,),
                ).fetchone()[0]
            )
            if spent + amount > self.daily_budget_usd:
                connection.rollback()
                return False
            connection.execute(
                "UPDATE llm_daily_costs SET estimated_cost_usd=estimated_cost_usd+? WHERE day=?",
                (amount, day),
            )
            connection.commit()
        return True


def _prompt(*, bpm: int, meter: str, deterministic: LLMTransformation) -> str:
    payload = {
        "capo": deterministic.capo_fret,
        "strumming": deterministic.strumming_pattern,
        "events": [[item.event_id, item.chord] for item in deterministic.chords],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        "Jesteś pomocnikiem początkującego gitarzysty. Potwierdź lub popraw wyłącznie zapis chwytów, "
        "nie zmieniając event_id, czasu, podstawy harmonicznej ani dozwolonej palety. "
        "Zwróć wyłącznie JSON zgodny ze schematem. "
        f"BPM={bpm}; metrum={meter}; propozycja={encoded}"
    )


def _validate_candidate(
    raw: str | dict[str, Any], deterministic: LLMTransformation, allowed: set[str]
) -> LLMTransformation:
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        value = LLMTransformation.model_validate(data)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise ValueError("invalid structured output") from exc
    expected_ids = [item.event_id for item in deterministic.chords]
    actual_ids = [item.event_id for item in value.chords]
    if actual_ids != expected_ids or any(item.chord not in allowed for item in value.chords):
        raise ValueError("LLM changed event identity or used a forbidden chord")
    if [item.chord for item in value.chords] != [item.chord for item in deterministic.chords]:
        raise ValueError("LLM changed the deterministic harmonic mapping")
    if (
        value.capo_fret != deterministic.capo_fret
        or value.strumming_pattern != deterministic.strumming_pattern
    ):
        raise ValueError("LLM changed deterministic capo or strumming")
    return value


def _is_quota_error(exc: Exception) -> bool:
    """Only quota exhaustion may unlock the paid Gemini key."""
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status == 429 or str(status).upper() in {"429", "RESOURCE_EXHAUSTED"}:
        return True
    text = str(exc).upper()
    return "RESOURCE_EXHAUSTED" in text or ("429" in text and "QUOTA" in text)


def _ollama(
    *,
    base_url: str,
    model: str,
    prompt: str,
    deterministic: LLMTransformation,
    allowed: set[str],
) -> LLMTransformation:
    response = httpx.post(
        f"{base_url.rstrip('/')}/api/chat",
        json={
            "model": model,
            "stream": False,
            "format": LLMTransformation.model_json_schema(),
            "options": {"temperature": 0.1, "num_predict": 1000},
            "messages": [
                {"role": "system", "content": "Zwracaj wyłącznie poprawny JSON."},
                {"role": "user", "content": prompt},
            ],
        },
        timeout=OLLAMA_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()
    return _validate_candidate(str((data.get("message") or {}).get("content") or ""), deterministic, allowed)


def _gemini(
    *,
    api_key: str,
    model: str,
    prompt: str,
    deterministic: LLMTransformation,
    allowed: set[str],
) -> tuple[LLMTransformation, float]:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS))
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=1000,
            response_mime_type="application/json",
            response_json_schema=LLMTransformation.model_json_schema(),
            thinking_config=types.ThinkingConfig(thinking_level="minimal"),
        ),
    )
    value = _validate_candidate(response.text or "", deterministic, allowed)
    usage = getattr(response, "usage_metadata", None)
    input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
    output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
    cost = (input_tokens / 1_000_000 * INPUT_PRICE_PER_MILLION) + (
        output_tokens / 1_000_000 * OUTPUT_PRICE_PER_MILLION
    )
    return value, round(cost, 6)


def transform(
    *,
    bpm: int,
    meter: str,
    deterministic: LLMTransformation,
    allowed: set[str],
    ollama_base_url: str,
    ollama_model: str,
    gemini_model: str,
    gemini_api_key: str,
    gemini_api_key_paid: str,
    ledger: BudgetLedger,
) -> TransformOutcome:
    prompt = _prompt(bpm=bpm, meter=meter, deterministic=deterministic)
    try:
        value = _ollama(
            base_url=ollama_base_url,
            model=ollama_model,
            prompt=prompt,
            deterministic=deterministic,
            allowed=allowed,
        )
        return TransformOutcome(value, SimplificationMode.OLLAMA, ollama_model)
    except Exception:
        pass
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        return TransformOutcome(
            deterministic,
            SimplificationMode.DETERMINISTIC,
            "deterministic:v1",
            warning="Lista akordów przekracza bezpieczny limit wejścia AI; użyto algorytmu lokalnego.",
        )
    if gemini_api_key:
        if not ledger.reserve():
            return TransformOutcome(
                deterministic,
                SimplificationMode.DETERMINISTIC,
                "deterministic:v1",
                warning="Dzienny budżet Gemini został wyczerpany.",
            )
        try:
            value, cost = _gemini(
                api_key=gemini_api_key,
                model=gemini_model,
                prompt=prompt,
                deterministic=deterministic,
                allowed=allowed,
            )
            return TransformOutcome(value, SimplificationMode.GEMINI, gemini_model, cost)
        except Exception as exc:
            if gemini_api_key_paid and _is_quota_error(exc):
                if not ledger.reserve():
                    return TransformOutcome(
                        deterministic,
                        SimplificationMode.DETERMINISTIC,
                        "deterministic:v1",
                        warning="Dzienny budżet Gemini został wyczerpany.",
                    )
                try:
                    value, cost = _gemini(
                        api_key=gemini_api_key_paid,
                        model=gemini_model,
                        prompt=prompt,
                        deterministic=deterministic,
                        allowed=allowed,
                    )
                    return TransformOutcome(value, SimplificationMode.GEMINI, gemini_model, cost)
                except Exception:
                    pass
    return TransformOutcome(
        deterministic,
        SimplificationMode.DETERMINISTIC,
        "deterministic:v1",
        warning="Lokalny model i Gemini były niedostępne; użyto bezpiecznego algorytmu.",
    )
