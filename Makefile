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
# Eval deliverables (reports/eval/<date>/, one CSV + its .provenance.json each):
#   make eval-annotation-tables            # annotation counts + per-label prevalence
#   make eval-retrieval-manifest           # what the retriever returned per query
#   make eval-score-human                  # the corpus metric estimates from human labels
#   make eval-catalog                      # the corpus catalog for the fairness audit
#   make eval-deliverables                 # all seven deliverable targets, in order
# The first four read the frozen canonical export and the log snapshot pinned in
# configs/eval/freeze.conf, which `make annotation-freeze` writes. See
# docs/data-dictionary.md for what the columns mean.
#
# Eval training (the synthetic evaluators; the training extra is not in uv.lock - GPU host):
#   make eval-train-inputs                 # pool the frozen export -> data/eval-inputs/training/
#   make eval-train-seqlen                 # diagnostic: sequence-length truncation per task
#   make eval-train TASK=retrieval         # train one evaluator (grounding is 2+ hours)
# See docs/synthetic-evaluators.md for the per-task config and what was tried and rejected.
#
# Eval prediction (applying the trained evaluators; same environment as training - GPU host):
#   make eval-predict-inputs POPULATION=annotated       # -> data/eval-inputs/predict/annotated/
#   make eval-predict TASK=retrieval POPULATION=annotated RUN_ID=<id>
#   make eval-score-synthetic POPULATION=annotated      # -> synthetic_metric_estimates.*.csv
#   make eval-model-metrics                             # -> evaluator_metrics.csv
#   make eval-model-calibration                         # -> evaluator_calibration.csv (GPU)
# See docs/synthetic-evaluators.md for the populations, the run order and the output layout.
#
# Naming: every target is <namespace>-<operation>, the namespace being the tool or stage
# it operates on — querygen-*, bot-*, combine-*, annotation-*, eval-*, transfer-*,
# repro-*. Only the orchestrator (pipeline, plan) is bare. The stage targets share their
# names with pipeline.sh's slice tokens; see its usage block.

SHELL := /bin/bash
PY := .venv/bin/python

# uv's interpreter and wheel cache live in-tree rather than under ~/.local/share/uv
# and ~/.cache/uv, so that a checkout shared between users (the GPU server, where
# access is granted by POSIX ACL) does not depend on any one home directory:
#
#   - ~/.local/share/uv is mode 700, so a .venv built against a uv-managed
#     interpreter there resolves to a python nobody else can execute.
#   - ~/.cache/uv extracts wheels as mode 711 — no group or other read. uv hardlinks
#     those into .venv, and link-mode=copy preserves the mode too, so .venv inherits
#     the unreadable bits either way.
#
# Extracted in-tree instead, both pick up the directory's default ACL (mode 771,
# mask rwx) and every user with ACL access can read them. Absolute paths because a
# relative uv cache-dir is resolved against the *current* directory, which would
# scatter caches through the tree. Override in the environment on a single-user
# machine to reuse a normal shared cache; the defaults just have to be safe here.
UV_PYTHON_INSTALL_DIR ?= $(CURDIR)/.uv/python
UV_CACHE_DIR ?= $(CURDIR)/.uv/cache
export UV_PYTHON_INSTALL_DIR UV_CACHE_DIR

# The Hugging Face cache moves in-tree for the same reason, and needs it just as badly: the
# eval-train targets download a tokenizer and a base model, and on this shared box
# ~/.cache/huggingface was created root-owned mode 755, so no user can write it and the
# download fails outright. In-tree it inherits the checkout's default ACL, and the base model
# is fetched once for everyone rather than once per home directory.
HF_HOME ?= $(CURDIR)/.hf
export HF_HOME

# Eval report output args. The scripts resolve the dated output dir themselves and drop
# the data dictionary beside the CSVs (ws.write_csv); OUT= redirects for an off-date or
# scratch run.
EVAL_ARGS := $(if $(OUT),--out-dir $(OUT),)

# Pass-through flags for pipeline.sh / plan, built from make vars.
PIPELINE_ARGS := $(if $(ONLY),--only $(ONLY),) $(if $(FROM),--from $(FROM),) \
                 $(if $(TO),--to $(TO),) $(if $(FILTER),--filter $(FILTER),) \
                 $(if $(JOBS),--jobs $(JOBS),)

