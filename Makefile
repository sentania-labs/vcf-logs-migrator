.PHONY: check lint test smoke

PYTHON ?= python3

check: lint test

lint:
	ruff check src tests packaging
	ruff format --check src tests packaging

test:
	$(PYTHON) -m pytest

smoke:
	$(PYTHON) tests/smoke_installed.py
