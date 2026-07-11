# Stan realizacji

Aktualizacja: 2026-07-11 15:41 UTC

## Wersja i środowisko

- Repozytorium: `pawel-PV/ogniskowy-grajek`.
- Wydanie: `main` @ `6235259`, tag `v0.1.0-cpu`.
- PR #1: squash merged; check `ci` zaliczony w 4 min 1 s.
- Ochrona `main`: włączona; wymagane aktualne `ci`, liniowa historia i rozwiązane rozmowy.
- Aktywny profil: `CPU`, obrazy `ogniskowy-grajek-web:6235259` i
  `ogniskowy-grajek-worker-cpu:6235259`.
- Kandydat publikacji ścieżkowej: `agent/api-path-routing` @ `df76b02`.
- Lokalny URL po wdrożeniu zmiany: `http://127.0.0.1:8501/ogniskowy-grajek/`.
- Usługa `ogniskowy-grajek.service`: zainstalowana, włączona i aktywna.
- Publiczny URL: `https://api.klikfirma.pl/ogniskowy-grajek/` — oczekuje na regułę ścieżki tunelu;
  nowy rekord DNS nie jest potrzebny.

## Ukończone

- Osobna aplikacja, baza SQLite WAL, Compose, systemd i dokumentacja; `app_new` pozostało nietknięte.
- Streamlit z trwałym pollingiem statusu i osobny pojedynczy worker kolejki.
- Pipeline yt-dlp/FFmpeg, Demucs, Chordino/librosa oraz Ollama/Gemini/algorytm deterministyczny.
- Fallbacki audio i LLM, limit kosztu Gemini, cleanup w każdym stanie terminalnym oraz 24-godzinny cache.
- Publiczne limity kolejki/rate limit, walidacja URL i bezpieczne błędy.
- Obrazy `web`, `worker-ci`, `worker-cpu` i `worker-gpu`; profil CPU działa na NUC-u.

## Weryfikacja

- Ruff check i format: zaliczone.
- Pytest: `94 passed` na Pythonie 3.11.
- `docker compose config -q`, `git diff --check` i kompilacja modułów: zaliczone.
- Pełne obrazy web/CPU: zbudowane; natywny Chordino wykryty, syntetyczny smoke librosa: 117 BPM.
- Legalny realny E2E na materiale Creative Commons: yt-dlp → Demucs CPU → Chordino → librosa →
  wynik → cleanup: zaliczony.
- E2E przez wdrożoną kolejkę: `DONE` w 58 s, 144 BPM, 4/4, capo 5,
  `DEMUCS_CPU`/`CHORDINO`/`GEMINI`; koszt Gemini około `$0.00059`; workspace pusty po cleanupie.
- Smoke bazowej ścieżki: UI i health HTTP 200, przekierowanie prefiksu 307, WebSocket 101.
- Regresja usług: `api.klikfirma.pl/health` i `gra.klikfirma.pl` zwracają HTTP 200.

## Blokery zewnętrzne

- **GPU: BLOCKED/DEFERRED.** Razer Core X ma stan `disconnected`, brak urządzenia NVIDIA w PCI,
  `nvidia-smi` nie komunikuje się ze sterownikiem, a Ollama jest offline. Wymagany fizyczny power-cycle
  obudowy eGPU i kontrola kabla Thunderbolt. Pełny smoke CUDA/VRAM nie może zostać wykonany zdalnie.
- **Cloudflare: BLOCKED.** Bieżący token aplikacyjny zwraca `Not authorized` dla konfiguracji tunelu
  i nie ma `Cloudflare Tunnel Edit`. Potrzebna jest reguła dla hosta `api.klikfirma.pl` i ścieżki
  `^/ogniskowy-grajek(/.*)?$` przed ogólną trasą API; można dodać ją ręcznie w panelu albo użyć
  tymczasowego tokenu Tunnel Edit.

Watchdogi GPU/Ollama pozostają wyłączone po wcześniejszej pętli rebootów i nie są częścią tego wdrożenia.
Następny krok po uzyskaniu Tunnel Edit: backup konfiguracji, reguła ścieżki i test HTTPS/WebSocket
bez restartu `cloudflared`. Po powrocie użytkownika: fizyczny power-cycle i smoke CUDA.
