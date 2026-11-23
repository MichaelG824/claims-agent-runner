PY ?= python3.12
VENV := .venv
BIN := $(VENV)/bin

.PHONY: bootstrap test db-up db-down db-reset

bootstrap:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -e ".[dev]"

test:
	$(BIN)/pytest -q

db-up:
	docker compose up -d --wait

db-down:
	docker compose down

db-reset:
	docker compose down -v
	docker compose up -d --wait
