# Changelog agenta

## 2026-07-11

- Rozpoczęto osobną implementację MVP na `feat/mvp-cpu-deploy`.
- Zapisano zweryfikowany stan awarii eGPU i decyzję o wdrożeniu CPU-first.
- Dodano kontrakty projektu, obrazy CPU/GPU, Compose, CI i dokumentację operacyjną.
- Zaimplementowano trwałą kolejkę SQLite WAL, limity publiczne, Streamlit oraz pełny pipeline audio.
- Dodano niezależne fallbacki Demucs/HPSS, Chordino/librosa oraz Ollama/Gemini/deterministyczny.
- Dodano ścisłe kontrakty JSON, cleanup, cache 24 h, anulowanie i limit czasu zadania.
- Zaliczone: Ruff, 93 testy, build obrazu CPU i dwa realne E2E na legalnym materiale CC.
- Uruchomiono profil CPU na `127.0.0.1:8501` i włączono jednostkę systemd bez zmian innych usług.
- Potwierdzono brak regresji `api.klikfirma.pl` i `gra.klikfirma.pl`.
- Scalono PR #1 po zielonym CI, włączono ochronę `main` i utworzono tag `v0.1.0-cpu`.
- Przełączono działające obrazy z etykiety `dev` na SHA wydania `6235259`.
- Pierwotna publikacja na osobnej subdomenie oczekiwała na token Cloudflare z Tunnel Edit i DNS Edit.
- Przygotowano Streamlit, healthchecki i dokumentację do publikacji pod istniejącą ścieżką
  `https://api.klikfirma.pl/ogniskowy-grajek/`, bez zmian w `app_new` i bez nowego DNS.
- Zaliczone: Ruff, 94 testy, Compose, build web oraz lokalny smoke HTTP/WebSocket bazowej ścieżki.
- Commit implementacji prefiksu: `df76b02`.
