# Eval pipeline

The evaluation pipeline is a sibling of the [annotation pipeline](annotation.md). Two of its
three parts have shipped: **data transport** ([Eval data transport](eval-data-transport.md))
and **scoring human labels** (this page). **Training and prediction** (`pragmata eval
train|predict`) are not built yet - see [Not built yet](#not-built-yet).

## Deliverables

Three targets produce the report deliverables into `reports/eval/<date>/` (`OUT=` to
redirect). Every CSV ships with a `.provenance.json` sidecar, and the
[data dictionary](eval-data-dictionary.md) is copied beside the CSVs whose sidecars pin it:

| Target | Script | Output |
|---|---|---|
| `make eval-report` | `annotation_tables.py` | `annotation_operations.csv`, `annotation_label_summary.csv` |
| `make eval-report` | `retrieval_manifest.py` | `retrieval_manifest.csv` |
| `make eval-score` | `score_human_annotations.py` | `eval_metric_estimates.csv`, via `pragmata eval score` |
| `make eval-catalog` | `corpus_catalog.py` | `corpus_catalog.csv`, from the publikationsbot vector store (needs `az login`; runs via `uv`, not the workspace venv) |

`scripts/eval/` also holds `vectorstore_inventory.py` (aggregate corpus counts, to stdout)
and `eval_common.py` (shared vocabulary and filters, not runnable).

**Vocabulary.** `response`, `record`, `unit`, `panel` and `query group` are defined in the
[data dictionary](eval-data-dictionary.md), together with every column of every CSV.
`eval_common.py` is the executable half of that document; the two are kept in step. Do not
redefine the terms anywhere else.

**Sidecars record identity, not explanation**: script, workspace SHA, pragmata pin, hashed
inputs, parameters and seeds, snapshot identity, and the dictionary's hash. What a number
means lives in the dictionary the hash pins.

## The three pins

Every report number is derived from pinned inputs, so a re-run months later reads exactly
what the original run read:

1. **The frozen export tree** - `data/annotation/exports-frozen/<FREEZE_DATE>/`, a read-only
   (`chmod -R a-w`) copy of one night's export. The live `data/annotation/exports/` is
   overwritten by the 02:00 cron and is never read by the report scripts.
2. **The canonical log snapshot** - one line of `logs/annotation/log.jsonl`, selected by its
   `run_at` timestamp (not "the latest") and pinned by the sha256 of that single line; the
   log is append-only, so a whole-file hash would change nightly and pin nothing.
3. **The eval pragmata pin** - `PRAGMATA_EVAL_SRC` in `.env`, a checkout separate from the
   annotation pipeline's frozen demo pin (`PRAGMATA_SRC`), so the live instance's export
   behaviour stays fixed while eval tracks upstream. One venv runs both: the workspace venv
   carries `pandera`, and the pin shadows the installed pragmata via `PYTHONPATH`.

The first two are constants in `scripts/eval/eval_common.py` (`FREEZE_DATE`,
`CANONICAL_SNAPSHOT_RUN_AT`) - one place, so a refresh moves them once. The current freeze
is recorded in [`reproducibility/2026-07-30-eval-report/`](../reproducibility/2026-07-30-eval-report/),
whose README names the exact pragmata pin commit.

## Annotator identities

`annotator_id` **is** pseudonymous - as of 2026-07-30, and not before. pragmata writes the
Argilla *username* into it, and the usernames on this instance are `firstname.lastname`, so
every export before that date carried real names in every task CSV and in the
`pairwise_kappa` keys of `iaa/report.json`. Two mechanisms keep names out now:

- `scripts/annotation/pseudonymize_export.py` runs as part of every export (`export.sh`,
  fatal on failure) and rewrites both surfaces to the annotator's Argilla user id - stable
  across exports, so cross-snapshot comparison still works.
- `transfer-push` independently refuses to upload any tree whose `annotator_id` values or
  IAA pairwise keys are not UUIDs, so a tree that skipped the rewrite cannot leave the box.

The rewrite is forward-only: `exports-frozen/2026-07-29/` predates it, still holds names,
and stays local. Exports still count as PII either way - the free-text `notes` and
`discard_notes` columns are annotator-authored and unreviewed.

## Ownership

`data/eval/` is pragmata's own tool tree and holds only what pragmata wrote there
(`scores/`, later `checkpoints/` and `predictions/`). Workspace-produced inputs *to* the
tool - the pooled, filtered CSVs `score_human_annotations.py` hands to
`eval score --path` - are staged in `data/eval-inputs/`. Eval consumes staged input by
explicit path only; nothing is inferred from prior tool outputs. See
[`data/README.md`](../data/README.md).

## Cutting a new freeze

The order matters: sidecars record the workspace commit they were generated at, and the
reproducibility bundle pins the sidecars by hash, so code changes and re-runs must not
interleave. The sequence that works:

1. **Finish and commit all code changes first.** Never rewrite history (`reset`, `amend`)
   after generating sidecars - they would name a commit that no longer exists.
2. **Take the export and snapshot**: `make annotation-export` (pseudonymises as it goes),
   then `make annotation-log`; note the new snapshot's `run_at`.
3. **Freeze the export tree.** The `exports-frozen/` parent is write-protected:
   `chmod u+w` the parent, copy `exports/` to `exports-frozen/<date>/`, `chmod -R a-w` the
   new dir, `chmod a-w` the parent again.
4. **Move the pins**: update `FREEZE_DATE` and `CANONICAL_SNAPSHOT_RUN_AT` in
   `eval_common.py`, commit.
5. **Regenerate everything on the clean tree**: `make eval-report eval-score eval-catalog`.
   A re-run rewrites the sidecars, so this must happen after the last code commit.
6. **Re-pin the bundle.** `repro-pin` refuses a pre-existing bundle dir and `pins.sha256`
   is generated, never hand-edited - so delete the old bundle dir, re-pin, restore the
   hand-written bundle README, and commit.
7. **Publish**: `make transfer-push SRC=data/annotation/exports-frozen/<date> PREFIX=exports-frozen/<date>`.
   To verify Blob without a pull, download the remote `MANIFEST.sha256` and diff it against
   a freshly computed local manifest - comparing push's own printed hash is circular.

If the eval pragmata pin moves (e.g. upstream PRs land in modified form), the numbers must
be **re-derived** under the new pin, not assumed to carry over.

## Not built yet

`pragmata eval train|predict` (the `tlmtc` extra) run on the GPU box and are a separate
effort in the pragmata repo. When they land, they mirror the annotation pipeline
(`scripts/eval/` ↔ `scripts/annotation/`, `configs/eval/` ↔ `configs/annotation/`) and
write to `data/eval/`. `score_synthetic_predictions.py` is the reserved name for scoring
the evaluator model's predictions - the twin of `score_human_annotations.py` - and is
deliberately not stubbed.
