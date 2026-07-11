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
- [ ] Publikacja przez istniejący Cloudflare Tunnel.
- [x] PR, zielone CI, squash merge i tag `v0.1.0-cpu`.
- [ ] Po fizycznym power-cycle: aktywacja i smoke CUDA.

Publikacja Cloudflare pod istniejącym `api.klikfirma.pl/ogniskowy-grajek` jest zablokowana wyłącznie
brakiem uprawnienia `Cloudflare Tunnel Edit`; nowy DNS nie jest potrzebny. GPU pozostaje odroczone
do fizycznego podłączenia eGPU.
