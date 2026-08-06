# scripts/

Committed pipeline code. See the [annotation pipeline](../docs/annotation.md) doc for the
overview; run `make help` for the targets.

- `pipeline.sh` - orchestrator: runs a contiguous slice of the stages (pre-flight, lock, parallelism).
- `daily.sh` - nightly logging (export → `logs/annotation/log.jsonl`).
- `annotation/` - the stages (`run_querygen.sh`, `run_bot.py`, `build_combined.py`, `setup.sh`, `import.sh`, `export.sh`) plus logging/reporting helpers, `argilla_backup.py` (dump/restore), and `prune_to_keeplist.py` (reduce live Argilla to a keep-list; used by `make repro-reproduce`).
- `lib/` - shared helpers: `common.sh` (shell) and `workspace.py` (python).
- `eval/` - the eval-stage report scripts (the deliverable CSVs), plus the model stage: `train_evaluators.py` (train the synthetic evaluators), `predict_evaluators.py` (stage unlabelled populations and apply an evaluator), `score_synthetic_predictions.py` and `evaluator_report.py` - all four are documented together in [Synthetic evaluators](../docs/eval-synthetic-evaluator.md). Plus `eval_common.py` - the shared vocabulary, and the pragmata/GPU/evaluator-run resolution the four model-stage scripts have in common.
- `repro/` - `bundle.py`, the pin/verify/reproduce tool behind the `repro-*` targets.
- `transfer/` - `sync.sh`, the Blob pipe behind the `transfer-*` targets.
