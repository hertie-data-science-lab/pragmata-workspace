# Evaluation-stage scripts

The scripts here, and what each writes into `reports/eval/<date>/`:

| Script | Output |
|---|---|
| `annotation_tables.py` | `annotation_operations.csv`, `annotation_label_summary.csv` |
| `score_human_annotations.py` | `eval_metric_estimates.csv` (via `pragmata eval score`) |
| `retrieval_manifest.py` | `retrieval_manifest.csv` |
| `corpus_catalog.py` | `corpus_catalog.csv` |
| `vectorstore_inventory.py` | aggregate corpus counts, to stdout |
| `eval_common.py` | shared vocabulary and filters; not runnable |

`score_synthetic_predictions.py` is reserved for the evaluator-model run and does not exist
yet — the human-label half is what has shipped.

**Vocabulary.** `response`, `record`, `unit`, `panel` and `query group` are defined in
[`docs/eval-data-dictionary.md`](../../docs/eval-data-dictionary.md), together with every
column of every CSV. `eval_common.py` is the executable half of that document; the two are
kept in step. Do not redefine these terms here.

Pragmata's `eval` tool fine-tunes an evaluator (via `tlmtc`) which must run on a
**GPU-enabled device** (for us, this is on the Hertie network), while the annotation exports it consumes live
on this **CPU-backed VM** (for us, this is in BSt's Azure tenant). The two are in different
organisations with no route to each other, so eval data moves over a shared
**Azure Blob** container both sides reach over HTTPS.

Pragmata writes eval artifacts to `data/eval/` via `tool_root('eval')`, a sibling
of `data/annotation/` and `data/querygen/`. Configs live in `configs/eval/`.

## Two planes

```
   CONTROL PLANE  git → GitHub (code only, same SHA both sides)
   CPU VM (BSt) ◄──── clone ────► GPU VM (Hertie)
        │                              │
        │   az blob upload/download    │
        ▼          (HTTPS 443)         ▼
     ┌────────────────────────────────────┐
     │  Azure Blob container (BSt-owned)   │
     │  exports/ (CPU→GPU)                 │
     │  predictions/, checkpoints/ (→CPU)  │
     └────────────────────────────────────┘
```

Neither VM reaches the other; both reach Blob. Direct box-to-box is blocked
structurally: the two VNets are not peered, so there is no route between them.

## `sync.sh` - the pipe

Lives in `scripts/transfer/`, not here: it moves any tree and is not eval-specific.

```
sync.sh push <src> <prefix>    # CPU→Blob: upload a tree + a sha256 manifest
sync.sh pull <prefix>          # Blob→box: download into data/transfer/<prefix>/, then verify
sync.sh verify <prefix>        # re-check data/transfer/<prefix>/ against its manifest
```

Every `push` writes a sorted per-file `sha256` manifest to `<prefix>/MANIFEST.sha256`
and prints a one-line **snapshot pin** (a single hash for the whole tree) for a
future reproducibility bundle. Every `pull` re-runs `sha256sum -c` on the
receiving end and fails loudly on any mismatch.

Driven from the `Makefile`:

```
make transfer-push SRC=<tree> PREFIX=<p>  # upload <tree> to blob <prefix>/ (+ manifest, pin)
make transfer-pull PREFIX=predictions     # blob predictions/ → data/transfer/predictions/ (+ verify)
make transfer-verify PREFIX=exports       # re-check a pulled tree against its manifest
```

## Ownership invariant (staging)

`sync.sh` **reads** pragmata tool trees (`data/annotation/`, `data/eval/`) in
place and **writes only** to `data/transfer/` on the receiving box - never inside
a tool's own output tree. `pull` refuses any destination outside `data/transfer/`.
So every pragmata tool tree holds only data that tool produced, and a tool
resetting its own dir cannot clobber received data.

The same invariant runs the other way. `score_human_annotations.py` stages the pooled,
filtered CSVs it hands to `eval score --path` in `data/eval-inputs/`, **not** `data/eval/`:
those are workspace-produced inputs *to* the tool, and `data/eval/` holds only what
pragmata itself wrote there (`scores/`, and later `checkpoints/`, `predictions/`).

Eval then consumes staged input by **explicit path** - its `labeled_data_path` /
`unlabeled_data_path` are explicit by design ("not inferred from prior tool
outputs"), e.g.
`pragmata eval train --labeled-data-path data/transfer/exports/<topic>/<task>.csv`.
See `data/transfer/README.md`.

## Data sensitivity

`annotator_id` **is** pseudonymous - as of 2026-07-30, and not before. pragmata writes the
Argilla *username* into it, and the usernames on this instance are `firstname.lastname`, so
every export up to that date carried real names in every CSV and in the `pairwise_kappa`
keys of `iaa/report.json`. `scripts/annotation/pseudonymize_export.py` now runs as part of
every export and rewrites both surfaces to the annotator's Argilla user id, which is stable
across exports so cross-snapshot comparison still works.

Exports still count as PII (`data/README.md`, "never commit"): the free-text `notes` and
`discard_notes` columns are annotator-authored and unreviewed. They ship into a **private,
IP-allowlisted** container; the roster (`configs/annotation/users.json`) is gitignored and
the GPU box never needs it - eval consumes label columns, not identities.

Frozen trees keep whatever they were frozen with: the rewrite is forward-only, so
`exports-frozen/2026-07-29/` still holds names and stays local.

## GPU work is disposable, except checkpoints

Any eval run is replayable from pinned inputs + pinned code, so the GPU-based work is
disposable - **except trained checkpoints**, which are expensive to reproduce and
must be `pull`ed off and pinned to a durable home before the box is torn down.
