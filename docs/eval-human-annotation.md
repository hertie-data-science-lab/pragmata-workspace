# Human annotation scoring pipeline

## Deliverables

Three targets produce the report deliverables into `reports/eval/<date>/` (`OUT=` to redirect):

| Target | Script | Output |
|---|---|---|
| `make eval-report` | `annotation_tables.py` | `annotation_operations.csv`, `annotation_label_summary.csv` |
| `make eval-report` | `retrieval_manifest.py` | `retrieval_manifest.csv` |
| `make eval-score` | `score_human_annotations.py` | `eval_metric_estimates.csv`, via `pragmata eval score` |
| `make eval-catalog` | `corpus_catalog.py` | `corpus_catalog.csv`, from the publikationsbot vector store (needs `az login`) |

Three further deliverables come from the model side of the pipeline ([Synthetic evaluators](eval-synthetic-evaluator.md)), into the same directory and under the same conventions:

| Target | Script | Output |
|---|---|---|
| `make eval-score-synthetic POPULATION=<p>` | `score_synthetic_predictions.py` | `synthetic_metric_estimates.<p>.csv`, via `pragmata eval score --prediction-id` |
| `make eval-evaluator-report` | `evaluator_report.py` | `evaluator_metrics.csv` |
| `make eval-evaluator-report PART=calibration` | `evaluator_report.py` | `evaluator_calibration.csv` (needs the GPU environment) |

Every CSV ships with a `.provenance.json` file (containing script, workspace SHA, pragmata pin, hashed inputs, parameters and seeds, snapshot identity, and the dictionary's hash), and the [data dictionary](deliverables-data-dictionary.md) is copied beside the CSVs whose `.provenance.json` pins it. Every input the script declared is listed, including one that was not there when it ran - as `"sha256": null, "missing": true`, never by omission.

## The three pins

Every report number is derived from pinned inputs:

1. **The frozen export tree** - `data/annotation/exports-frozen/<FREEZE_DATE>/`, a read-only (`chmod -R a-w`) copy of a given export, cut by `make annotation-freeze`. The live `data/annotation/exports/` is overwritten by the 02:00 cron and is never read by the report scripts (as it is effectively dynamic).
2. **The canonical log snapshot** - one line of `logs/annotation/log.jsonl`, selected by its `run_at` timestamp (not "the latest") and pinned by the sha256 of that single line.
3. **The eval pragmata pin** - `PRAGMATA_EVAL_SRC` in `.env`, a checkout separate from the annotation pipeline's frozen demo pin, so the live instance's export behaviour stays fixed while eval tracks upstream. 
>NB: The annotation pin is a git dependency installed by `uv sync`; the eval pin cannot be, because two commits of one package cannot coexist in one venv - so it stays a path in `.env` and shadows the installed package on `PYTHONPATH` at call time. 

The **environment** is pinned too: `uv.lock` freezes all 126 packages at the versions that produced these numbers. See the `constraint-dependencies` comment in `pyproject.toml`.

The first two are `FREEZE_DATE` and `CANONICAL_SNAPSHOT_RUN_AT` in
[`configs/eval/freeze.conf`](../configs/eval/freeze.conf) - the pin as data rather than as code, so `make annotation-freeze` writes it and the operator only commits it. `eval_common.py` reads that file, so a refresh moves the pin once for all three scripts. The scripts also refuse to run when the pin is not the newest freeze on disk. Pass `--exports` to read a non-canonical tree on purpose. 

## Ownership

`data/eval/` is pragmata's own tool tree and holds only what pragmata wrote there (`scores/`, `train_outputs/`, `prediction_outputs/`). Workspace-produced inputs *to* the tool - the pooled, filtered CSVs `score_human_annotations.py` hands to `eval score --path`, and the staged unlabelled CSVs `predict_evaluators.py` hands to `predict-labels` - are staged in `data/eval-inputs/`.

Two files this workspace writes *into* that tree are the deliberate exceptions, both marked as such by a `.workspace.` infix in the name: `train_provenance.workspace.json` and `predict_provenance.workspace.json`, each inside the run directory it describes. They are there because those directories are what gets pushed off the GPU box, and neither pragmata's nor tlmtc's own sidecars name the workspace commit, the staged input or the freeze behind it.

## Cutting a new freeze

The order matters: each `.provenance.json` records the workspace commit it was generated at, and the reproducibility bundle pins them by hash, so code changes and re-runs must not interleave. 

1. **Finish and commit any code changes first.** We generally assume the pipeline is stable, but if there's been any edits this must be committed.
2. **Take the export and snapshot**: `make annotation-export`  then `make annotation-log`.
3. **Freeze the tree and write the pin**: `make annotation-freeze`. 
   - DATE and RUN_AT (i.e. relevant log) both derive from the export tree's own `created_at` (pass `DATE=<date>` or `RUN_AT=<run_at>` to override either). 
   - Guards before the copy: (i) clean working tree, (ii) no freeze under that date already, (iii) the resolved RUN_AT schema-current and paired with the export - one that predates it, or lags it implausibly, is refused whether derived or passed, (iv) no real names left in `exports/`.
   - It takes the same `.export.lock` `export.sh` does, so it cannot copy a tree the 02:00 cron is halfway through rewriting.
   - After the guards the target creates the read-only dated copy (copy, then `chmod -R a-w` the new dir) and writes `configs/eval/freeze.conf`. It cleans up after itself: if the copy or the `chmod` fails, the partial dated dir is removed rather than left for the next run's "already frozen" guard to mistake for a real freeze.
4. **Commit the pin.**  Until it is committed, another checkout still resolves the old date.
5. **Regenerate everything on the clean tree**: `make eval-report eval-score eval-catalog`. 
6. **Re-pin the bundle (if preexisting).** `repro-pin` refuses a pre-existing bundle dir and `pins.sha256` is generated, never hand-edited - so delete the old bundle dir, re-pin, commit.
7. **Publish**: `make transfer-push SRC=data/annotation/exports-frozen/<date> PREFIX=exports-frozen/<date>`. To verify Blob without a pull, download the remote `MANIFEST.sha256` and diff it against a freshly computed local manifest - comparing push's own printed hash is circular.

If the eval pragmata pin moves, the numbers must be re-derived under the new pin, not assumed to carry over.
