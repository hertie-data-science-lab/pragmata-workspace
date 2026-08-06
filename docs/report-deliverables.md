# Report deliverables and the pins behind them

The CSVs the BSt report is built from, the pins that make each number citable, and how to read the numbers themselves. Every column is defined in the [data dictionary](data-dictionary.md).

Producing a set: [the pins](#the-three-pins), then [Refreshing the numbers](#refreshing-the-numbers). Reading one: [Reading the numbers](#reading-the-numbers).

## The eight CSVs

Seven targets, eight CSVs - `eval-annotation-tables` emits two. All land under `reports/eval/<date>/` (`OUT=` to redirect that run root). One target per script, so each names its own output; `make eval-deliverables` runs all seven in order. They come in three subsets, named in the table below and used by those names throughout: the **human-annotation** and **fairness-audit** CSVs read the frozen export and the two corpora, while the **synthetic-evaluator** ones need what the GPU host produced ([Synthetic evaluators](synthetic-evaluators.md)).

| Target | Subset | Script | Output |
|---|---|---|---|
| `make eval-annotation-tables` | human-annotation | `annotation_tables.py` | `annotation_operations.csv`, `annotation_label_summary.csv` |
| `make eval-score-human` | human-annotation | `score_human_annotations.py` | `eval_metric_estimates.csv`, via `pragmata eval score` |
| `make eval-retrieval-manifest` | fairness-audit | `retrieval_manifest.py` | `retrieval_manifest.csv` |
| `make eval-catalog` | fairness-audit | `corpus_catalog.py` | `corpus_catalog.csv`, from the publikationsbot vector store (needs `az login`) |
| `make eval-score-synthetic POPULATION=<p>` | synthetic-evaluator | `score_synthetic_predictions.py` | `synthetic_metric_estimates.<p>.csv`, via `eval score --prediction-id` |
| `make eval-model-metrics` | synthetic-evaluator | `evaluator_report.py` | `evaluator_metrics.csv` |
| `make eval-model-calibration` | synthetic-evaluator | `evaluator_report.py` | `evaluator_calibration.csv` (needs the GPU environment) |

**The subset is a directory, not just a label.** Each script writes into its own subset, so a run produces the tiered layout the report is assembled from without anyone grouping a flat directory by hand:

```
reports/eval/<date>/
├── data-dictionary.md              one copy for the whole set - it defines all three subsets
├── human-annotation/               annotation_label_summary, annotation_operations,
│                                   eval_metric_estimates
├── fairness-audit/                 retrieval_manifest, corpus_catalog
└── synthetic-evaluator/            evaluator_metrics, evaluator_calibration,
                                    synthetic_metric_estimates.<population>
```

The mapping from output filename to subset lives once, in `DELIVERABLE_SUBSETS` in `scripts/lib/workspace.py`; a filename that is not in it is refused rather than filed at the run root, because a deliverable outside its subset would still be produced, hashed and pinned, and would only be noticed when the report was assembled. Anything hand-written for a run - an executive summary, a note - belongs at the run root beside the dictionary.

> The pinned 2026-08-06 set has this layout, assembled by hand at the time; from this change on the scripts produce it themselves. Its dictionary copy is named `eval-data-dictionary.md`, the name that file shipped under then.

Every CSV ships a `.provenance.json` - script, workspace SHA, pragmata pin, hashed inputs, parameters and seeds, snapshot identity, and the dictionary's hash - and the data dictionary is copied to the run root, one copy for the set whose records pin it. Every declared input is listed - one that was absent when the script ran appears as `"sha256": null, "missing": true`, never by omission.

## Reading the numbers

The dictionary says when a value is blank and how it was computed. What the numbers *mean* is here. For the three synthetic-evaluator CSVs, see [Synthetic evaluators](synthetic-evaluators.md#reading-the-numbers).

### Agreement (`annotation_label_summary.csv`)

**`alpha = 1.0` with `degenerate_calibration = True` is not evidence of reliability.** Alpha is `1 - Do/De` and is undefined when expected disagreement is zero - the label never varies in the overlap - and pragmata returns 1.0 there by convention. Read it beside `n_items_calibration`, and beside `n_true` against `n_items` - a label that is almost all-true or all-false has little for alpha to measure.

### Metric estimates (`eval_metric_estimates.csv`)

- **The intervals cover sampling uncertainty over queries only** - not annotator disagreement, and not label error. A tight interval on a label whose alpha is at or below chance reads as precision that is not there; the `alpha_*` columns exist to stop that reading.
- **`n` is the filtered population, not the curated corpus it was drawn from.** Read it beside `n_panels_skipped`: for retrieval, incomplete panels are dropped before scoring, so `n` describes what survived rather than what was retrieved. It is prevented during annotation, not at scoring: [`--tag-partial-panels`](IMPLEMENTATION-GUIDE.md#81-when-annotation-counts-as-done).

### The retrieval manifest

- **One row per retrieved passage, and one passage per document.** So how often a document was retrieved is a row count, while distinct *documents* means deduplicating on `(query_id, doc_id)` - or equivalently `(query_id, chunk_id)`, since `chunk_id` is just `<doc_id>-c1`. **Never join on `doc_id` or `chunk_id` alone**: 739 of the 1092 retrieved documents are returned for more than one query (one of them for 72), so a document-only join multiplies rows across unrelated queries.
- **Joining to the annotation exports** needs the query text as the key, since the exports carry no `query_id`. The query text is verified 1:1 with `query_id` (1143 texts, 1143 ids, both directions). Once the query is resolved, join on `(query_id, chunk_id)`; join to `corpus_catalog.csv` on `doc_id`.

### The corpus catalog, and the fairness audit

- **Gender is inferred from a first-name dictionary** (`gender-guesser` 0.4.0). Gender is not recorded in the corpus, and this is not a measure of how anyone identifies. The dictionary is weaker on non-Western names, which is why the `_raw` columns keep `andy` (ambiguous) distinct from `unknown` (absent from the dictionary) - so coverage stays visible rather than being absorbed into one bucket.
- **`author_gender_collapsed = 'unknown'` merges two populations**: documents with no recorded author at all, and documents whose authors the dictionary cannot classify.
- **Do not collapse the author slots into a single list.** Two documents (`52109`, `53806`) record `verf1` and `verf3` with no `verf2`; a list of only the classified authors closes that hole and presents `verf3`'s verdict as the second author's. Cut by slot, or count across slots - never by list position.
- **Compute the aggregates from the slot `_raw` columns, not from `author_gender_collapsed`.** The report's "authors classified" is the count of slots whose `_raw` is in {`female`, `mostly_female`, `male`, `mostly_male`}; its "any female author" is whether any slot's `_raw` is in {`female`, `mostly_female`}.
- **"Majority" is over *recorded* authors.** The metadata holds at most three, so a twelve-author volume is judged on three.

## The three pins

A published number has to be re-derivable from the same bytes and the same code, months later, by someone else. Nothing in this pipeline holds still on its own: the live Argilla instance keeps being annotated, the export tree is overwritten by the nightly cron, and `pragmata` moves upstream. So each report run cites three fixed inputs, and every `.provenance.json` records which.

1. **The frozen export tree** - `data/annotation/exports-frozen/<FREEZE_DATE>/`, a read-only (`chmod -R a-w`) copy cut by `make annotation-freeze`. The live `data/annotation/exports/` is overwritten by the 02:00 cron, so the report scripts never read it.
2. **The canonical log snapshot** - one line of `logs/annotation/log.jsonl`, chosen by its `run_at` timestamp rather than by being latest, and pinned by that line's sha256.
3. **The eval pragmata pin** - `PRAGMATA_EVAL_SRC` in `.env`, a checkout separate from the annotation pipeline's frozen demo pin, so the live instance's export behaviour stays fixed while eval tracks upstream. It cannot be a git dependency like the annotation pin, because two commits of one package cannot coexist in one venv - so it stays a path in `.env` and shadows the installed package on `PYTHONPATH` at call time.

The first two live in [`configs/eval/freeze.conf`](../configs/eval/freeze.conf) as `FREEZE_DATE` and `CANONICAL_SNAPSHOT_RUN_AT` - the pin as data rather than as code, written by `make annotation-freeze` for the operator to commit. `eval_common.py` reads that file, so a refresh moves the pin once for every script. The scripts refuse to run when the pin is not the newest freeze on disk; pass `--exports` to read a non-canonical tree on purpose.

The environment is pinned too: `uv.lock` freezes all 126 packages at the versions that produced these numbers - see the `constraint-dependencies` comment in `pyproject.toml`.

## Where files go: `data/eval/` vs `data/eval-inputs/`

`data/eval/` is pragmata's tool tree and holds only what pragmata wrote there (`scores/`, `train_outputs/`, `prediction_outputs/`). Workspace-produced inputs *to* the tool - the pooled CSVs `score_human_annotations.py` hands to `eval score --path`, and the staged unlabelled CSVs `predict_evaluators.py` hands to `predict-labels` - go in `data/eval-inputs/`.

Two files this workspace writes *into* that tree are deliberate exceptions, marked by a `.workspace.` infix: `train_provenance.workspace.json` and `predict_provenance.workspace.json`, each inside the run directory it describes. Those directories are what gets pushed off the GPU box, and neither pragmata's nor tlmtc's own sidecars name the workspace commit, the staged input or the freeze behind it.

## Refreshing the numbers

The runbook for producing a new set of these CSVs. It moves all three pins and regenerates every deliverable behind them, so it is the procedure for a report refresh rather than for a routine export. Order matters: each `.provenance.json` records the commit it was generated at and the bundle pins them by hash, so code changes and re-runs must not interleave.

1. **Commit any code changes first.** Nothing below is valid from a dirty tree.
2. **Export and snapshot**: `make annotation-export`, then `make annotation-log`.
3. **Freeze and write the pin**: `make annotation-freeze`. DATE and RUN_AT both derive from the export tree's own `created_at`; pass `DATE=` or `RUN_AT=` to override either. Guards before the copy: clean working tree, no freeze under that date already, no real names left in `exports/`, and a RUN_AT that is schema-current and consistent with the export - one earlier than the export, or implausibly later, is refused whether it was derived or passed in. It takes the same `.export.lock` that `export.sh` takes, so it cannot copy a tree the cron is halfway through rewriting. Only then does it make the read-only dated copy and write `configs/eval/freeze.conf`. A failed copy or `chmod` removes the partial dated directory, so a later run cannot mistake it for a real freeze.
4. **Commit the pin.** Until it is committed, another checkout still resolves the old date.
5. **Regenerate on the clean tree.** `make eval-deliverables` runs all seven, but the synthetic-evaluator three need the GPU host's `train_outputs/` and `prediction_outputs/` copied in first ([Getting the data in and out](synthetic-evaluators.md#getting-the-data-in-and-out)). For a human-label-only refresh, run the human-annotation and fairness-audit targets: `make eval-annotation-tables eval-retrieval-manifest eval-score-human eval-catalog`.
6. **Re-pin the bundle**, if one already exists. `repro-pin` refuses a pre-existing bundle directory and `pins.sha256` is generated rather than hand-edited, so: delete the old directory, re-pin, commit.
7. **Publish**: `make transfer-push SRC=data/annotation/exports-frozen/<date> PREFIX=exports-frozen/<date>`. To check Blob without pulling, download the remote `MANIFEST.sha256` and diff it against a freshly computed local one - comparing push's own printed hash is circular.

If the eval pragmata pin moves, the numbers must be re-derived under it, not assumed to carry over.