.DEFAULT_GOAL := help
.PHONY: pipeline plan \
        querygen-run bot-run bot-probe combine-run \
        annotation-setup annotation-import \
        annotation-log annotation-export annotation-snapshot annotation-freeze \
        annotation-backup annotation-restore \
        annotation-report annotation-report-tables annotation-report-pdf \
        annotation-report-plots \
        eval-deliverables eval-annotation-tables eval-retrieval-manifest \
        eval-score-human eval-catalog \
        eval-train-inputs eval-train-seqlen eval-train \
        eval-predict-inputs eval-predict eval-score-synthetic \
        eval-model-metrics eval-model-calibration \
        transfer-push transfer-pull transfer-verify \
        repro-pin repro-verify repro-reproduce venv-setup docs-check help

# --- setup ---

venv-setup: ## Create/refresh .venv from uv.lock, on the in-tree interpreter every user can read
	uv sync --frozen
	@$(PY) --version

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

annotation-snapshot: ## Export + log one annotation snapshot -> log.jsonl; what the nightly cron runs (reporting is manual: make annotation-report)
	bash scripts/daily.sh

# Deliberately not part of annotation-export: the nightly cron re-exports every night, and a
# freeze asserts "these bytes back a published number". It writes configs/eval/freeze.conf;
# committing that is the operator's step, and the script prints it.
annotation-freeze: ## Freeze the current export tree + pin it for the eval reports (DATE=/RUN_AT= optional; derived from the export by default)
	bash scripts/annotation/freeze.sh "$(DATE)" "$(RUN_AT)"

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

# --- eval deliverables (reports/eval/<date>/; OUT= to redirect) ---

# One target per script, so each names its own output and its own prerequisites. The
# umbrella runs all seven; it is the only target here that fans out. Later ones need
# artefacts the GPU host produces (train_outputs/, prediction_outputs/) and the last needs
# a GPU, so on a box without them the umbrella fails at that step and says why - rather
# than a name implying a subset. The model *stages* (eval-train*, eval-predict*) are not
# deliverables and stay out: they produce models, and one of them runs for two hours.
eval-deliverables: eval-annotation-tables eval-retrieval-manifest eval-catalog eval-score-human eval-score-synthetic eval-model-metrics eval-model-calibration ## Eval: every report deliverable, in order (later ones need the GPU host's outputs)

eval-annotation-tables: ## Eval: frozen export + pinned log snapshot -> annotation_operations.csv, annotation_label_summary.csv
	$(PY) scripts/eval/annotation_tables.py $(EVAL_ARGS)

eval-retrieval-manifest: ## Eval: curated corpus + annotation state -> retrieval_manifest.csv (the fairness audit's join key)
	$(PY) scripts/eval/retrieval_manifest.py $(EVAL_ARGS)

eval-score-human: ## Eval: frozen export -> eval_metric_estimates.csv (runs `pragmata eval score` from the eval pin; stages filtered CSVs in data/eval-inputs/)
	$(PY) scripts/eval/score_human_annotations.py $(EVAL_ARGS)

eval-catalog: ## Eval: publikationsbot vector store -> corpus_catalog.csv (needs an active `az login`)
	$(PY) scripts/eval/corpus_catalog.py $(EVAL_ARGS)

# --- eval training (the synthetic evaluators; see docs/synthetic-evaluators.md) ---
#
# Training needs the `eval` extra (pragmata[eval] -> tlmtc[train]) and a CUDA torch, neither
# of which is in uv.lock - deliberately, since that lock freezes the environment behind the
# published human-label numbers. So these run on the GPU host's own environment; the first
# two are CPU-only and run anywhere.

eval-train-inputs: ## Eval training: pool the frozen export per task -> data/eval-inputs/training/ (EXPORTS= to override the tree)
	$(PY) scripts/eval/train_evaluators.py combine $(if $(EXPORTS),--exports $(EXPORTS),)

eval-train-seqlen: ## Eval training: diagnostic, how much of each task's input the default sequence_length truncates
	$(PY) scripts/eval/train_evaluators.py check-sequence-length

