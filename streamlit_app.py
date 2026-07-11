"""Public Streamlit UI for Ogniskowy Grajek.

The UI only validates/enqueues work and polls SQLite.  Audio processing is always
performed by the separate worker process, so a browser refresh cannot interrupt it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

try:  # Pure helper functions remain importable in the lightweight test environment.
    import streamlit as st
except ModuleNotFoundError:  # pragma: no cover - exercised only outside the web image
    st = None  # type: ignore[assignment]

from ogniskowy_grajek.config import (
    AppConfig,
    ConfigurationError,
    client_hash_from_headers,
)
from ogniskowy_grajek.jobs import (
    JobRecord,
    JobStage,
    JobStatus,
    JobStore,
    JobStoreError,
    validate_youtube_url,
)
from ogniskowy_grajek.lyrics import (
    build_chordpro,
    render_songbook_line,
    ultimate_guitar_search_url,
)
from ogniskowy_grajek.models import Songbook, SongbookLineKind

STAGE_LABELS: Mapping[JobStage, str] = {
    JobStage.QUEUED: "W kolejce",
    JobStage.VALIDATING: "Sprawdzanie materiału",
    JobStage.DOWNLOADING: "Pobieranie dźwięku",
    JobStage.PREPROCESSING: "Przygotowanie dźwięku",
    JobStage.SEPARATING: "Separacja instrumentów",
    JobStage.ANALYZING: "Analiza rytmu i akordów",
    JobStage.SIMPLIFYING: "Upraszczanie chwytów",
    JobStage.FINALIZING: "Układanie opracowania",
    JobStage.CLEANING_UP: "Usuwanie plików roboczych",
    JobStage.COMPLETE: "Gotowe",
}


def stage_label(stage: JobStage | str) -> str:
    try:
        normalized = stage if isinstance(stage, JobStage) else JobStage(str(stage))
    except ValueError:
        return "Przetwarzanie"
    return STAGE_LABELS[normalized]


def result_view(result: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize AnalysisResult v1 into display-only values without audio paths."""

    arrangement = result.get("arrangement")
    if not isinstance(arrangement, Mapping):
        arrangement = {}
    processing = result.get("processing")
    if not isinstance(processing, Mapping):
        processing = {}

    sections: list[dict[str, Any]] = []
    raw_sections = arrangement.get("sections", [])
    if isinstance(raw_sections, list):
        for item in raw_sections:
            if not isinstance(item, Mapping):
                continue
            chords = item.get("chords", [])
            sections.append(
                {
                    "Sekcja": item.get("id") or item.get("label") or "—",
                    "Od": item.get("start_display") or _format_seconds(item.get("start_seconds")),
                    "Do": item.get("end_display") or _format_seconds(item.get("end_seconds")),
                    "Chwyty": " | ".join(map(str, chords))
                    if isinstance(chords, list)
                    else str(chords or "—"),
                }
            )

    timeline: list[dict[str, Any]] = []
    raw_timeline = arrangement.get("timeline", [])
    if isinstance(raw_timeline, list):
        for item in raw_timeline:
            if not isinstance(item, Mapping):
                continue
            timeline.append(
                {
                    "Czas": item.get("timestamp") or _format_seconds(item.get("start_seconds")),
                    "Sekcja": item.get("section_id") or "—",
                    "Chwyt": item.get("played_chord") or item.get("chord") or "—",
                    "Oryginał": item.get("concert_chord") or "—",
                    "Trudny": "tak" if item.get("difficult") else "",
                }
            )

    warnings = processing.get("warnings", [])
    if not isinstance(warnings, list):
        warnings = []
    songbook: Songbook | None = None
    raw_songbook = arrangement.get("songbook")
    if isinstance(raw_songbook, Mapping):
        try:
            songbook = Songbook.model_validate(raw_songbook)
        except Exception:
            songbook = None
    return {
        "capo": arrangement.get("capo_fret", 0),
        "bpm": arrangement.get("bpm", "—"),
        "meter": arrangement.get("meter", "—"),
        "meter_confidence": arrangement.get("meter_confidence"),
        "strumming": arrangement.get("strumming_pattern", "—"),
        "analysis_mode": processing.get("analysis_mode", "—"),
        "chord_detector": processing.get("chord_detector", "—"),
        "simplification_mode": processing.get("simplification_mode", "—"),
        "lyrics_source": processing.get("lyrics_source", "UNAVAILABLE"),
        "transcription_model": processing.get("transcription_model"),
        "warnings": [str(value) for value in warnings],
        "sections": sections,
        "timeline": timeline,
        "songbook": songbook,
    }


def _format_seconds(value: Any) -> str:
    try:
        seconds = max(0, int(float(value)))
    except (TypeError, ValueError):
        return "—"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _resources() -> tuple[AppConfig, JobStore]:
    config = AppConfig.from_env(require_secret=True)
    return config, JobStore.from_config(config)


