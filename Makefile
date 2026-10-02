PY := .venv/bin/python

.PHONY: help setup test lint check dry-run run assistant tunnel clean reset-data

help:            ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

setup:           ## create the virtualenv and install dependencies
	python3 -m venv .venv
	$(PY) -m pip install -q -r requirements-dev.txt

test:            ## run the test suite
	$(PY) -m pytest

lint:            ## static checks
	.venv/bin/ruff check .

check: lint test ## what CI runs

dry-run:         ## replay scripted calls through the webhook (no account needed)
	$(PY) -m scripts.dry_run --reset

run:             ## start the server and dashboard on :8000
	.venv/bin/uvicorn app.server:app --port 8000

assistant:       ## create or update the Vapi assistant from app/prompt.py + app/tools.py
	$(PY) -m scripts.setup_assistant

tunnel:          ## expose :8000 so Vapi can reach the tool webhooks
	ngrok http 8000

clean:           ## remove caches (keeps your call history)
	rm -rf .pytest_cache .ruff_cache
	find . -name __pycache__ -not -path './.venv/*' -prune -exec rm -rf {} +

reset-data:      ## delete the local database: all call history and customer state
	rm -f data/state.db
