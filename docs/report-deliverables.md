# Report deliverables

The CSVs the BSt report is built from, and the pins that make each number citable. Every column is defined in the [data dictionary](data-dictionary.md).

## The seven CSVs

All land in `reports/eval/<date>/` (`OUT=` to redirect). The first four come from the human labels, the last three from the model side ([Synthetic evaluators](synthetic-evaluators.md)).

| Target | Script | Output |
|---|---|---|
| `make eval-report` | `annotation_tables.py` | `annotation_operations.csv`, `annotation_label_summary.csv` |
| `make eval-report` | `retrieval_manifest.py` | `retrieval_manifest.csv` |
| `make eval-score` | `score_human_annotations.py` | `eval_metric_estimates.csv`, via `pragmata eval score` |
| `make eval-catalog` | `corpus_catalog.py` | `corpus_catalog.csv`, from the publikationsbot vector store (needs `az login`) |
| `make eval-score-synthetic POPULATION=<p>` | `score_synthetic_predictions.py` | `synthetic_metric_estimates.<p>.csv`, via `eval score --prediction-id` |
| `make eval-evaluator-report` | `evaluator_report.py` | `evaluator_metrics.csv` |
| `make eval-evaluator-report PART=calibration` | `evaluator_report.py` | `evaluator_calibration.csv` (needs the GPU environment) |

Every CSV ships a `.provenance.json` - script, workspace SHA, pragmata pin, hashed inputs, parameters and seeds, snapshot identity, and the dictionary's hash - and the data dictionary is copied beside the CSVs whose record pins it. Every declared input is listed, including one that was absent when the script ran: as `"sha256": null, "missing": true`, never by omission.

## The three pins

1. **The frozen export tree** - `data/annotation/exports-frozen/<FREEZE_DATE>/`, a read-only (`chmod -R a-w`) copy cut by `make annotation-freeze`. The live `data/annotation/exports/` is overwritten by the 02:00 cron, so the report scripts never read it.
2. **The canonical log snapshot** - one line of `logs/annotation/log.jsonl`, chosen by its `run_at` timestamp rather than by being latest, and pinned by that line's sha256.
3. **The eval pragmata pin** - `PRAGMATA_EVAL_SRC` in `.env`, a checkout separate from the annotation pipeline's frozen demo pin, so the live instance's export behaviour stays fixed while eval tracks upstream. It cannot be a git dependency like the annotation pin, because two commits of one package cannot coexist in one venv - so it stays a path in `.env` and shadows the installed package on `PYTHONPATH` at call time.

The environment is pinned too: `uv.lock` freezes all 126 packages at the versions that produced these numbers - see the `constraint-dependencies` comment in `pyproject.toml`.

The first two live in [`configs/eval/freeze.conf`](../configs/eval/freeze.conf) as `FREEZE_DATE` and `CANONICAL_SNAPSHOT_RUN_AT` - the pin as data rather than as code, written by `make annotation-freeze` for the operator to commit. `eval_common.py` reads that file, so a refresh moves the pin once for every script. The scripts refuse to run when the pin is not the newest freeze on disk; pass `--exports` to read a non-canonical tree on purpose.

## Ownership

`data/eval/` is pragmata's tool tree and holds only what pragmata wrote there (`scores/`, `train_outputs/`, `prediction_outputs/`). Workspace-produced inputs *to* the tool - the pooled CSVs `score_human_annotations.py` hands to `eval score --path`, and the staged unlabelled CSVs `predict_evaluators.py` hands to `predict-labels` - go in `data/eval-inputs/`.

Two files this workspace writes *into* that tree are deliberate exceptions, marked by a `.workspace.` infix: `train_provenance.workspace.json` and `predict_provenance.workspace.json`, each inside the run directory it describes. Those directories are what gets pushed off the GPU box, and neither pragmata's nor tlmtc's own sidecars name the workspace commit, the staged input or the freeze behind it.

## Cutting a new freeze

Order matters: each `.provenance.json` records the commit it was generated at and the bundle pins them by hash, so code changes and re-runs must not interleave.

