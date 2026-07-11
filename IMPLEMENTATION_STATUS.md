# Stan realizacji

Aktualizacja: 2026-07-11 11:01 UTC

## Wersja i środowisko

- Repozytorium: `pawel-PV/ogniskowy-grajek`.
- Gałąź: `feat/mvp-cpu-deploy`.
- SHA bazowy: `4e08b81`; SHA implementacji zostanie wpisany po pierwszym commicie.
- Aktywny profil: `CPU`, obrazy `ogniskowy-grajek-web:dev` i
  `ogniskowy-grajek-worker-cpu:dev`.
- Lokalny URL: `http://127.0.0.1:8501` (`/_stcore/health` zwraca `ok`).
- Usługa `ogniskowy-grajek.service`: zainstalowana, włączona i aktywna.
- Publiczny URL: `https://ogniskowy-grajek.klikfirma.pl` — oczekuje na trasę i DNS Cloudflare.

## Ukończone

- Osobna aplikacja, baza SQLite WAL, Compose, systemd i dokumentacja; `app_new` pozostało nietknięte.
- Streamlit z trwałym pollingiem statusu i osobny pojedynczy worker kolejki.
- Pipeline yt-dlp/FFmpeg, Demucs, Chordino/librosa oraz Ollama/Gemini/algorytm deterministyczny.
- Fallbacki audio i LLM, limit kosztu Gemini, cleanup w każdym stanie terminalnym oraz 24-godzinny cache.
- Publiczne limity kolejki/rate limit, walidacja URL i bezpieczne błędy.
- Obrazy `web`, `worker-ci`, `worker-cpu` i `worker-gpu`; profil CPU działa na NUC-u.

## Weryfikacja

- Ruff check i format: zaliczone.
- Pytest: `93 passed` na Pythonie 3.11.
- `docker compose config -q`, `git diff --check` i kompilacja modułów: zaliczone.
- Pełne obrazy web/CPU: zbudowane; natywny Chordino wykryty, syntetyczny smoke librosa: 117 BPM.
- Legalny realny E2E na materiale Creative Commons: yt-dlp → Demucs CPU → Chordino → librosa →
  wynik → cleanup: zaliczony.
- E2E przez wdrożoną kolejkę: `DONE` w 58 s, 144 BPM, 4/4, capo 5,
  `DEMUCS_CPU`/`CHORDINO`/`GEMINI`; koszt Gemini około `$0.00059`; workspace pusty po cleanupie.
- Regresja usług: `api.klikfirma.pl/health` i `gra.klikfirma.pl` zwracają HTTP 200.

## Blokery zewnętrzne

- **GPU: BLOCKED/DEFERRED.** Razer Core X ma stan `disconnected`, brak urządzenia NVIDIA w PCI,
  `nvidia-smi` nie komunikuje się ze sterownikiem, a Ollama jest offline. Wymagany fizyczny power-cycle
  obudowy eGPU i kontrola kabla Thunderbolt. Pełny smoke CUDA/VRAM nie może zostać wykonany zdalnie.
- **Cloudflare: BLOCKED.** Bieżący token aplikacyjny zwraca `Not authorized` dla konfiguracji tunelu
  i nie ma `Cloudflare Tunnel Edit`. Brak jeszcze DNS dla publicznego URL. Potrzebny jest tymczasowy
  token z `Cloudflare Tunnel Edit` i `DNS Edit`; nie może trafić do repo ani `.env`.

Watchdogi GPU/Ollama pozostają wyłączone po wcześniejszej pętli rebootów i nie są częścią tego wdrożenia.
Następny krok: PR/CI/merge/tag, a po dostarczeniu tokenu — backup konfiguracji tunelu, nowa trasa/DNS
i test HTTPS/WebSocket bez restartu `cloudflared`.
