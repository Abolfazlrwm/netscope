.PHONY: install test test-cov lint format typecheck tui clean build

# Editable install with every extra + dev tools
install:
	pip install -e ".[tui,dev]"

# Fully offline test suite (fake probes, no network)
test:
	pytest

test-cov:
	pytest --cov=netscope --cov-report=term-missing

lint:
	ruff check src tests scripts
	ruff format --check src tests

format:
	ruff check --fix src tests scripts
	ruff format src tests

typecheck:
	mypy

# Launch the dashboard locally
tui:
	python -m netscope.ui.cli tui

screenshots:
	python scripts/generate_screenshots.py

clean:
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} \;

build:
	python -m build
