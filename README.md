# Ogniskowy Grajek

Publiczne MVP dla początkujących gitarzystów. Użytkownik podaje pojedynczy link YouTube, a aplikacja
wyświetla BPM, metrum, pozycję kapodastra, jeden prosty schemat bicia, uporządkowane w czasie chwyty
oraz — gdy jakość źródła pozwala — statyczny śpiewnik z akordami nad przybliżonymi sylabami.

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

Interfejs lokalny: `http://127.0.0.1:8501/`. Adres publiczny:
`https://ogniskowy-grajek.klikfirma.pl/`. Cloudflare kieruje osobną trasę Published application
do lokalnego Streamlit; rekord CNAME jest zarządzany razem z trasą tunelu.

## Architektura

```text
Streamlit -> SQLite/WAL -> pojedynczy worker
                         -> yt-dlp + FFmpeg
                         -> Demucs CUDA | CPU | HPSS
                         -> Chordino | librosa chroma
                         -> oryginalne napisy YT | lokalny faster-whisper
                         -> Pyphen + wyrównanie akordów + ChordPro
                         -> Ollama | Gemini | deterministycznie
```

Audio, stem-y i robocze napisy są plikami tymczasowymi i nigdy nie są udostępniane. Tekst nie trafia
do Ollamy ani Gemini. Wynik JSON i ChordPro wygasają po 24 godzinach. Jeżeli tekstu brak, aplikacja
zachowuje dotychczasowy wynik akordów i pokazuje wyłącznie bezpieczny link wyszukiwania Ultimate
Guitar — backend nie pobiera stamtąd treści. Zobacz `SPECIFICATION.md`, `IMPLEMENTATION_STATUS.md`
i `ops/RUNTIME_HELPER.md`.
