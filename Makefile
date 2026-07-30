# pragmata-workspace — operational entrypoint over scripts/.
# Run `make` or `make help` for the target list. Scripts remain runnable
# directly; these targets just document the pipeline and wire up args.
#
# Dataset build pipeline, in order (each stage is a make target and a pipeline.sh token):
#   querygen-run       generate synthetic queries
#   bot-run            query publikationsbot for answers + chunks
#   combine-run        assemble the import-ready dataset
#   annotation-setup   provision Argilla workspaces + users
#   annotation-import  load the datasets into Argilla
# It ends at the Argilla import; annotating, logging and reporting are separate ops.
#
# Orchestrated (scripts/pipeline.sh) — runs a contiguous slice over a filter:
#   make pipeline                          # full pipeline, all domains
#   make pipeline TO=bot-run               # querygen-run + bot-run
#   make pipeline FROM=combine-run         # combine-run + the two annotation stages
#   make pipeline ONLY=bot-run FILTER=gesundheit JOBS=8
#   make plan TO=bot-run                   # preview a slice without running
#
# Single stages (call the stage scripts directly):
#   make querygen-run SPECS=demokratie-und-zusammenhalt,europas-zukunft
#   make bot-run SPEC=gesundheit
#   make bot-probe                         # one-query smoke test, writes no JSONL
#   make combine-run DOMAINS="gesundheit europas-zukunft"
#   make annotation-setup DOMAIN=gesundheit
#   make annotation-import DOMAIN=gesundheit
#
# Reproducibility (dated bundles under reproducibility/, one per operation or run):
#   make repro-verify                      # check every bundle's pins
#   make repro-verify PIN=2026-07-01-annotation-curation
#   make repro-pin NAME=x PATHS="a b"      # start a new dated bundle
#   make repro-reproduce PIN=2026-07-01-annotation-curation
# See reproducibility/README.md for the bundle contract.
#
# Naming: every target is <namespace>-<operation>, the namespace being the tool or stage
# it operates on — querygen-*, bot-*, combine-*, annotation-*, transfer-*, repro-*. Only
# the orchestrator (pipeline, plan) is bare. The stage targets share their names with
# pipeline.sh's slice tokens; see its usage block.

SHELL := /bin/bash
PY := .venv/bin/python

# Pass-through flags for pipeline.sh / plan, built from make vars.
PIPELINE_ARGS := $(if $(ONLY),--only $(ONLY),) $(if $(FROM),--from $(FROM),) \
                 $(if $(TO),--to $(TO),) $(if $(FILTER),--filter $(FILTER),) \
                 $(if $(JOBS),--jobs $(JOBS),)

.DEFAULT_GOAL := help
.PHONY: pipeline plan \
        querygen-run bot-run bot-probe combine-run \
        annotation-setup annotation-import \
        annotation-log annotation-export annotation-daily \
        annotation-backup annotation-restore \
        annotation-report annotation-report-tables annotation-report-pdf \
        annotation-report-plots \
        transfer-push transfer-pull transfer-verify \
        repro-pin repro-verify repro-reproduce help

# --- orchestrator ---

pipeline: ## Run a slice of the build pipeline, querygen-run -> annotation-import (FROM= TO= ONLY= FILTER= JOBS=); no args = full
	bash scripts/pipeline.sh $(PIPELINE_ARGS)

plan: ## Preview a pipeline slice without running it (same FROM= TO= ONLY= FILTER= vars)
	bash scripts/pipeline.sh --dry-run $(PIPELINE_ARGS)

# --- pipeline stages ---

querygen-run: ## Stage: generate synthetic queries (SPECS=a,b to filter)
	bash scripts/annotation/run_querygen.sh "$(SPECS)"

bot-run: ## Stage: run publikationsbot over generated queries (SPEC=x to filter)
	$(PY) scripts/annotation/run_bot.py $(if $(SPEC),--spec $(SPEC),)

bot-probe: ## One-query bot smoke test; dumps raw SSE, writes no JSONL (SPEC=x to pick the spec)
	$(PY) scripts/annotation/run_bot.py --probe $(if $(SPEC),--spec $(SPEC),)

combine-run: ## Stage: pool bot batches + edgecases into the import-ready per-domain dataset (DOMAINS= to filter)
	$(PY) scripts/annotation/build_combined.py $(DOMAINS)

annotation-setup: ## Stage: provision Argilla workspaces + users for one domain (DOMAIN=)
	@test -n "$(DOMAIN)" || { echo "usage: make annotation-setup DOMAIN=<domain>"; exit 2; }
	bash scripts/annotation/setup.sh "$(DOMAIN)"

annotation-import: ## Stage: import one domain's combined JSONL (DOMAIN=)
	@test -n "$(DOMAIN)" || { echo "usage: make annotation-import DOMAIN=<domain>"; exit 2; }
	bash scripts/annotation/import.sh "$(DOMAIN)"

# --- annotation ops ---