# TASK is the whole interface on purpose: each task's configuration is pinned in
# configs/eval/training/<task>.yaml, not passed here, because the three are not
# interchangeable knobs. THRESHOLD_TYPE overrides that pin for one run.
eval-train: ## Eval training: train one evaluator -> data/eval/train_outputs/<run_id>/ (TASK=retrieval|grounding|generation; grounding is 2+ hours)
	@case "$(TASK)" in retrieval|grounding|generation) ;; *) \
	  echo "usage: make eval-train TASK=retrieval|grounding|generation"; exit 2 ;; esac
	$(PY) scripts/eval/train_evaluators.py train $(TASK) $(if $(THRESHOLD_TYPE),--threshold-type $(THRESHOLD_TYPE),)

# --- eval prediction (applying the evaluators; see docs/synthetic-evaluators.md) ---
#
# Same environment split as training: staging and scoring are CPU-only and run anywhere,
# `eval-predict` needs the training venv inside the GPU container (PY=$$HOME/train-venv/bin/python).
# There are no YAML configs here on purpose - population and evaluator run id are arguments,
# because until the final run nothing is published for a pin to stand behind.

eval-predict-inputs: ## Eval prediction: stage the unlabelled per-task CSVs -> data/eval-inputs/predict/<population>/ (POPULATION=annotated|corpus; EXPORTS=/CORPUS_DIR= to override the source)
	@case "$(POPULATION)" in annotated|corpus) ;; *) \
	  echo "usage: make eval-predict-inputs POPULATION=annotated|corpus"; exit 2 ;; esac
	$(PY) scripts/eval/predict_evaluators.py predict-inputs --population $(POPULATION) \
	  $(if $(EXPORTS),--exports $(EXPORTS),) $(if $(CORPUS_DIR),--corpus-dir $(CORPUS_DIR),)

# RUN_ID is optional and should not be: the script defaults to the latest evaluator for the
# task and says so, which is right for a scratch run and wrong for a published one. Pass it
# for anything whose numbers leave the box.
# POPULATION=testsplit is internal - `eval-model-calibration` stages a run's
# own held-out split and predicts it in one pass. It is accepted here so that pass can be
# repeated by hand, which needs RUN_ID to name the run the split was staged from.
eval-predict: ## Eval prediction: one evaluator over one population -> data/eval/prediction_outputs/<run_id>-<population>/ (TASK=, POPULATION=annotated|corpus|testsplit, RUN_ID=, BATCH_SIZE=)
	@case "$(TASK)" in retrieval|grounding|generation) ;; *) \
	  echo "usage: make eval-predict TASK=retrieval|grounding|generation POPULATION=annotated|corpus (testsplit is internal - see make help)"; exit 2 ;; esac
	@case "$(POPULATION)" in annotated|corpus|testsplit) ;; *) \
	  echo "usage: make eval-predict TASK=$(TASK) POPULATION=annotated|corpus, or testsplit with RUN_ID= (internal - staged by eval-model-calibration)"; exit 2 ;; esac
	$(PY) scripts/eval/predict_evaluators.py predict $(TASK) --population $(POPULATION) \
	  $(if $(RUN_ID),--evaluator-run-id $(RUN_ID),) $(if $(BATCH_SIZE),--batch-size $(BATCH_SIZE),)

eval-score-synthetic: ## Eval prediction: score one predicted population -> synthetic_metric_estimates.<population>.csv (POPULATION=annotated|corpus, default annotated)
	$(PY) scripts/eval/score_synthetic_predictions.py \
	  --population $(if $(POPULATION),$(POPULATION),annotated) $(EVAL_ARGS)

# These two were one target behind PART=, which hid the fact that only one of them needs a
# GPU. Split so the prerequisite is visible in the target name, as it is everywhere else.
# RUN_IDS is a space-separated list of <task>=<run_id>, not a bare id: both report on all
# three tasks at once.
eval-model-metrics: ## Eval: each evaluator's quality on its own held-out split -> evaluator_metrics.csv (CPU-only; RUN_IDS="retrieval=<id> ...")
	$(PY) scripts/eval/evaluator_report.py metrics $(EVAL_ARGS) \
	  $(foreach pair,$(RUN_IDS),--run-id $(pair))

