PYTHON ?= .venv/bin/python
# Explicit public paths avoid traversing local credentials, state, or AGENTS.md.
MARKDOWN = README.md CONTRIBUTING.md CHANGELOG.md \
	docs/SHORTCUTS.md docs/VERIFICATION.md docs/OPERATIONS.md \
	docs/ARCHITECTURE.md docs/SOURCES.md

.PHONY: setup dev format check test

setup:
	bash scripts/setup.sh

dev:
	$(PYTHON) -m pip install -r requirements.txt -r requirements-dev.txt

format:
	$(PYTHON) -m ruff check --fix .
	$(PYTHON) -m ruff format .
	$(PYTHON) -m mdformat $(MARKDOWN)

check:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .
	$(PYTHON) -m mdformat --check $(MARKDOWN)
	bash -n scripts/setup.sh health-garmin-sync
	git diff --check

test:
	$(PYTHON) -c 'from importlib.metadata import version; import garminconnect, requests, urllib3, curl_cffi; assert version("garminconnect") == "0.3.16", "Install the reviewed Garmin SDK before running tests"'
	$(PYTHON) -m unittest discover -s tests -v
