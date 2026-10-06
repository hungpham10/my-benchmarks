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
	$(COMPOSE) --profile ingest --profile bench --profile monitoring down -v

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

.PHONY: monitor
monitor: ## start the monitoring stack on top of the targets (Grafana on :3000)
	$(COMPOSE) --profile monitoring up -d cadvisor monitoring-prometheus \
	  postgres-exporter alloy grafana
	@echo
	@echo "  Grafana      http://localhost:$${GRAFANA_PORT:-3000}  (admin/admin)"
	@echo "  Alloy UI     http://localhost:$${ALLOY_PORT:-12345}"
	@echo
	@echo "  The monitoring Prometheus holds its own series and its own volume."
	@echo "  results/footprint.json still measures the Prometheus under test only."

.PHONY: monitor-cloud
monitor-cloud: ## same, plus a push to Grafana Cloud for a durable record
	@test -f monitoring/.env.monitoring || { \
	  echo "  copy monitoring/.env.monitoring.example to monitoring/.env.monitoring and fill it in"; \
	  exit 1; }
	ALLOY_CONFIG=config.cloud.alloy $(COMPOSE) --profile monitoring up -d \
	  cadvisor monitoring-prometheus postgres-exporter alloy grafana
	@echo
	@echo "  Grafana      http://localhost:$${GRAFANA_PORT:-3000}  (admin/admin)"
	@echo "  Alloy UI     http://localhost:$${ALLOY_PORT:-12345}"
	@echo
	@echo "  Pushing to Grafana Cloud as well. Check it arrived:"
	@echo "    docker logs bench-alloy 2>&1 | grep -i remote_write"
	@echo "  Run make monitor-check to confirm both destinations are receiving."

.PHONY: monitor-check
monitor-check: ## verify the monitoring stack is collecting what the dashboard needs
	$(PY) monitoring/check.py

.PHONY: clean-results
clean-results: ## delete measurements, keep the stack
	rm -rf $(RESULTS)
