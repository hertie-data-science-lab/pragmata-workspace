# Eval pipeline

The evaluation pipeline is a sibling of the [annotation pipeline](annotation.md). Two of its
three parts are wired up in this workspace: **data transport**
([Eval data transport](eval-data-transport.md)) and **scoring human labels** (this page).
**Training and prediction** (`pragmata eval train-evaluator|predict-labels`) are implemented
in `pragmata` and run on the GPU box, but have no workspace-side glue - see
[No workspace glue yet](#no-workspace-glue-yet).

## Deliverables

Three targets produce the report deliverables into `reports/eval/<date>/` (`OUT=` to
redirect). Every CSV ships with a `.provenance.json` file, and the
[data dictionary](eval-data-dictionary.md) is copied beside the CSVs whose
`.provenance.json` pins it:

| Target | Script | Output |
|---|---|---|
| `make eval-report` | `annotation_tables.py` | `annotation_operations.csv`, `annotation_label_summary.csv` |
| `make eval-report` | `retrieval_manifest.py` | `retrieval_manifest.csv` |
| `make eval-score` | `score_human_annotations.py` | `eval_metric_estimates.csv`, via `pragmata eval score` |
| `make eval-catalog` | `corpus_catalog.py` | `corpus_catalog.csv`, from the publikationsbot vector store (needs `az login`; runs via `uv`, not the workspace venv) |

`scripts/eval/` also holds `vectorstore_inventory.py` (aggregate corpus counts, to stdout)
and `eval_common.py` (shared vocabulary and filters, not runnable).

**Vocabulary.** `response`, `record`, `item`, `panel` and `query group` are defined in the
[data dictionary](eval-data-dictionary.md), together with every column of every CSV.
`eval_common.py` is the executable half of that document; the two are kept in step. Do not
redefine the terms anywhere else.

**A `.provenance.json` records identity, not explanation**: script, workspace SHA,
pragmata pin, hashed inputs, parameters and seeds, snapshot identity, and the dictionary's
hash. What a number means lives in the dictionary the hash pins.

## The three pins

Every report number is derived from pinned inputs, so a re-run months later reads exactly
what the original run read:

1. **The frozen export tree** - `data/annotation/exports-frozen/<FREEZE_DATE>/`, a read-only
   (`chmod -R a-w`) copy of one night's export, cut by `make annotation-freeze`. The live
   `data/annotation/exports/` is overwritten by the 02:00 cron and is never read by the
   report scripts.
2. **The canonical log snapshot** - one line of `logs/annotation/log.jsonl`, selected by its
   `run_at` timestamp (not "the latest") and pinned by the sha256 of that single line; the
   log is append-only, so a whole-file hash would change nightly and pin nothing.
3. **The eval pragmata pin** - `PRAGMATA_EVAL_SRC` in `.env`, a checkout separate from the
   annotation pipeline's frozen demo pin, so the live instance's export behaviour stays
   fixed while eval tracks upstream. The annotation pin is a git dependency installed by
   `uv sync`; the eval pin cannot be, because two commits of one package cannot coexist in
   one venv - so it stays a path in `.env` and shadows the installed package on
   `PYTHONPATH` at call time. One venv runs both: `pandera`, the only thing eval needs
   beyond the annotation side, is a workspace dependency in `pyproject.toml`.

   The **environment** is pinned too: `uv.lock` freezes all 126 packages at the versions
   that produced these numbers, `numpy`/`scipy` included, since the alpha bootstrap runs
   on them. See the `constraint-dependencies` comment in `pyproject.toml`.

The first two are `FREEZE_DATE` and `CANONICAL_SNAPSHOT_RUN_AT` in
[`configs/eval/freeze.conf`](../configs/eval/freeze.conf) - the pin as data rather than as
code, so `make annotation-freeze` writes it and the operator only commits it.
`eval_common.py` reads that file, so a refresh moves the pin once for all three scripts.
The scripts also **refuse to run when the pin is not the newest freeze on disk**: a stale
pin would otherwise publish the previous dataset in silence, which is the one failure in
this chain that is not loud (a *missing* freeze raises `no such export tree`). Pass
`--exports` to read a non-canonical tree on purpose. The current freeze
is recorded in [`reproducibility/2026-07-31-eval-report/`](../reproducibility/2026-07-31-eval-report/),
whose README names the exact pragmata pin commit. It pins the same export tree as
`2026-07-30-eval-report/`, which it supersedes: the export did not move, the report schema
did.

## Annotator identities

`annotator_id` **is** pseudonymous - as of 2026-07-30, and not before. pragmata writes the
Argilla *username* into it, and the usernames on this instance are `firstname.lastname`, so
every export before that date carried real names in every task CSV and in the
`pairwise_kappa` keys of `iaa/report.json`. Two mechanisms keep names out now:

- `scripts/annotation/pseudonymize_export.py` runs as part of every export (`export.sh`,
  fatal on failure) and rewrites both surfaces to the annotator's Argilla user id - stable
  across exports, so cross-snapshot comparison still works.
- `transfer-push` and `annotation-freeze` independently refuse any tree whose `annotator_id`
  values or IAA pairwise keys are not UUIDs, so a tree that skipped the rewrite can neither
  leave the box nor be immortalised in a freeze. One check, `scripts/lib/check_pseudonymised.py`,
  at both boundaries.

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

The order matters: each `.provenance.json` records the workspace commit it was generated
at, and the reproducibility bundle pins them by hash, so code changes and re-runs must
not interleave. The sequence that works:

1. **Finish and commit all code changes first.** Never rewrite history (`reset`, `amend`)
   after generating the provenance files - they would name a commit that no longer exists.
2. **Take the export and snapshot**: `make annotation-export` (pseudonymises as it goes),
   then `make annotation-log`; note the new snapshot's `run_at`.
3. **Freeze the tree and write the pin**:
   `make annotation-freeze DATE=<date> RUN_AT=<the run_at from step 2>`. Everything is a
   guard until the copy - clean working tree, no freeze under that date already, `RUN_AT`
   really present in the log, no real names left in `exports/` - and only then does it make
   the read-only dated copy (`chmod u+w` parent, copy, `chmod -R a-w` the new dir,
   `chmod a-w` the parent again) and write `configs/eval/freeze.conf`.
4. **Commit the pin.** The target stops one step short on purpose: a script must not make
   this commit, because step 1 forbids rewriting history once provenance files name a
   commit. Until it is committed, another checkout still resolves the old date - which is
   what the staleness guard above catches.
5. **Regenerate everything on the clean tree**: `make eval-report eval-score eval-catalog`.
   A re-run rewrites them, so this must happen after the last code commit.
6. **Re-pin the bundle.** `repro-pin` refuses a pre-existing bundle dir and `pins.sha256`
   is generated, never hand-edited - so delete the old bundle dir, re-pin, restore the
   hand-written bundle README, and commit.
7. **Publish**: `make transfer-push SRC=data/annotation/exports-frozen/<date> PREFIX=exports-frozen/<date>`.
   To verify Blob without a pull, download the remote `MANIFEST.sha256` and diff it against
   a freshly computed local manifest - comparing push's own printed hash is circular.

If the eval pragmata pin moves (e.g. upstream PRs land in modified form), the numbers must
be **re-derived** under the new pin, not assumed to carry over.

## No workspace glue yet

`pragmata eval train-evaluator|predict-labels` are implemented in the pragmata repo, behind
the `eval` extra (`pragmata[eval]` → `tlmtc[train]`), and run on the GPU box against staged
export CSVs. What does not exist is the workspace side: no make targets, no eval configs
(`configs/eval/` holds the freeze pin and nothing else), no tested procedure - see
[implementation guide §10](implementation-guide.md#10-run-the-evaluation) for the open list.
When that glue lands it mirrors the annotation pipeline (`scripts/eval/` ↔
`scripts/annotation/`, `configs/eval/` ↔ `configs/annotation/`) and writes to `data/eval/`.
`score_synthetic_predictions.py` is the reserved name for scoring the evaluator model's
predictions - the twin of `score_human_annotations.py` - and is deliberately not stubbed.