eval-model-calibration: ## Eval: evaluator probability calibration -> evaluator_calibration.csv (GPU host - it re-predicts each run's test split; RUN_IDS= BATCH_SIZE=)
	$(PY) scripts/eval/evaluator_report.py calibration $(EVAL_ARGS) \
	  $(foreach pair,$(RUN_IDS),--run-id $(pair)) \
	  $(if $(BATCH_SIZE),--batch-size $(BATCH_SIZE),)

# --- data transport (Blob, staged through data/transfer/; EVAL_BLOB_* env names are
#     historical - the pipe is not eval-specific) ---

transfer-push: ## Push a tree to the transfer Blob (SRC= source tree, PREFIX= dest prefix; both required)
	@test -n "$(SRC)" && test -n "$(PREFIX)" || { echo "usage: make transfer-push SRC=<tree> PREFIX=<prefix>"; exit 2; }
	bash scripts/transfer/sync.sh push "$(SRC)" "$(PREFIX)"

transfer-pull: ## Pull a Blob prefix into data/transfer/<prefix>/ + verify (PREFIX= required)
	@test -n "$(PREFIX)" || { echo "usage: make transfer-pull PREFIX=<prefix>"; exit 2; }
	bash scripts/transfer/sync.sh pull "$(PREFIX)"

transfer-verify: ## Re-verify an already-pulled tree against its manifest (PREFIX= under data/transfer/)
	@test -n "$(PREFIX)" || { echo "usage: make transfer-verify PREFIX=<prefix>"; exit 2; }
	bash scripts/transfer/sync.sh verify "$(PREFIX)"

# --- reproducibility (dated bundles; see reproducibility/README.md for the contract) ---

repro-pin: ## Pin paths into a new bundle reproducibility/<today>-<NAME>/ (NAME= PATHS= required, KIND=lineage|freeze)
	@test -n "$(NAME)" && test -n "$(PATHS)" || { echo 'usage: make repro-pin NAME=<name> PATHS="<path ...>" [KIND=freeze]'; exit 2; }
	$(PY) scripts/repro/bundle.py pin "$(NAME)" $(PATHS) $(if $(KIND),--kind $(KIND),)

# bundle.py exits 0 all-OK / 2 mismatch / 3 absent-only. make collapses any recipe failure
# to its own exit 2, but prints the script's code as `Error <n>`; call the script directly
# when a caller needs to branch on absent-vs-mismatch.
repro-verify: ## Verify bundle pins per file - OK/MISMATCH/ABSENT (PIN=<bundle-dir>, default all)
	$(PY) scripts/repro/bundle.py verify $(PIN)

# PIN names the bundle you are replaying toward and is what the kind: check applies to; it
# does not select how much of the chain is composed. Lineage replay always composes every
# lineage bundle's keep-lists in date order, because a prefix of the chain was never live.
repro-reproduce: ## Replay the lineage onto its composed end state (PIN= required; MODE=structure|responses, BACKUP=, APPLY=1). No APPLY = preview
	@test -n "$(PIN)" || { echo "usage: make repro-reproduce PIN=<bundle-dir> [MODE=structure|responses] [BACKUP=<dir>] [APPLY=1]"; exit 2; }
	$(PY) scripts/repro/bundle.py reproduce "$(PIN)" $(if $(MODE),--mode $(MODE),) $(if $(BACKUP),--backup $(BACKUP),) $(if $(APPLY),--apply,)

# --- docs ---
# The README lists the targets in its own shortened wording rather than piping `make help`,
# because the grouping is what makes it readable. This checks the two agree on which targets
# exist, and that no cross-reference points at a renamed file or heading.
docs-check: ## Check the README's target list against the Makefile + every doc link resolves
	$(PY) scripts/lib/check_docs.py

help: ## Show this help
	@awk 'BEGIN{FS=":.*## "} /^[a-zA-Z_-]+:.*## /{printf "  \033[36m%-24s\033[0m %s\n",$$1,$$2}' $(MAKEFILE_LIST)
