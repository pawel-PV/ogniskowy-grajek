# Stan realizacji

Aktualizacja: 2026-07-11 17:13 UTC

## Wersja i środowisko

- Repozytorium: `pawel-PV/ogniskowy-grajek`.
- Wydanie: `main` @ `27cd8f6a9cb8`, tag `v0.2.0-cpu`.
- PR #5: squash merged; wymagany check `ci` zaliczony w 3 min 56 s.
- Aktywny profil: `CPU`; web `ogniskowy-grajek-web:27cd8f6a9cb8`, worker
  `ogniskowy-grajek-worker-cpu:27cd8f6a9cb8`, pipeline `2.0.0`.
- Usługa `ogniskowy-grajek.service`: aktywna. Publiczny URL:
  `https://ogniskowy-grajek.klikfirma.pl/` — **LIVE v0.2**.

## Ukończone w v0.2

- `AnalysisResult` i pipeline `2.0`; UI zachowuje odczyt niewygasłych wyników `1.0`.
- Oryginalne ręczne/automatyczne napisy YouTube PL/EN bez tłumaczeń, limit `json3` 2 MB.
- Lokalny `faster-whisper==1.2.1`, model medium z przypiętej rewizji, CPU INT8, VAD i timestampy słów.
- Bramki języka/pewności/liczby słów, timeout 600 s i rezerwa 240 s dla reszty pipeline.
- Sylabizacja Pyphen, anchory akordów, linie `LYRIC`/`INSTRUMENTAL`, monospace UI i UTF-8 ChordPro.
- Fallback do dotychczasowych akordów i wyłącznie linku wyszukiwania Ultimate Guitar.
- Rozszerzone oświadczenie o prawach; audio, wokal i napisy robocze są usuwane bezwarunkowo.

## Weryfikacja wydania

- Ruff format/check, `git diff --check`, kompilacja modułów i `docker compose config -q`: zaliczone.
- Pytest: `111 passed` na Pythonie 3.11; GitHub Actions `ci`: zaliczone.
- Obrazy `web`, `worker-ci` i pełny `worker-cpu`: zbudowane; finalny worker ma około 2,37 GB.
- Worker CPU: SQLite WAL, FFmpeg i Chordino wykryte; syntetyczny rytm 120 BPM zmierzony jako 117 BPM.
- Przypięty Whisper medium: model załadowany lokalnie przez `--asr-smoke` na CPU.
- Legalna próbka CC, wymuszony lokalny ASR: `LOCAL_ASR`, `en`, pewność 0,923, 21 słów.
- Produkcyjny E2E kolejki: `DONE`, schema 2.0, `YOUTUBE_AUTO`, 8 linii śpiewnika, eksport
  ChordPro 458 bajtów, cleanup `DONE`; `data/work` jest pusty.
- Publiczny health HTTPS 200 i WebSocket 101; regresja `api.klikfirma.pl` oraz `gra.klikfirma.pl`:
  HTTP 200. Kolejka po wdrożeniu: 0 aktywnych jobów.

## Blokery zewnętrzne

- **GPU: BLOCKED/DEFERRED.** Razer Core X ma stan `disconnected`, brak urządzenia NVIDIA w PCI,
  `nvidia-smi` nie komunikuje się ze sterownikiem, a Ollama jest offline. Wymagany fizyczny power-cycle
  obudowy eGPU i kontrola kabla Thunderbolt. Watchdogi GPU/Ollama pozostają wyłączone.

Następny krok po powrocie użytkownika: fizyczny power-cycle eGPU i pełny smoke CUDA/VRAM.