if st is not None:  # Cache the connection settings, not a long-lived SQLite handle.
    _resources = st.cache_resource(show_spinner=False)(_resources)


def _request_headers() -> dict[str, str]:
    if st is None:
        return {}
    try:
        return {str(key): str(value) for key, value in st.context.headers.items()}
    except (AttributeError, RuntimeError):
        return {}


def _render_result(job: JobRecord) -> None:
    assert st is not None
    if not job.result:
        st.warning("Wynik tej analizy wygasł. Wklej link ponownie, aby wykonać nową analizę.")
        return
    view = result_view(job.result)
    if job.cache_hit:
        st.info("Gotowe od razu — użyto wyniku z ostatnich 24 godzin.")

    metric_capo, metric_bpm, metric_meter = st.columns(3)
    capo = view["capo"]
    metric_capo.metric("Kapodaster", "bez capo" if capo in (0, "0", None) else f"próg {capo}")
    metric_bpm.metric("Tempo", f"{view['bpm']} BPM")
    confidence = view["meter_confidence"]
    confidence_text = ""
    if isinstance(confidence, (int, float)):
        confidence_text = f"pewność {round(float(confidence) * 100):d}%"
    metric_meter.metric("Metrum", str(view["meter"]), confidence_text)

    st.subheader("Bicie")
    st.code(str(view["strumming"]), language=None)
    st.caption("D = ruch w dół · U = ruch w górę")

    songbook = view["songbook"]
    source = job.result.get("source") if isinstance(job.result.get("source"), Mapping) else {}
    if isinstance(songbook, Songbook):
        st.subheader("Śpiewnik — akordy nad sylabami")
        source_labels = {
            "YOUTUBE_MANUAL": "oryginalne napisy YouTube",
            "YOUTUBE_AUTO": "automatyczne napisy YouTube",
            "LOCAL_ASR": "lokalna transkrypcja wokalu",
        }
        st.caption(
            f"Źródło: {source_labels.get(songbook.source.value, songbook.source.value)} · "
            f"język: {songbook.language} · wyrównanie sylab jest przybliżone"
        )
        for line in songbook.lines:
            chord_line, text_line = render_songbook_line(line)
            if line.kind is SongbookLineKind.INSTRUMENTAL:
                timestamp = _format_seconds(line.start_seconds)
                st.code(f"[{timestamp}] {text_line}", language=None)
            else:
                st.code(f"{chord_line}\n{text_line}", language=None)
        chordpro = build_chordpro(
            songbook,
            title=str(source.get("title") or "Bez tytułu"),
            capo=int(view["capo"] or 0),
            bpm=int(view["bpm"]),
            meter=str(view["meter"]),
        ).encode("utf-8")
        st.download_button(
            "Pobierz śpiewnik ChordPro",
            data=chordpro,
            file_name=f"ogniskowy-grajek-{job.video_id}.cho",
            mime="text/plain; charset=utf-8",
            key=f"chordpro-{job.id}",
        )
    else:
        st.warning("Nie udało się uzyskać wystarczająco pewnego tekstu. Akordy pozostają dostępne.")
        st.link_button(
            "Szukaj tekstu i chwytów w Ultimate Guitar",
            ultimate_guitar_search_url(str(source.get("title") or "")),
        )

    if view["sections"]:
        st.subheader("Sekcje")
        st.dataframe(view["sections"], use_container_width=True, hide_index=True)
    if view["timeline"]:
        st.subheader("Chwyty w czasie")
        st.dataframe(view["timeline"], use_container_width=True, hide_index=True)

    st.caption(
        " · ".join(
            (
                f"Analiza: {view['analysis_mode']}",
                f"Akordy: {view['chord_detector']}",
                f"Uproszczenie: {view['simplification_mode']}",
                f"Tekst: {view['lyrics_source']}",
            )
        )
    )
    for warning in view["warnings"]:
        st.warning(warning)

    encoded = json.dumps(job.result, ensure_ascii=False, indent=2).encode("utf-8")
    st.download_button(
        "Pobierz wynik JSON",
        data=encoded,
        file_name=f"ogniskowy-grajek-{job.video_id}.json",
        mime="application/json",
        key=f"download-{job.id}",
    )


