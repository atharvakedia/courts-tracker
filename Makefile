# All targets run out of the project venv. No global interpreter is used.
VENV    := $(CURDIR)/.venv
PY      := $(VENV)/bin/python
PYTEST  := $(VENV)/bin/pytest
RUFF    := $(VENV)/bin/ruff
MYPY    := $(VENV)/bin/mypy
UVICORN := $(VENV)/bin/uvicorn

.PHONY: install check ci-check fmt lint typecheck test run collect collect-dry discover clean

install:  ## create the venv and install the project with dev extras
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -e '.[dev]'

fmt:
	$(RUFF) check --fix tracker tests
	$(RUFF) format tracker tests

lint:
	$(RUFF) check tracker tests
	$(RUFF) format --check tracker tests

typecheck:
	$(MYPY) tracker

test:
	$(PYTEST) tests

# Fixing form: use before committing.
check:
	$(RUFF) check --fix tracker tests
	$(RUFF) format tracker tests
	$(MYPY) tracker
	$(PYTEST) tests

# Non-fixing form: what CI runs.
ci-check:
	$(RUFF) check tracker tests
	$(RUFF) format --check tracker tests
	$(MYPY) tracker
	$(PYTEST) tests

run:
	$(UVICORN) tracker.web:app --host 127.0.0.1 --port 8000 --reload

collect:
	$(PY) -m tracker collect

collect-dry:
	$(PY) -m tracker collect --dry-run

discover:
	$(PY) -m tracker discover

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
