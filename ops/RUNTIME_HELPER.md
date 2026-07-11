# Runbook produkcyjny

## Profil CPU

```bash
cd /home/wewenek/ai-home/ogniskowy-grajek
cp .env.example .env
chmod 600 .env
secret="$(openssl rand -hex 32)"
sed -i "s/^RATE_LIMIT_SECRET=.*/RATE_LIMIT_SECRET=${secret}/" .env
sed -i "s/^APP_VERSION=.*/APP_VERSION=$(git rev-parse --short=12 HEAD)/" .env
unset secret
sudo install -d -o wewenek -g nogroup -m 0770 data data/work
docker compose build web worker
docker compose up -d --no-build --wait
curl --fail http://127.0.0.1:8501/ogniskowy-grajek/_stcore/health
docker compose exec worker python -m ogniskowy_grajek.worker --doctor
docker compose exec worker python -m ogniskowy_grajek.worker --audio-smoke
```

Jednostkę systemd instalować dopiero po udanym smoke. Nie wykonuje builda przy starcie hosta.

## Cloudflare

Tunel jest zdalnie zarządzany. Po lokalnym smoke dodać przed ogólną trasą hosta `api` regułę:

```text
hostname: api.klikfirma.pl
path: ^/ogniskowy-grajek(/.*)?$
service: http://localhost:8501
```

Nie tworzyć nowego DNS — `api.klikfirma.pl` już wskazuje na tunel. Zachować późniejszą ogólną trasę
`api.klikfirma.pl -> 8000`, trasę `gra.klikfirma.pl -> 8080` i końcowy 404. Nie restartować
`cloudflared` i nie rotować connector tokenu. Po zmianie sprawdzić HTTPS, WebSocket Streamlit,
`api.klikfirma.pl/health` oraz `gra.klikfirma.pl`.

## Aktywacja GPU po powrocie sprzętu

1. Wyłączyć Razer Core X na minimum 30 sekund, sprawdzić zasilanie i kabel Thunderbolt.
2. Wymagać kolejno: `boltctl`, NVIDIA w `lspci`, działający `nvidia-smi`.
3. Wygenerować CDI i sprawdzić `nvidia-ctk cdi list`.
4. Zbudować target GPU i wykonać krótki smoke Demucs na CUDA.
5. Dopiero wtedy odtworzyć worker z gotowego obrazu:
   `docker compose -f compose.yml -f compose.gpu.yml up -d --no-build --force-recreate worker`.

Nie włączać starych watchdogów GPU/Ollama bez osobnej naprawy rate limitu rebootów.

## Rollback

- Publikacja: usunąć tylko regułę ścieżki `/ogniskowy-grajek`, potem zatrzymać nową usługę.
- Aplikacja: przywrócić poprzedni tag obrazu i wykonać Compose `up`; nie używać `git reset`.
- GPU: odtworzyć worker z samego `compose.yml`, co przywraca bezpieczny profil CPU.