def _render_job(store: JobStore, job_id: str, identity: str) -> bool:
    """Render one snapshot; return true when no more polling is necessary."""

    assert st is not None
    job = store.get_job(job_id, client_hash=identity)
    if job is None:
        st.error("Nie znaleziono tej analizy w bieżącej sesji.")
        return True

    st.subheader("Stan analizy")
    st.progress(job.progress, text=f"{stage_label(job.stage)} · {job.progress}%")
    if job.status_message:
        st.caption(job.status_message)

    def render_cancel_button() -> None:
        if st.button("Anuluj analizę", key=f"cancel-{job.id}"):
            try:
                store.cancel(job.id, client_hash=identity)
            except JobStoreError as exc:
                st.error(exc.public_message)
            else:
                st.rerun(scope="app")

    if job.status is JobStatus.PENDING:
        position = store.queue_position(job.id)
        if position is not None:
            st.info(f"Pozycja w kolejce: {position}. Jednocześnie analizujemy jeden utwór.")
        render_cancel_button()
        return False
    if job.status is JobStatus.RUNNING:
        st.info("Analiza działa poza przeglądarką — możesz odświeżyć lub zamknąć tę kartę.")
        st.caption("Faktycznie użyty tryb CPU/GPU lub fallback pojawi się w gotowym wyniku.")
        render_cancel_button()
        return False
    if job.status is JobStatus.DONE:
        _render_result(job)
        return True
    if job.status is JobStatus.CANCELLED:
        st.warning(job.error_message or "Analiza została anulowana.")
        return True

    st.error(job.error_message or "Analiza nie powiodła się. Spróbuj ponownie.")
    if job.retryable:
        st.caption("Możesz ponowić zgłoszenie.")
    return True


def main() -> None:
    if st is None:  # pragma: no cover - friendly error for an incorrectly built image
        raise RuntimeError("Streamlit nie jest zainstalowany; użyj obrazu Docker target=web")

    st.set_page_config(
        page_title="Ogniskowy Grajek",
        page_icon="🎸",
        layout="centered",
    )
    st.title("🎸 Ogniskowy Grajek")
    st.write(
        "Wklej link do utworu z YouTube. Przygotujemy proste chwyty, kapodaster, "
        "jedno wygodne bicie oraz — jeśli jakość pozwoli — śpiewnik z akordami nad sylabami."
    )

    try:
        config, store = _resources()
    except ConfigurationError:
        st.error("Aplikacja nie ma kompletnej konfiguracji bezpieczeństwa.")
        st.stop()
        return
    except Exception:
        st.error("Usługa kolejki jest chwilowo niedostępna. Spróbuj później.")
        st.stop()
        return

    identity = client_hash_from_headers(_request_headers(), config.client_hmac_secret)

    with st.form("new-analysis", clear_on_submit=False):
        source_url = st.text_input(
            "Link do pojedynczego filmu YouTube",
            placeholder="https://www.youtube.com/watch?v=…",
            max_chars=2_048,
        )
        rights_confirmed = st.checkbox(
            "Mam prawa lub zgodę na analizę, transkrypcję, wyświetlenie i tymczasowe "
            "zapisanie tekstu tego materiału.",
        )
        submitted = st.form_submit_button("Przygotuj chwyty", type="primary")

    st.caption(
        "Nie pobieramy prywatnych filmów, playlist ani transmisji na żywo. "
        "Pliki dźwiękowe i ścieżki robocze są usuwane po analizie; wynik i tekst wygasają po 24 h."
    )
    with st.expander("Ważne informacje o materiale"):
        st.write(
            "Korzystaj wyłącznie z materiałów, które wolno Ci analizować. Zaznaczenie "
            "oświadczenia nie zastępuje praw ani zgody właściciela. Publiczne pobieranie "
            "z YouTube może podlegać również warunkom korzystania z tej usługi."
        )

    if submitted:
        if not rights_confirmed:
            st.error("Potwierdź prawa lub zgodę na analizę materiału.")
        else:
            try:
                # Validate before opening the write transaction for quicker feedback.
                validate_youtube_url(source_url)
                job = store.enqueue(source_url, client_hash=identity)
            except JobStoreError as exc:
                st.error(exc.public_message)
                if exc.retry_after:
                    minutes = max(1, round(exc.retry_after / 60))
                    st.caption(f"Ponowna próba za około {minutes} min.")
            except Exception:
                st.error("Nie udało się przyjąć zgłoszenia. Spróbuj ponownie.")
            else:
                st.session_state["job_id"] = job.id
                st.query_params["job"] = job.id
                st.session_state["job_terminal"] = job.status in {
                    JobStatus.DONE,
                    JobStatus.FAILED,
                    JobStatus.CANCELLED,
                }
                st.rerun()

    query_job = st.query_params.get("job")
    job_id = st.session_state.get("job_id") or (
        query_job if isinstance(query_job, str) and len(query_job) <= 64 else None
    )
    if not job_id:
        return
    st.session_state["job_id"] = str(job_id)

    if st.session_state.get("job_terminal"):
        _render_job(store, str(job_id), identity)
        return

    @st.fragment(run_every=config.poll_interval_seconds)
    def poll_job() -> None:
        terminal = _render_job(store, str(job_id), identity)
        if terminal:
            st.session_state["job_terminal"] = True
            st.rerun(scope="app")

    poll_job()


if __name__ == "__main__":
    main()
