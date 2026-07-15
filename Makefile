# pragmata-workspace — operational entrypoint over scripts/.
# Run `make` or `make help` for the target list. Scripts remain runnable
# directly; these targets just document the pipeline and wire up args.
#
# Pipeline order:  querygen -> bot -> combine -> setup -> import
#
# Orchestrated (scripts/pipeline.sh) — runs a contiguous slice over a filter:
#   make pipeline                      # full pipeline, all domains
#   make pipeline TO=bot               # querygen + bot
#   make pipeline FROM=combine         # combine + setup + import
#   make pipeline ONLY=bot FILTER=gesundheit JOBS=8
#   make plan TO=bot                   # preview a slice without running
#
# Single stages (call the stage scripts directly):
#   make querygen SPECS=demokratie-und-zusammenhalt,europas-zukunft
#   make bot SPEC=gesundheit
#   make combine DOMAINS="gesundheit europas-zukunft"
#   make setup DOMAIN=gesundheit
#   make import DOMAIN=gesundheit

SHELL := /bin/bash
PY := .venv/bin/python

# Reproducibility bundles (dated, one per operation).
IMPORT := reproducibility/2026-05-initial-import
CURATION := reproducibility/2026-07-01-annotation-curation

# Pass-through flags for pipeline.sh / plan, built from make vars.
PIPELINE_ARGS := $(if $(ONLY),--only $(ONLY),) $(if $(FROM),--from $(FROM),) \
                 $(if $(TO),--to $(TO),) $(if $(FILTER),--filter $(FILTER),) \
                 $(if $(JOBS),--jobs $(JOBS),)

.DEFAULT_GOAL := help
.PHONY: help pipeline plan querygen bot combine setup import probe log export report report-tables report-pdf plots daily backup reproduce-curation eval-push eval-pull eval-verify

help: ## Show this help
	@awk 'BEGIN{FS=":.*## "} /^[a-zA-Z_-]+:.*## /{printf "  \033[36m%-10s\033[0m %s\n",$$1,$$2}' $(MAKEFILE_LIST)

pipeline: ## Run a pipeline slice (FROM= TO= ONLY= FILTER= JOBS=); no args = full
	bash scripts/pipeline.sh $(PIPELINE_ARGS)

querygen: ## Stage: generate synthetic queries (SPECS=a,b to filter)
	bash scripts/annotation/run_querygen.sh "$(SPECS)"

bot: ## Stage: run publikationsbot over generated queries (SPEC=x to filter)
	$(PY) scripts/annotation/run_bot.py $(if $(SPEC),--spec $(SPEC),)

combine: ## Stage: pool runs + intersperse edgecases (DOMAINS="a b" to filter)
	$(PY) scripts/annotation/build_combined.py $(DOMAINS)

setup: ## Stage: provision Argilla workspaces + users for one domain (DOMAIN=)
	@test -n "$(DOMAIN)" || { echo "usage: make setup DOMAIN=<domain>"; exit 2; }
	bash scripts/annotation/setup.sh "$(DOMAIN)"

import: ## Stage: import one domain's combined JSONL (DOMAIN=)
	@test -n "$(DOMAIN)" || { echo "usage: make import DOMAIN=<domain>"; exit 2; }
	bash scripts/annotation/import.sh "$(DOMAIN)"

log: ## Log an annotation snapshot -> logs/annotation/log.jsonl (--summary for a CLI table)
	$(PY) scripts/annotation/log.py $(if $(DOMAIN),--domain $(DOMAIN),)

export: ## Export current annotations to per-task CSVs (DOMAIN= to filter, default all)
	bash scripts/annotation/export.sh $(DOMAIN)

report: ## Render latest snapshot -> reports/annotation/<date>/ (report.md + plots, +_latest)
	$(PY) scripts/annotation/report_tables.py
	$(PY) scripts/annotation/plot_summary.py

report-tables: ## Render tables only -> reports/annotation/<date>/report.md
	$(PY) scripts/annotation/report_tables.py

report-pdf: ## Render latest snapshot tables -> reports/annotation/<date>/report.pdf (needs pandoc + xelatex)
	@md=$$($(PY) scripts/annotation/report_tables.py 2>&1 | sed -n 's/^wrote //p'); \
	pandoc "$$md" -o "$${md%.md}.pdf" --pdf-engine=xelatex -V fontsize=9pt \
	  -V geometry:margin=1.5cm -V mainfont="DejaVu Serif" -V monofont="DejaVu Sans Mono" \
	  && echo "wrote $${md%.md}.pdf"

plots: ## Render plots only (PNGs) -> reports/annotation/<date>/ (needs matplotlib)
	$(PY) scripts/annotation/plot_summary.py

daily: ## Nightly logging: export -> log.jsonl (reporting is manual: make report)
	bash scripts/daily.sh

backup: ## Status-preserving Argilla backup (make backup; ARGS="restore <dir>" to restore)
	$(PY) scripts/annotation/argilla_backup.py $(if $(ARGS),$(ARGS),dump)

eval-push: ## Push a tree to the eval Blob (SRC= source tree, PREFIX= dest prefix; both required)
	@test -n "$(SRC)" && test -n "$(PREFIX)" || { echo "usage: make eval-push SRC=<tree> PREFIX=<prefix>"; exit 2; }
	bash scripts/eval/sync.sh push "$(SRC)" "$(PREFIX)"

eval-pull: ## Pull a Blob prefix into data/transfer/<prefix>/ + verify (PREFIX= required)
	@test -n "$(PREFIX)" || { echo "usage: make eval-pull PREFIX=<prefix>"; exit 2; }
	bash scripts/eval/sync.sh pull $(PREFIX)

eval-verify: ## Re-verify an already-pulled tree against its manifest (PREFIX= under data/transfer/)
	@test -n "$(PREFIX)" || { echo "usage: make eval-verify PREFIX=<prefix>"; exit 2; }
	bash scripts/eval/sync.sh verify $(PREFIX)

reproduce-curation: ## Rebuild the 2026-07-01 curated set (MODE=structure|responses, APPLY=1 to mutate, BACKUP= for responses). No args = preview.
	@echo "== verifying artifact checksums =="; \
	sha256sum -c $(IMPORT)/checksums.sha256 2>/dev/null \
	  || echo "(corpus/backup not present locally — fetch the external artifacts first)"; \
	if [ "$(MODE)" = "structure" ] && [ -n "$(APPLY)" ]; then \
	  for y in configs/annotation/domains/*.yaml; do d=$$(basename $$y .yaml); \
	    echo "== import $$d =="; bash scripts/annotation/import.sh "$$d"; done; \
	elif [ "$(MODE)" = "responses" ] && [ -n "$(APPLY)" ]; then \
	  test -n "$(BACKUP)" || { echo "usage: make reproduce-curation MODE=responses BACKUP=<dir> APPLY=1"; exit 2; }; \
	  $(PY) scripts/annotation/argilla_backup.py restore "$(BACKUP)" --apply; \
	fi; \
	echo "== prune live -> curated keep-lists (no APPLY = preview, doubles as verification) =="; \
	$(PY) scripts/annotation/prune_to_keeplist.py --keep-lists $(CURATION)/keep_lists $(if $(APPLY),--apply,)
