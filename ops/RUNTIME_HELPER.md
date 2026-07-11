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
curl --fail http://127.0.0.1:8501/_stcore/health
docker compose exec worker python -m ogniskowy_grajek.worker --doctor
docker compose exec worker python -m ogniskowy_grajek.worker --audio-smoke
docker compose exec worker python -m ogniskowy_grajek.worker --asr-smoke
```

`--asr-smoke` ładuje lokalny model medium i jest poleceniem wdrożeniowym, nie healthcheckiem. Pełny
model jest przypięty rewizją w Dockerfile i powiększa obraz o około 1,5 GB. Jednostkę systemd
instalować dopiero po udanym smoke. Nie wykonuje builda przy starcie hosta.

Przed przełączeniem workera sprawdzić, że liczba jobów `PENDING/RUNNING` wynosi zero. Po wdrożeniu
wykonać legalny test napisów oraz lokalnego ASR, pobrać JSON/ChordPro i potwierdzić pusty `data/work`.
Audio, wokal i robocze `json3` muszą zniknąć również po błędzie i anulowaniu.

## Cloudflare

Tunel `BetaNode` jest zdalnie zarządzany. Trasa Published application:

```text
hostname: ogniskowy-grajek.klikfirma.pl
path: puste
service: http://localhost:8501
DNS: CNAME -> d17f5d12-3fdf-443c-9aad-4185c1763157.cfargotunnel.com
```

Cloudflare utworzył rekord DNS automatycznie razem z trasą. Zachować trasy `api.klikfirma.pl -> 8000`,
`gra.klikfirma.pl -> 8080` i końcowy 404. Nie restartować `cloudflared` i nie rotować connector tokenu.
Po wdrożeniu sprawdzić HTTPS, WebSocket Streamlit, `api.klikfirma.pl/health` i `gra.klikfirma.pl`.

## Aktywacja GPU po powrocie sprzętu

1. Wyłączyć Razer Core X na minimum 30 sekund, sprawdzić zasilanie i kabel Thunderbolt.
2. Wymagać kolejno: `boltctl`, NVIDIA w `lspci`, działający `nvidia-smi`.
3. Wygenerować CDI i sprawdzić `nvidia-ctk cdi list`.
4. Zbudować target GPU i wykonać krótki smoke Demucs na CUDA.
5. Dopiero wtedy odtworzyć worker z gotowego obrazu:
   `docker compose -f compose.yml -f compose.gpu.yml up -d --no-build --force-recreate worker`.

Nie włączać starych watchdogów GPU/Ollama bez osobnej naprawy rate limitu rebootów.

## Rollback

- Publikacja: usunąć tylko trasę `ogniskowy-grajek.klikfirma.pl` i jej CNAME, potem zatrzymać usługę.
- Aplikacja: przywrócić poprzedni tag obrazu v0.1 i wykonać Compose `up`; migracja SQLite nie jest
  wymagana, a wyniki schematu 2.0 pozostaną danymi wygasającymi; nie używać `git reset`.
- GPU: odtworzyć worker z samego `compose.yml`, co przywraca bezpieczny profil CPU.
