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
- Użytkownik utworzył w tunelu `BetaNode` osobną trasę Published application i CNAME dla
  `ogniskowy-grajek.klikfirma.pl`; rozpoczęto powrót Streamlit na root subdomeny.
- Przełączono Streamlit na root subdomeny; publiczne UI/health zwracają HTTPS 200, WebSocket 101,
  a regresja `api.klikfirma.pl` i `gra.klikfirma.pl` zakończyła się powodzeniem.
- Commit wdrożonego wariantu subdomeny: `3cec266`.
- Rozpoczęto v0.2 na `agent/lyrics-songbook-v2`: kontrakt wyniku i pipeline podniesiono do `2.0`.
- Dodano oryginalne napisy YouTube PL/EN, lokalny faster-whisper medium, bramki jakości i cleanup.
- Dodano przybliżoną sylabizację Pyphen, anchory akordów, linie instrumentalne i eksport ChordPro.
- Dodano bezpieczny fallback: dotychczasowe akordy oraz wyłącznie link wyszukiwania Ultimate Guitar.
- UI nadal wyświetla niewygasłe wyniki 1.0; tekst i audio nigdy nie trafiają do Ollamy ani Gemini.
- Testy przed publikacją: Ruff zaliczony, `111 passed`.
