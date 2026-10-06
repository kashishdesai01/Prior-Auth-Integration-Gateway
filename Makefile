UV_CACHE_DIR ?= $(CURDIR)/.runtime/uv-cache
export UV_CACHE_DIR
PY = .venv/bin/python
COMPOSE ?= docker compose

.PHONY: setup db up down demo test lint typecheck verify migrate openapi load
setup:
	uv sync --frozen
	$(PY) scripts/setup.py
db:
	$(COMPOSE) up -d postgres
up:
	$(PY) scripts/dev.py up
down:
	$(PY) scripts/dev.py down
migrate:
	$(PY) -m pagw.cli migrate
demo:
	$(PY) scripts/demo.py
test:
	.venv/bin/pytest --cov=pagw --cov-report=term-missing
lint:
	.venv/bin/ruff check src tests scripts
	.venv/bin/ruff format --check src tests scripts
typecheck:
	.venv/bin/mypy
verify: lint typecheck test
openapi:
	$(PY) -m pagw.cli openapi > docs/openapi.json
load:
	$(PY) scripts/load.py
