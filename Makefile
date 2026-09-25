PYTHON ?= python3
VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: bootstrap api web fixtures test test-python test-web lint lint-python lint-web build build-python build-web check release-gate release-audit qa-accessibility qa-calibration qa-first-run qa-output qa-recovery qa-mural qa-mural-performance qa-selection qa-local-edits qa-replacement-lifecycle qa-replacement-scroll macos-install macos-uninstall clean

bootstrap:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e '.[dev]'
	cd frontend && npm ci

api:
	$(PY) -m uvicorn image23mf.api.app:app --reload --host 127.0.0.1 --port 8323

web:
	cd frontend && npm run dev

fixtures:
	$(PY) scripts/generate_synthetic_fixtures.py

test: test-python test-web

test-python:
	$(PY) -m pytest

test-web:
	cd frontend && npm test -- --run

lint: lint-python lint-web

lint-python:
	$(PY) -m ruff check src tests scripts benchmarks
	$(PY) -m ruff format --check src tests scripts benchmarks

lint-web:
	cd frontend && npm run lint
	cd frontend && npm run typecheck

build: build-python build-web

build-python:
	$(PY) -m build

build-web:
	cd frontend && npm run build

check: lint test build

release-gate:
	$(PY) scripts/release_gate.py

release-audit:
	$(PY) scripts/release_gate.py --audit

qa-accessibility:
	IMAGE23MF_DEVTOOLS_URL=$${IMAGE23MF_DEVTOOLS_URL:-http://127.0.0.1:9223} \
	IMAGE23MF_WEB_URL=$${IMAGE23MF_WEB_URL:-http://127.0.0.1:5173} \
	node scripts/accessibility_qa.mjs

qa-calibration:
	$(PY) scripts/calibration_review_browser_qa.py

qa-replacement-scroll:
	IMAGE23MF_DEVTOOLS_URL=$${IMAGE23MF_DEVTOOLS_URL:-http://127.0.0.1:9223} \
	IMAGE23MF_WEB_URL=$${IMAGE23MF_WEB_URL:-http://127.0.0.1:5173} \
	IMAGE23MF_QA_REPLACEMENT_ONLY=1 \
	node scripts/live_editor_qa.mjs

qa-replacement-lifecycle:
	IMAGE23MF_DEVTOOLS_URL=$${IMAGE23MF_DEVTOOLS_URL:-http://127.0.0.1:9223} \
	IMAGE23MF_WEB_URL=$${IMAGE23MF_WEB_URL:-http://127.0.0.1:5173} \
	node scripts/replacement_revision_browser_qa.mjs

qa-first-run:
	IMAGE23MF_DEVTOOLS_URL=$${IMAGE23MF_DEVTOOLS_URL:-http://127.0.0.1:9223} \
	IMAGE23MF_WEB_URL=$${IMAGE23MF_WEB_URL:-http://127.0.0.1:5173} \
	node scripts/first_run_browser_qa.mjs

qa-output:
	IMAGE23MF_DEVTOOLS_URL=$${IMAGE23MF_DEVTOOLS_URL:-http://127.0.0.1:9223} \
	IMAGE23MF_WEB_URL=$${IMAGE23MF_WEB_URL:-http://127.0.0.1:5173} \
	IMAGE23MF_QA_OUTPUT_ONLY=1 \
	node scripts/live_editor_qa.mjs

qa-recovery:
	$(PY) scripts/recovery_browser_qa.py

qa-mural:
	IMAGE23MF_DEVTOOLS_URL=$${IMAGE23MF_DEVTOOLS_URL:-http://127.0.0.1:9223} \
	node scripts/mural_browser_qa.mjs

qa-mural-performance:
	$(PY) benchmarks/run_mural_partition.py \
		--run-id mural-local \
		--git-commit $$(git rev-parse HEAD) \
		--output-dir workspace/qa/mural-performance \
		--verify-against benchmarks/mural-partition-budgets.json

qa-selection:
	IMAGE23MF_DEVTOOLS_URL=$${IMAGE23MF_DEVTOOLS_URL:-http://127.0.0.1:9223} \
	IMAGE23MF_WEB_URL=$${IMAGE23MF_WEB_URL:-http://127.0.0.1:5173} \
	node scripts/selection_browser_qa.mjs

qa-local-edits:
	IMAGE23MF_DEVTOOLS_URL=$${IMAGE23MF_DEVTOOLS_URL:-http://127.0.0.1:9223} \
	IMAGE23MF_WEB_URL=$${IMAGE23MF_WEB_URL:-http://127.0.0.1:5173} \
	node scripts/local_edit_browser_qa.mjs

macos-install:
	./packaging/macos/install.command

macos-uninstall:
	./packaging/macos/uninstall.command

clean:
	rm -rf build dist .pytest_cache .ruff_cache frontend/dist frontend/coverage
