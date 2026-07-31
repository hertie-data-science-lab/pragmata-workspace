# Eval report freeze - 2026-07-31

kind: freeze
fetch: `make transfer-pull PREFIX=exports-frozen/2026-07-30` (the export tree) and
`make transfer-pull PREFIX=reports/eval/2026-07-31` (the report deliverables). The CSVs are
also regenerable: check out the workspace SHA below on a clean tree and run
`make eval-report eval-score eval-catalog`.

The **canonical data for the BSt report**, superseding
[`../2026-07-30-eval-report/`](../2026-07-30-eval-report/). Every human-annotation and
fairness-audit number in the report is derived from this freeze and no other.

This is a **freeze**, not a lineage stage (see [`../README.md`](../README.md)): it records one
run and is never replayed. `repro-reproduce` refuses it.

It pins the same two halves as its predecessor:

- the **annotation export** it rests on - `data/annotation/exports-frozen/2026-07-30/`,
  unchanged from that bundle, and
- the **report CSVs themselves**, with their `.provenance.json` files and the data dictionary
  that defines their columns.

## Why it supersedes 2026-07-30

**The data did not move; the schema did.** This bundle pins the identical export tree, the
identical log snapshot and the identical eval pragmata pin. What changed is the shape of the
deliverables and the wording of the document that defines them, so the two sets are not
interchangeable and the earlier one no longer matches the dictionary:

1. **One name per concept.** `n_units_annotated` becomes `n_items_annotated`; the vocabulary
   uses `item` throughout, where three of four columns already said items.
2. **Columns say what they measure.** `annotated` becomes `panel_started` (it is true when
   *one* chunk of a panel has a response, not when the panel is done), `n_annotated_chunks`
   becomes `n_chunks_annotated`, and `domain` is gone - it was verified 1:1 with `programme`.
3. **`corpus_catalog.csv` carries bibliographic identity**: `title` (catalogue non-filing
   markers stripped), `subtitle`, `doi`, `catalog_url`. Without a title the fairness audit is
   a table of opaque `doc_id`s.
4. **Author gender is per slot.** The old `author_genders_raw` held only the authors whose
   names parsed, so a hole shifted every later author left: two documents (`52109`, `53806`)
   record `verf1` and `verf3` with no `verf2`, and that string's second position was `verf3`'s
   verdict presented as the second author's. Now one `_raw`/`_collapsed` pair per slot,
   aligned to `verf1..verf3`. `n_authors_resolved`, `female_present` and the joined string are
   gone as restatements.
5. **`retrieval_manifest.csv` carries `score`**, the retriever's own relevance value. It is
   **blank in this freeze**: the value was dropped at normalisation for these runs and the bot
   response is not retained, so it cannot be back-filled. Populated from the next bot run
   onward.

Every number that appears in both sets is unchanged: `annotation_label_summary.csv` and
`eval_metric_estimates.csv` regenerate byte-identical, `annotation_operations.csv` differs
only in one header, and `retrieval_manifest.csv` only in the renames and the dropped column.

The 2026-07-30 bundle is retained as an archived record and still verifies; its pinned
artefacts are untouched.

## Pins

| Pin | Value |
|---|---|
| Files pinned | 52 (41 export + 11 report) |
| Workspace git | `5bbc9a5906f6af9763932b2d8d3528e781e63b51`, branch `main` |
| pragmata annotation pin | `94e821965eaa7f3cc7a4951d35e1603604dd48f0` (the `git+ssh` dependency in `pyproject.toml`) |
| pragmata eval pin | branch `pin/eval-report-2026-07`, SHA `f0e355e`, clean |
| Log snapshot | `run_at = 2026-07-30T12:41:38.450281+00:00`, sha256 `cb1df09f…` of that one line, IAA 1000 resamples / seed 0, session gap 1800s |
| Blob snapshot, export tree (sha256 of `MANIFEST.sha256`) | `f33aff2a3baaa25df9cf60043f0c2fb60d8b49012c5d0543514781652bd4bdef` (41 files) |
| Blob snapshot, report tree (sha256 of `MANIFEST.sha256`) | `92001e32bbe60d6ee2fb916ee5a476b86e33dc333561696db1821304814c83c6` (11 files) |

All five deliverables were generated on a **clean tree** at that workspace SHA - each
`.provenance.json` records `dirty: false` - and this bundle was pinned immediately afterwards.
The `dirty: True` the pin step reports for itself is this directory being created, nothing
else. The report tree is pushed to `reports/eval/2026-07-31/` in the Blob container, a prefix
introduced by this freeze; the export tree keeps its 2026-07-30 prefix, since it is the same
tree.

The pragmata eval pin is `origin/main` plus the two eval-score PRs the scoring depends on
(#305 panel-completeness skipping, #304 score-by-path), both **pending review upstream**. If
they land in modified form, the numbers must be re-derived from the merged code, not assumed
to carry over. Every report `.provenance.json` records the pin's branch, SHA and clean/dirty
state.

The snapshot's identity is pinned transitively: `annotation_operations.csv.provenance.json`
carries the sha256 of that one log **line** - not of the append-only log, whose whole-file
digest changes nightly - and that file is itself pinned here.

`pins.sha256` and the Blob `MANIFEST.sha256` are not comparable by design: the first lists
repo-relative paths, the second tree-relative ones. Each pins the same bytes under its own
naming.

## Cutoff and totals

Unchanged from the export this freeze rests on. Export `created_at` runs
`2026-07-30T12:38Z` - `12:39Z`; paired log snapshot at `12:41:38Z`. Instance totals at cutoff:
**3468 submitted responses, 2516 completed records of 4244, 35 annotators**, 181 complete
retrieval panels.

`zentrum-fuer-datenmanagement` is in the export tree (70 imported panels, zero annotations)
but **excluded from every report table** - decided 2026-07-30, and recorded in
`excluded_programmes` in every `.provenance.json`. An all-blank row reads as a measurement
rather than as an absence.

## How to verify

    make repro-verify PIN=2026-07-31-eval-report

`reports/` and `data/` are gitignored, so on a fresh clone every pin reads **ABSENT** until
the trees are fetched with the `fetch:` commands above. `MISMATCH` always means something is
wrong.
