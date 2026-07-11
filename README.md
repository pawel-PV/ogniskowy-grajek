# Ogniskowy Grajek

Publiczne MVP dla początkujących gitarzystów. Użytkownik podaje pojedynczy link YouTube, a aplikacja
wyświetla BPM, metrum, pozycję kapodastra, jeden prosty schemat bicia oraz uporządkowane w czasie
chwyty gitarowe.

## Aktualny tryb

eGPU Razer Core X jest fizycznie odłączone od magistrali Thunderbolt. Profil produkcyjny działa więc
na CPU. Aplikacja nie uruchamia ani nie naprawia istniejących watchdogów GPU/Ollama.

```bash
cp .env.example .env
secret="$(openssl rand -hex 32)"
sed -i "s/^RATE_LIMIT_SECRET=.*/RATE_LIMIT_SECRET=${secret}/" .env
unset secret
docker compose build web worker
docker compose up -d --no-build --wait
curl --fail http://127.0.0.1:8501/_stcore/health
```

Interfejs lokalny: `http://127.0.0.1:8501`. Docelowy adres publiczny:
`https://ogniskowy-grajek.klikfirma.pl`.

## Architektura

```text
Streamlit -> SQLite/WAL -> pojedynczy worker
                         -> yt-dlp + FFmpeg
                         -> Demucs CUDA | CPU | HPSS
                         -> Chordino | librosa chroma
                         -> Ollama | Gemini | deterministycznie
```

Audio i stem-y są plikami tymczasowymi i nigdy nie są udostępniane. Wynik JSON wygasa po 24 godzinach.
Zobacz `SPECIFICATION.md`, `IMPLEMENTATION_STATUS.md` i `ops/RUNTIME_HELPER.md`.
