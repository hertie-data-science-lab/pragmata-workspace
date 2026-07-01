# scripts/

Committed pipeline code. See the root README for the [pipeline overview](../README.md#pipeline)
and [make targets](../README.md#make-targets).

- `pipeline.sh` — orchestrator: runs a contiguous slice of the stages (pre-flight, lock, parallelism).
- `daily.sh` — nightly logging (export → `logs/annotation/log.jsonl`).
- `annotation/` — the stages (`run_querygen.sh`, `run_bot.py`, `build_combined.py`, `setup.sh`, `import.sh`, `export.sh`) plus logging/reporting helpers, `argilla_backup.py` (dump/restore), and `prune_to_keeplist.py` (reduce live Argilla to a keep-list; used by `make reproduce-curation`).
- `lib/` — shared helpers: `common.sh` (shell) and `workspace.py` (python).
- `eval/` — stub for the evaluation stage.
