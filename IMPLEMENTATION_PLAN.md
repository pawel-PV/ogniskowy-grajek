# Plan realizacji

- [x] Audyt repo, NUC-a, GPU, Ollamy i tunelu Cloudflare.
- [x] Osobne repo i gałąź `feat/mvp-cpu-deploy`.
- [x] Kontrakty danych, kolejka SQLite i publiczne limity.
- [x] Pipeline yt-dlp/Demucs/Chordino/librosa/LLM.
- [x] Streamlit UI z pollingiem statusu.
- [x] Testy jednostkowe i integracyjne.
- [x] Obrazy CPU/GPU, Compose i systemd.
- [x] Lokalny CPU smoke na legalnej próbce.
- [x] Instalacja i uruchomienie `ogniskowy-grajek.service` bez restartu Dockera.
- [x] Publikacja przez istniejący Cloudflare Tunnel.
- [x] PR, zielone CI, squash merge i tag `v0.1.0-cpu`.
- [x] Kontrakt `AnalysisResult 2.0` i zgodność odczytu cache `1.0` w UI.
- [x] Oryginalne napisy YouTube `json3` z limitem 2 MB i bez automatycznych tłumaczeń.
- [x] Lokalny `faster-whisper medium` z timeoutem oraz bramką jakości PL/EN.
- [x] Sylabizacja Pyphen, wyrównanie akordów, linie instrumentalne i eksport ChordPro.
- [x] Bezpieczny fallback do samych akordów i linku wyszukiwania Ultimate Guitar.
- [x] Rozszerzone oświadczenie o prawach i retencja tekstu przez 24 godziny.
- [x] Build pełnego obrazu worker v0.2 i smoke modelu ASR na NUC-u.
- [x] Publiczny E2E v0.2, PR, zielone CI, squash merge i tag `v0.2.0-cpu`.
- [ ] Po fizycznym power-cycle: aktywacja i smoke CUDA.

Trasa Published application i CNAME dla `ogniskowy-grajek.klikfirma.pl` zostały utworzone ręcznie
w panelu Cloudflare. GPU pozostaje odroczone do fizycznego podłączenia eGPU.
