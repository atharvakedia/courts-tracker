# All targets run out of the project venv. No global interpreter is used.
VENV    := $(CURDIR)/.venv
PY      := $(VENV)/bin/python
PYTEST  := $(VENV)/bin/pytest
RUFF    := $(VENV)/bin/ruff
MYPY    := $(VENV)/bin/mypy
UVICORN := $(VENV)/bin/uvicorn

# Everything the linter and formatter cover, named once so a new top-level
# module cannot quietly sit outside the gate. smoke_test.py is the script
# that recorded fixtures/raw/ against the live API; it ships with the repo,
# so it is held to the same standard.
SOURCES := tracker tests smoke_test.py
# mypy runs over the package only: tests and the one-off script are checked
# for style, not for strict typing.
TYPED   := tracker

.PHONY: install check ci-check fmt lint typecheck test run collect collect-dry discover clean

install:  ## create the venv and install the project with dev extras
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -e '.[dev]'

fmt:
	$(RUFF) check --fix $(SOURCES)
	$(RUFF) format $(SOURCES)

lint:
	$(RUFF) check $(SOURCES)
	$(RUFF) format --check $(SOURCES)

typecheck:
	$(MYPY) $(TYPED)

test:
	$(PYTEST) tests

# Fixing form: use before committing.
check:
	$(RUFF) check --fix $(SOURCES)
	$(RUFF) format $(SOURCES)
	$(MYPY) $(TYPED)
	$(PYTEST) tests

# Non-fixing form: what CI runs.
ci-check:
	$(RUFF) check $(SOURCES)
	$(RUFF) format --check $(SOURCES)
	$(MYPY) $(TYPED)
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
