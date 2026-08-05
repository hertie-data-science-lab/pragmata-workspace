# Human annotation scoring pipeline

## Deliverables

Three targets produce the report deliverables into `reports/eval/<date>/` (`OUT=` to redirect):

| Target | Script | Output |
|---|---|---|
| `make eval-report` | `annotation_tables.py` | `annotation_operations.csv`, `annotation_label_summary.csv` |
| `make eval-report` | `retrieval_manifest.py` | `retrieval_manifest.csv` |
| `make eval-score` | `score_human_annotations.py` | `eval_metric_estimates.csv`, via `pragmata eval score` |
| `make eval-catalog` | `corpus_catalog.py` | `corpus_catalog.csv`, from the publikationsbot vector store (needs `az login`) |

Every CSV ships with a `.provenance.json` file (containing script, workspace SHA, pragmata pin, hashed inputs, parameters and seeds, snapshot identity, and the dictionary's hash), and the [data dictionary](eval-data-dictionary.md) is copied beside the CSVs whose`.provenance.json` pins it.

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

`data/eval/` is pragmata's own tool tree and holds only what pragmata wrote there (`scores/`, later `checkpoints/` and `predictions/`).Workspace-produced inputs *to* the tool - the pooled, filtered CSVs `score_human_annotations.py` hands to `eval score --path` - are staged in `data/eval-inputs/`.

## Cutting a new freeze

The order matters: each `.provenance.json` records the workspace commit it was generated at, and the reproducibility bundle pins them by hash, so code changes and re-runs must not interleave. 

1. **Finish and commit any code changes first.** We generally assume the pipeline is stable, but if there's been any edits this must be committed.
2. **Take the export and snapshot**: `make annotation-export`  then `make annotation-log`.
3. **Freeze the tree and write the pin**: `make annotation-freeze`. 
   - DATE and RUN_AT (i.e. relevant log) both derive from the export tree's own `created_at` (pass `DATE=<date>` or `RUN_AT=<run_at>` to override either). 
   - Guards before the copy is op'd: (i) clean working tree, (ii) no freeze under that date already, (iii) the resolved RUN_AT schema-current, (iv) no real names left in `exports/`. 
   - After the guards this make targets creates the read-only dated copy (copy, then `chmod -R a-w` the new dir) and writes `configs/eval/freeze.conf`.
4. **Commit the pin.**  Until it is committed, another checkout still resolves the old date.
5. **Regenerate everything on the clean tree**: `make eval-report eval-score eval-catalog`. 
6. **Re-pin the bundle (if preexisting).** `repro-pin` refuses a pre-existing bundle dir and `pins.sha256` is generated, never hand-edited - so delete the old bundle dir, re-pin, commit.
7. **Publish**: `make transfer-push SRC=data/annotation/exports-frozen/<date> PREFIX=exports-frozen/<date>`. To verify Blob without a pull, download the remote `MANIFEST.sha256` and diff it against a freshly computed local manifest - comparing push's own printed hash is circular.

If the eval pragmata pin moves, the numbers must be re-derived under the new pin, not assumed to carry over.
