# Stan realizacji

Aktualizacja: 2026-07-11 19:05 UTC

## Wersja i środowisko

- Repozytorium: `pawel-PV/ogniskowy-grajek`.
- Kandydat v0.2: gałąź `agent/lyrics-songbook-v2`, baza `main` @ `033ab14`.
- Aktywna produkcja do czasu zielonego CI: v0.1, web `ogniskowy-grajek-web:3cec266`, worker
  `ogniskowy-grajek-worker-cpu:6235259`.
- Profil: `CPU`; GPU pozostaje niedostępne z powodu fizycznie odłączonego eGPU.
- Usługa `ogniskowy-grajek.service`: aktywna. Publiczny URL:
  `https://ogniskowy-grajek.klikfirma.pl/` — **LIVE v0.1**.

## Kandydat v0.2 — ukończone

- `AnalysisResult` i pipeline `2.0`; UI zachowuje odczyt niewygasłych wyników `1.0`.
- Oryginalne ręczne/automatyczne napisy YouTube PL/EN bez tłumaczeń, limit `json3` 2 MB.
- Lokalny `faster-whisper==1.2.1`, model medium z przypiętej rewizji, CPU INT8, VAD i timestampy słów.
- Bramki języka/pewności/liczby słów, timeout 600 s i rezerwa 240 s dla reszty pipeline.
- Sylabizacja Pyphen, anchory akordów, linie `LYRIC`/`INSTRUMENTAL`, monospace UI i UTF-8 ChordPro.
- Fallback do dotychczasowych akordów i wyłącznie linku wyszukiwania Ultimate Guitar.
- Rozszerzone oświadczenie o prawach; audio, wokal i napisy robocze są usuwane bezwarunkowo.

## Weryfikacja kandydata v0.2

- Ruff format/check, `git diff --check`, kompilacja modułów i `docker compose config -q`: zaliczone.
- Pytest: `111 passed` na Pythonie 3.11.
- Obrazy `web`, `worker-ci` i pełny `worker-cpu`: zbudowane.
- Worker CPU: SQLite WAL, FFmpeg i Chordino wykryte; syntetyczny rytm 120 BPM zmierzony jako 117 BPM.
- Przypięty Whisper medium: `--asr-smoke` załadował model lokalnie na CPU; obraz ma około 2,37 GB.
- Legalna próbka CC, napisy YouTube: `YOUTUBE_AUTO`, język `en`, 29 słów.
- Ta sama legalna próbka, wymuszony lokalny ASR na miksie: `LOCAL_ASR`, `en`, pewność 0,923, 21 słów.
- Pełny legalny E2E: yt-dlp → Demucs CPU → Chordino → śpiewnik `2.0`, 7 linii, cleanup `DONE`,
  workspace usunięty.

## Pozostało przed wydaniem

- Commit, draft PR, wymagany zielony check `ci`, squash merge i obrazy oznaczone SHA.
- Ponowne sprawdzenie pustej kolejki, przełączenie wyłącznie nowego web/worker i publiczny E2E v0.2.
- Regresja `api.klikfirma.pl`/`gra.klikfirma.pl`, potwierdzenie pustego workspace i tag `v0.2.0-cpu`.

## Blokery zewnętrzne

- **GPU: BLOCKED/DEFERRED.** Razer Core X ma stan `disconnected`, brak urządzenia NVIDIA w PCI,
  `nvidia-smi` nie komunikuje się ze sterownikiem, a Ollama jest offline. Wymagany fizyczny power-cycle
  obudowy eGPU i kontrola kabla Thunderbolt. Watchdogi GPU/Ollama pozostają wyłączone.
