PY ?= python3
SNAPSHOT ?= data/sops

.PHONY: install test run ingest update eval eval-gemini clean

install:            ## install runtime + test dependencies
	$(PY) -m pip install -r requirements-dev.txt

test:               ## offline test suite (no network, no API key)
	$(PY) -m pytest -q

ingest:             ## index a snapshot folder: make ingest SNAPSHOT=data/sop_updates/2026-11-01
	$(PY) -m app.cli ingest $(SNAPSHOT)

update:             ## apply the 2026-11-01 snapshot
	$(PY) -m app.cli ingest data/sop_updates/2026-11-01

run:                ## index the original library (no-op if already indexed) and start the API on :8000
	$(PY) -m app.cli ingest data/sops || true
	$(PY) -m uvicorn app.api.main:app --host 0.0.0.0 --port 8000

eval:               ## full evaluation with the provider in .env (default: offline fake)
	$(PY) -m eval.run_eval

eval-gemini:       ## real model; EVAL_SLEEP_S paces calls for free-tier rate limits
	LLM_PROVIDER=gemini EVAL_SLEEP_S=$${EVAL_SLEEP_S:-7} $(PY) -m eval.run_eval

clean:
	rm -rf var .pytest_cache
