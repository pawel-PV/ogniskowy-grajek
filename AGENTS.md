# Zasady pracy agentów

- To osobne repozytorium. Nie importuj kodu z `../app_new` i nie modyfikuj jego plików.
- Nigdy nie commituj `.env`, kluczy API, tokenów Cloudflare, cookies ani plików audio.
- Ciężkie przetwarzanie wykonuje worker, nie wątek Streamlit.
- Nie uruchamiaj automatycznych rebootów, restartu Dockera ani watchdogów GPU.
- Każda zmiana kontraktu wyniku wymaga zwiększenia `schema_version` oraz testu kompatybilności.
- Przed wdrożeniem wymagane są: `ruff check .`, `pytest`, `docker compose config -q` i lokalny health smoke.
