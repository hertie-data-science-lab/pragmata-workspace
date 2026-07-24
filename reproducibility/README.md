# reproducibility/

Dated records of the operations that produced the live Argilla annotation instance -
**one bundle per operation**, in chronological order. Together they are the instance's
**lineage**. Stage 1 is a replayable build step; the later curation/descope stages are
**declarative end-states**. So "rebuild" means: import stage 1's corpus,
then apply each later stage's keep-lists in order.

| # | Bundle | Operation |
|---|---|---|
| 1 | `2026-05-initial-import/` | Original build + import of the corpus (querygen → bot → combine → import). Holds the original manifests + the pinned corpus/backup checksums + provenance. |
| 2 | `2026-07-01-annotation-curation/` | Curation: pruned 21,346 → 4,244 records. Holds the record + the declared end-state keep-lists + audit log. |
| 3 | `2026-07-02-generation-descope/` | Amendment on stage 2 for one dataset: descoped `Digitalisierung-und-Gemeinwohl_generation/generation_production` 100 → 80 (dropped 20 zero-submission records). Holds the amended keep-list. |

Rebuild end-to-end: `make reproduce-curation MODE=structure APPLY=1` (imports stage 1's
corpus, then prunes to stage 2's keep-lists). The reusable tooling lives in `scripts/`
(`prune_to_keeplist.py`, `import.sh`, `argilla_backup.py`), not in the bundles.

Large artifacts (the full corpus, Argilla backups) are **not** in git — they're pinned by
SHA256 in stage 1's `checksums.sha256` and stored externally. See the
[Reproducibility](../docs/reproducibility.md) doc.

## Two kinds of reproducibility artifact

The bundles above are **instance-lineage**: an ordered record that rebuilds a *stateful*
system (the live Argilla instance). The eval stage introduces the other kind —
**run-provenance**: independent, self-contained dated snapshots that pin one eval run's
inputs → code → outputs by SHA256, *not* replayed in sequence. Those aren't here yet; they
land with the eval pipeline (transport groundwork in [`scripts/eval/`](../scripts/eval/)).
