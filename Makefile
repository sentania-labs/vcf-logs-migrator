.PHONY: check lint test smoke

PYTHON ?= python3

check: lint test

lint:
	ruff check src tests
	ruff format --check src tests

test:
	$(PYTHON) -m pytest

smoke:
	$(PYTHON) tests/smoke_installed.py
