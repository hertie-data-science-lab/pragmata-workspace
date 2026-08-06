# 2026-08-06-eval-report

kind: freeze
fetch: `make transfer-pull PREFIX=exports-frozen/2026-07-30` (the export tree) and
`make transfer-pull PREFIX=checkpoints/<run_id>` for each of the three training runs below,
and `make transfer-pull PREFIX=reports/eval/2026-08-06` (the report tree, snapshot
`sha256:319d04fb62398676c5c0283950630fd9da17d4ddc201ab42263d720fa7d34769`). The report tree is
also regenerable: check out the workspace SHA recorded in each `.provenance.json` on a clean
tree and run `make eval-deliverables`.

The **canonical data for the BSt report**, superseding `2026-07-31-eval-report/`, which the
same commit that adds this bundle deletes. Every human-annotation, fairness-audit and
synthetic-evaluator number in the report is derived from this freeze and no other.

This is a **freeze**, not a lineage stage (see [`../README.md`](../README.md)): it records one
run and is never replayed. `repro-reproduce` refuses it.

**What** — the artefacts behind the 2026-08-06 deliverable set, in three parts:

- the **annotation export** it rests on — `data/annotation/exports-frozen/2026-07-30/`
  (41 files), unchanged from the tree its predecessor pinned. Recomputing the tree manifest
  the way `scripts/transfer/sync.sh` does still gives `f33aff2a…`, so the export bytes are
  the same bytes the 2026-07-31 publication was derived from;
- the **report deliverables** — `reports/eval/2026-08-06/` (20 files), grouped
  `human-annotation/`, `fairness-audit/` and `synthetic-evaluator/`, each CSV beside its
  `.provenance.json`, plus `executive-summary.md` and the data dictionary that defines the
  columns; and
- the **three evaluator training runs** — `data/eval/train_outputs/<run_id>/` (61 files).
  Per run: `pragmata_train.meta.json`, `train_run_meta.json`,
  `train_provenance.workspace.json`, the `evaluation/` metrics and plots, the `model/`
  adapter weights and tokenizer, and the `data/` split parquets.

**Why** — it is the first bundle to pin the synthetic evaluators alongside the human
annotation record, so the two halves of the report are pinned as one set. If the run
directories drift, the evaluator numbers in the report stop being attributable to any
particular adapter.

**How to verify** — `make repro-verify PIN=2026-08-06-eval-report`

## What the run directories pin, and what they leave out

Following the precedent of the earlier freezes, the pinned run directories carry the
**citable record only**: meta, provenance, the evaluation JSONs and plots, the adapter
weights and the split parquets. The trainer `logs/` subtree — the HPO checkpoint ladder and
the Optuna trial database, 148 MB for retrieval alone — is **excluded**, as is the transfer
`MANIFEST.sha256`, which pins tree-relative paths and belongs to the Blob tree rather than
to this bundle. The complete trees, `logs/` included, stay at their Blob prefixes.

## Evaluator training and prediction artefacts

Three training runs, one per task: **retrieval** `a3e4b21886db4fbd85dd498f68f7a4d3`
(`roc_auc_macro` 0.710), **grounding** `294541b61b0a456caaf96c5174f808cd`, **generation**
`9e59a57291314731bfbfc655527c3028` — all three on `jhu-clsp/mmBERT-base`, seed 42, trained
against the 2026-07-30 freeze. Checkpoints are mirrored at Blob prefix
`checkpoints/<run_id>`, and the predictions each evaluator produced at `predictions/`
(`<run_id>-annotated`, `<run_id>-corpus`, `<run_id>-testsplit`). The retrieval 0.710 is a
single unselected draw of the final configuration on pinned code and the frozen export;
`executive-summary.md` records why it, and not the higher best-of-ladder figure from an
export tree that no longer exists, is the reproducible benchmark.

## Alpha CI supersession

The Krippendorff-alpha confidence bounds published on 2026-07-31 came from an **unseeded**
200-resample bootstrap and were not reproducible. This bundle's
`annotation_label_summary.csv` carries **seeded** bounds — seed 0, 1000 resamples, ci 0.95,
recorded in its `.provenance.json` — and those supersede them. Only the bounds moved: all
point estimates reproduce the 2026-07-31 publication exactly.

## Provenance

All pinned artefacts were produced at workspace commit `4a44a05` (clean `main`) — every
`.provenance.json` and `train_provenance.workspace.json` in this bundle records that SHA
with `dirty: false`. This bundle is committed at a later docs-only commit; **reruns should
check out the SHA recorded in each artefact's provenance record, not this bundle's commit.**
The `dirty: True` the pin step reports for itself is this directory being created, nothing
else.

One consequence of that gap is visible in the pins: the artefacts name the data dictionary
`eval-data-dictionary.md`, the name it shipped under at `4a44a05`. The docs-only commit
renamed it to `docs/data-dictionary.md`. The pin names the bytes that shipped, under the
name they shipped as; that is correct and is not a mismatch.

## Pins

| Pin | Value |
|---|---|
| Files | 122 (41 export + 20 report + 61 training) |
| Workspace git | `3ae5246d306caef418271a3c469810d6c1d6eac2` (dirty: True) |
| pragmata git | `94e821965eaa7f3cc7a4951d35e1603604dd48f0` |
| Artefact workspace git | `4a44a05a5e389c9b88e303f9a81c3b666f2cf54e`, branch `main`, clean |
| pragmata eval pin | branch `pin/eval-report-2026-07`, SHA `f0e355e`, clean |
| Log snapshot | `run_at = 2026-07-30T12:41:38.450281+00:00`, sha256 `cb1df09f…` of that one line |
| IAA recomputation | seed 0, 1000 resamples, ci 0.95 |
| Blob snapshot, export tree (sha256 of `MANIFEST.sha256`) | `f33aff2a3baaa25df9cf60043f0c2fb60d8b49012c5d0543514781652bd4bdef` (41 files) |
| Blob snapshot, `checkpoints/a3e4b21886db4fbd85dd498f68f7a4d3` | `fcd4a2ec1ea1c24301a51a4060b2c77d4dd25a40af270f60422b4e727dd48d56` |
| Blob snapshot, `checkpoints/294541b61b0a456caaf96c5174f808cd` | `51c93889245a667a44eb9ccb28e733b5902900639d5aed149fe8d6e0bb2d8b4a` |
| Blob snapshot, `checkpoints/9e59a57291314731bfbfc655527c3028` | `5596ba0594a34a8c24c1ee6755fd52658168122514cb7ecfa00e70bb741020c7` |
| Blob snapshot, `predictions/` | `9a4f6f603c4f85e6dc4fd10224217faf110338c6bbf9ac0521156bf053d05629` |

`zentrum-fuer-datenmanagement` is in the export tree (70 imported panels, zero annotations)
but **excluded from every report table**, recorded in `excluded_programmes` in every
`.provenance.json`. An all-blank row reads as a measurement rather than as an absence.

`pins.sha256` and the Blob `MANIFEST.sha256` files are not comparable by design: the first
lists repo-relative paths, the second tree-relative ones. Each pins the same bytes under its
own naming.

`reports/` and `data/` are gitignored, so on a fresh clone every pin reads **ABSENT** until
the trees are fetched or regenerated with the `fetch:` commands above. `MISMATCH` always
means something is wrong.