annotation-log: ## Log an annotation snapshot -> logs/annotation/log.jsonl (--summary for a CLI table)
	$(PY) scripts/annotation/log.py $(if $(DOMAIN),--domain $(DOMAIN),)

annotation-export: ## Export current annotations to per-task CSVs (DOMAIN= to filter, default all)
	bash scripts/annotation/export.sh $(DOMAIN)

annotation-daily: ## Nightly logging: export -> log.jsonl (reporting is manual: make annotation-report)
	bash scripts/daily.sh

annotation-backup: ## Status-preserving Argilla backup -> argilla_backup/<UTC-ts>/ (read-only)
	$(PY) scripts/annotation/argilla_backup.py dump

# Scoped restores (--workspace / --dataset / --record-id / --only) call the script directly.
annotation-restore: ## Restore an Argilla backup (DIR= required); previews unless APPLY=1
	@test -n "$(DIR)" || { echo "usage: make annotation-restore DIR=<backup-dir> [APPLY=1]"; exit 2; }
	$(PY) scripts/annotation/argilla_backup.py restore "$(DIR)" $(if $(APPLY),--apply,)

# --- annotation reporting ---

annotation-report: annotation-report-tables annotation-report-plots ## Render latest snapshot -> reports/annotation/<date>/ (report.md + plots, +_latest)

annotation-report-tables: ## Render tables only -> reports/annotation/<date>/report.md
	$(PY) scripts/annotation/report_tables.py

annotation-report-pdf: ## Render latest snapshot tables -> reports/annotation/<date>/report.pdf (needs pandoc + xelatex)
	@md=$$($(PY) scripts/annotation/report_tables.py); \
	pandoc "$$md" -o "$${md%.md}.pdf" --pdf-engine=xelatex -V fontsize=9pt \
	  -V geometry:margin=1.5cm -V mainfont="DejaVu Serif" -V monofont="DejaVu Sans Mono" \
	  && echo "wrote $${md%.md}.pdf"

annotation-report-plots: ## Render plots only (PNGs) -> reports/annotation/<date>/ (needs matplotlib)
	$(PY) scripts/annotation/plot_summary.py

# --- data transport (Blob, staged through data/transfer/; EVAL_BLOB_* env names are
#     historical - the pipe is not eval-specific) ---

transfer-push: ## Push a tree to the transfer Blob (SRC= source tree, PREFIX= dest prefix; both required)
	@test -n "$(SRC)" && test -n "$(PREFIX)" || { echo "usage: make transfer-push SRC=<tree> PREFIX=<prefix>"; exit 2; }
	bash scripts/transfer/sync.sh push "$(SRC)" "$(PREFIX)"

transfer-pull: ## Pull a Blob prefix into data/transfer/<prefix>/ + verify (PREFIX= required)
	@test -n "$(PREFIX)" || { echo "usage: make transfer-pull PREFIX=<prefix>"; exit 2; }
	bash scripts/transfer/sync.sh pull $(PREFIX)

transfer-verify: ## Re-verify an already-pulled tree against its manifest (PREFIX= under data/transfer/)
	@test -n "$(PREFIX)" || { echo "usage: make transfer-verify PREFIX=<prefix>"; exit 2; }
	bash scripts/transfer/sync.sh verify $(PREFIX)

# --- reproducibility (dated bundles; see reproducibility/README.md for the contract) ---

repro-pin: ## Pin paths into a new bundle reproducibility/<today>-<NAME>/ (NAME= PATHS= required, KIND=lineage|freeze)
	@test -n "$(NAME)" && test -n "$(PATHS)" || { echo 'usage: make repro-pin NAME=<name> PATHS="<path ...>" [KIND=freeze]'; exit 2; }
	$(PY) scripts/repro/bundle.py pin "$(NAME)" $(PATHS) $(if $(KIND),--kind $(KIND),)

# bundle.py exits 0 all-OK / 2 mismatch / 3 absent-only. make collapses any recipe failure
# to its own exit 2, but prints the script's code as `Error <n>`; call the script directly
# when a caller needs to branch on absent-vs-mismatch.
repro-verify: ## Verify bundle pins per file - OK/MISMATCH/ABSENT (PIN=<bundle-dir>, default all)
	$(PY) scripts/repro/bundle.py verify $(PIN)

repro-reproduce: ## Replay a lineage bundle onto the composed end state (PIN= required; MODE=structure|responses, BACKUP=, APPLY=1). No APPLY = preview
	@test -n "$(PIN)" || { echo "usage: make repro-reproduce PIN=<bundle-dir> [MODE=structure|responses] [BACKUP=<dir>] [APPLY=1]"; exit 2; }
	$(PY) scripts/repro/bundle.py reproduce "$(PIN)" $(if $(MODE),--mode $(MODE),) $(if $(BACKUP),--backup $(BACKUP),) $(if $(APPLY),--apply,)

help: ## Show this help
	@awk 'BEGIN{FS=":.*## "} /^[a-zA-Z_-]+:.*## /{printf "  \033[36m%-24s\033[0m %s\n",$$1,$$2}' $(MAKEFILE_LIST)
