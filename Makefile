.PHONY: install test lint fmt up down logs migrate revision psql

install:
	cd backend && pip install -e ".[dev]"

test:
	cd backend && python -m pytest -q

lint:
	cd backend && ruff check app workers tests

fmt:
	cd backend && ruff check --fix app workers tests && ruff format app workers tests

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

migrate:
	docker compose run --rm migrate

revision:
	cd backend && alembic revision --autogenerate -m "$(m)"

psql:
	docker compose exec db psql -U copytrader -d copytrader
