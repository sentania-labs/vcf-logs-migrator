.PHONY: check lint test

PYTHON ?= python3

check: lint test

lint:
	ruff check src tests
	ruff format --check src tests

test:
	$(PYTHON) -m pytest
