PY ?= python3.12
VENV := .venv
BIN := $(VENV)/bin

.PHONY: bootstrap test lint run check db-up db-down db-reset

bootstrap:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -e ".[dev]"

test:
	$(BIN)/pytest -q

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	$(BIN)/mypy

check: lint test

run:
	$(BIN)/python -m app.runner $(ARGS)

db-up:
	docker compose up -d --wait

db-down:
	docker compose down

db-reset:
	docker compose down -v
	docker compose up -d --wait
