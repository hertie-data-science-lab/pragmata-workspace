# pragmata-workspace — operational entrypoint over scripts/.
# Run `make` or `make help` for the target list. Scripts remain runnable
# directly; these targets just document the pipeline and wire up args.
#
# Pipeline order:  querygen -> bot -> combine -> import
#
# Orchestrated (scripts/pipeline.sh) — runs a contiguous slice over a filter:
#   make pipeline                      # full pipeline, all domains
#   make pipeline TO=bot               # querygen + bot
#   make pipeline FROM=combine         # combine + import
#   make pipeline ONLY=bot FILTER=gesundheit JOBS=8
#   make plan TO=bot                   # preview a slice without running
#
# Single stages (call the stage scripts directly):
#   make querygen SPECS=demokratie-und-zusammenhalt,europas-zukunft
#   make bot SPEC=gesundheit
#   make combine DOMAINS="gesundheit europas-zukunft"
#   make import DOMAIN=gesundheit

SHELL := /bin/bash
PY := .venv/bin/python

# Pass-through flags for pipeline.sh / plan, built from make vars.
PIPELINE_ARGS := $(if $(ONLY),--only $(ONLY),) $(if $(FROM),--from $(FROM),) \
                 $(if $(TO),--to $(TO),) $(if $(FILTER),--filter $(FILTER),) \
                 $(if $(JOBS),--jobs $(JOBS),)

.DEFAULT_GOAL := help
.PHONY: help pipeline plan querygen bot combine import probe

help: ## Show this help
	@awk 'BEGIN{FS=":.*## "} /^[a-zA-Z_-]+:.*## /{printf "  \033[36m%-10s\033[0m %s\n",$$1,$$2}' $(MAKEFILE_LIST)

pipeline: ## Run a pipeline slice (FROM= TO= ONLY= FILTER= JOBS=); no args = full
	bash scripts/pipeline.sh $(PIPELINE_ARGS)

plan: ## Preview a pipeline slice without running (same vars as pipeline)
	bash scripts/pipeline.sh --dry-run $(PIPELINE_ARGS)

querygen: ## Stage: generate synthetic queries (SPECS=a,b to filter)
	bash scripts/run_querygen.sh "$(SPECS)"

bot: ## Stage: run publikationsbot over generated queries (SPEC=x to filter)
	$(PY) scripts/run_bot.py $(if $(SPEC),--spec $(SPEC),)

combine: ## Stage: pool runs + intersperse edgecases (DOMAINS="a b" to filter)
	$(PY) scripts/build_combined.py $(DOMAINS)

import: ## Stage: setup + import one domain (requires DOMAIN=)
	@test -n "$(DOMAIN)" || { echo "usage: make import DOMAIN=<domain>"; exit 2; }
	bash scripts/setup_and_import.sh "$(DOMAIN)"

probe: ## Single-query bot probe, no JSONL write (SPEC=x optional)
	$(PY) scripts/run_bot.py --probe $(if $(SPEC),--spec $(SPEC),)
