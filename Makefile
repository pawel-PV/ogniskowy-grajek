.PHONY: test lint check build-cpu up-cpu down logs smoke doctor

test:
	pytest

lint:
	ruff check .

check: lint test

build-cpu:
	docker compose build web worker

up-cpu:
	docker compose up -d --no-build --wait

down:
	docker compose stop --timeout 120

logs:
	docker compose logs -f --tail=200

smoke:
	curl --fail --silent http://127.0.0.1:8501/_stcore/health

doctor:
	docker compose exec worker python -m ogniskowy_grajek.worker --doctor
