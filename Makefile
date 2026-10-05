# Prometheus vs PostgreSQL metrics benchmark.
#
# `make bench` runs the whole thing and writes results/report.md. Every phase
# is separately runnable because they are separately diagnosable: when a run
# goes wrong, the question is always which of ingest/footprint/query broke.

PY      ?= python3
COMPOSE ?= $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo docker-compose)
RESULTS := results

.DEFAULT_GOAL := help

.PHONY: help
help: ## show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

.PHONY: up
up: ## start Prometheus and PostgreSQL, wait for both to be healthy
	$(PY) bench/run.py up

.PHONY: down
down: ## stop everything and delete the data volumes
	$(COMPOSE) --profile ingest --profile bench down -v

.PHONY: build
build: ## build the bench image (needs network for pip)
	$(COMPOSE) --profile bench build bench

.PHONY: ingest
ingest: ## load the dataset into both targets, one after the other
	$(PY) bench/run.py ingest

.PHONY: footprint
footprint: ## measure settled on-disk bytes, normalised per sample
	$(PY) bench/run.py footprint

.PHONY: query
query: ## run the latency suite and sample CPU/memory while it runs
	$(PY) bench/run.py query

.PHONY: parity
parity: ## confirm both targets hold the same data
	$(PY) bench/run.py parity

.PHONY: report
report: ## render $(RESULTS)/report.md
	$(PY) bench/run.py report

.PHONY: bench
bench: ## everything, in order
	$(PY) bench/run.py all

.PHONY: clean-results
clean-results: ## delete measurements, keep the stack
	rm -rf $(RESULTS)
