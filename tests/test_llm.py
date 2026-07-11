from __future__ import annotations

from pathlib import Path

from ogniskowy_grajek import llm
from ogniskowy_grajek.models import LLMChord, LLMTransformation, SimplificationMode


def proposal(count: int = 1) -> LLMTransformation:
    return LLMTransformation(
        capo_fret=2,
        strumming_pattern="D D U U D U",
        chords=[LLMChord(event_id=f"e{index:04d}", chord="C") for index in range(count)],
    )


def run_transform(tmp_path: Path, monkeypatch, *, free: str = "free", paid: str = "paid"):
    monkeypatch.setattr(llm, "_ollama", lambda **_kwargs: (_ for _ in ()).throw(OSError()))
    return llm.transform(
        bpm=100,
        meter="4/4",
        deterministic=proposal(),
        allowed={"C"},
        ollama_base_url="http://ollama.invalid",
        ollama_model="llama3:8b",
        gemini_model="gemini-3.1-flash-lite",
        gemini_api_key=free,
        gemini_api_key_paid=paid,
        ledger=llm.BudgetLedger(tmp_path / "jobs.sqlite3", 1.0),
    )


def test_paid_key_is_not_used_for_non_quota_failure(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def fail(*, api_key: str, **_kwargs):
        calls.append(api_key)
        raise TimeoutError

    monkeypatch.setattr(llm, "_gemini", fail)
    outcome = run_transform(tmp_path, monkeypatch)

    assert calls == ["free"]
    assert outcome.mode is SimplificationMode.DETERMINISTIC


def test_paid_key_gets_one_retry_after_quota_only(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    class QuotaError(RuntimeError):
        status_code = 429

    def generate(*, api_key: str, **_kwargs):
        calls.append(api_key)
        if api_key == "free":
            raise QuotaError
        return proposal(), 0.001

    monkeypatch.setattr(llm, "_gemini", generate)
    outcome = run_transform(tmp_path, monkeypatch)

    assert calls == ["free", "paid"]
    assert outcome.mode is SimplificationMode.GEMINI
    assert outcome.estimated_cost_usd == 0.001


def test_oversized_prompt_skips_gemini(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(llm, "_ollama", lambda **_kwargs: (_ for _ in ()).throw(OSError()))
    called = False

    def unexpected(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError

    monkeypatch.setattr(llm, "_gemini", unexpected)
    deterministic = proposal(300)
    outcome = llm.transform(
        bpm=100,
        meter="4/4",
        deterministic=deterministic,
        allowed={"C"},
        ollama_base_url="http://ollama.invalid",
        ollama_model="llama3:8b",
        gemini_model="gemini-3.1-flash-lite",
        gemini_api_key="free",
        gemini_api_key_paid="paid",
        ledger=llm.BudgetLedger(tmp_path / "jobs.sqlite3", 1.0),
    )

    assert not called
    assert outcome.mode is SimplificationMode.DETERMINISTIC
    assert "limit" in (outcome.warning or "")


def test_budget_reserves_maximum_call_cost(tmp_path: Path) -> None:
    ledger = llm.BudgetLedger(tmp_path / "jobs.sqlite3", llm.MAX_CALL_COST_USD)
    assert ledger.available()
    assert ledger.reserve()
    assert not ledger.available()
    assert not ledger.reserve()
