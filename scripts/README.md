# scripts/

Committed pipeline code. See the [annotation pipeline](../docs/annotation.md) doc for the
overview; run `make help` for the targets.

- `pipeline.sh` - orchestrator: runs a contiguous slice of the stages (pre-flight, lock, parallelism).
- `daily.sh` - nightly logging (export → `logs/annotation/log.jsonl`).
- `annotation/` - the stages (`run_querygen.sh`, `run_bot.py`, `build_combined.py`, `setup.sh`, `import.sh`, `export.sh`) plus logging/reporting helpers, `argilla_backup.py` (dump/restore), and `prune_to_keeplist.py` (reduce live Argilla to a keep-list; used by `make repro-reproduce`).
- `lib/` - shared helpers: `common.sh` (shell) and `workspace.py` (python).
- `eval/` - the eval-stage report scripts (the deliverable CSVs), `train_evaluators.py` (train the synthetic evaluators; see [eval training](../docs/eval-training.md)), plus shared vocabulary.
- `repro/` - `bundle.py`, the pin/verify/reproduce tool behind the `repro-*` targets.
- `transfer/` - `sync.sh`, the Blob pipe behind the `transfer-*` targets.