1. **Commit any code changes first.** Nothing below is valid from a dirty tree.
2. **Export and snapshot**: `make annotation-export`, then `make annotation-log`.
3. **Freeze and write the pin**: `make annotation-freeze`. DATE and RUN_AT both derive from the export tree's own `created_at`; pass `DATE=` or `RUN_AT=` to override either. Guards before the copy: clean working tree, no freeze under that date already, no real names left in `exports/`, and a RUN_AT that is schema-current and paired with the export - one that predates it or lags it implausibly is refused, derived or passed. It takes the same `.export.lock` `export.sh` does, so it cannot copy a tree the cron is halfway through rewriting. Only then does it make the read-only dated copy and write `configs/eval/freeze.conf` - and if the copy or the `chmod` fails, the partial dated directory is removed rather than left for the next run's "already frozen" guard to mistake for a real freeze.
4. **Commit the pin.** Until it is committed, another checkout still resolves the old date.
5. **Regenerate on the clean tree**: `make eval-report eval-score eval-catalog`.
6. **Re-pin the bundle**, if one already exists. `repro-pin` refuses a pre-existing bundle directory and `pins.sha256` is generated rather than hand-edited, so: delete the old directory, re-pin, commit.
7. **Publish**: `make transfer-push SRC=data/annotation/exports-frozen/<date> PREFIX=exports-frozen/<date>`. To check Blob without pulling, download the remote `MANIFEST.sha256` and diff it against a freshly computed local one - comparing push's own printed hash is circular.

If the eval pragmata pin moves, the numbers must be re-derived under it, not assumed to carry over.

## Reading the numbers

Every column is defined in the [data dictionary](data-dictionary.md), which also says when a value is blank and how it was computed. What the numbers *mean* is here. For the three model-side CSVs, see [Synthetic evaluators](synthetic-evaluators.md#reading-the-numbers).

### Agreement (`annotation_label_summary.csv`)

**`alpha = 1.0` with `degenerate_calibration = True` is not evidence of reliability.** Alpha is `1 - Do/De` and is undefined when expected disagreement is zero - the label never varies in the overlap - and pragmata returns 1.0 there by convention. Read it beside `n_items_calibration` and the minority-class count.

### Metric estimates (`eval_metric_estimates.csv`)

- **The intervals cover sampling uncertainty over queries only** - not annotator disagreement, and not label error. A tight interval on a label whose alpha is at or below chance reads as precision that is not there; the `alpha_*` columns exist to stop that reading.
- **`n` is the filtered population, not the corpus.** Read it beside `n_panels_skipped`: for retrieval, incomplete panels are dropped before scoring, so `n` describes what survived rather than what was retrieved.

### The retrieval manifest

- **Row fan-out is per retrieved passage, one per document.** Because `chunk_id` is `<doc_id>-c1`, document *frequency* means counting rows, and distinct *documents* means deduplicating on `(query_id, doc_id)` - identical to deduplicating on `(query_id, chunk_id)`. **Never join on `doc_id` or `chunk_id` alone**: 739 of the 1092 retrieved documents are returned for more than one query (one of them for 72), so a document-only join multiplies rows across unrelated queries.
- **Joining to the annotation exports** needs the query text as the key, since the exports carry no `query_id`. It is verified 1:1 with `query_id` (1143 texts, 1143 ids, both directions). Once the query is resolved, join on `(query_id, chunk_id)`; join to `corpus_catalog.csv` on `doc_id`.

### The corpus catalog, and the fairness audit

- **Gender is inferred from a first-name dictionary** (`gender-guesser` 0.4.0). It is not recorded in the corpus and is not a measure of how anyone identifies. It is weaker on non-Western names, which is why the `_raw` columns keep `andy` (ambiguous) distinct from `unknown` (absent from the dictionary) - so coverage stays visible rather than being absorbed into one bucket.
- **`author_gender_collapsed = 'unknown'` merges two populations**: documents with no recorded author at all, and documents whose authors the dictionary cannot classify.
- **Do not collapse the author slots into a single list.** Two documents (`52109`, `53806`) record `verf1` and `verf3` with no `verf2`; a list of only the classified authors closes that hole and presents `verf3`'s verdict as the second author's. Cut by slot, or count across slots - never by list position.
- **Aggregate counts are one line off the slots.** Authors classified is the count of slots whose `_raw` is in {`female`, `mostly_female`, `male`, `mostly_male`}; "any female author" is whether any slot's `_raw` is in {`female`, `mostly_female`}.
- **"Majority" is over *recorded* authors.** The metadata holds at most three, so a twelve-author volume is judged on three.
