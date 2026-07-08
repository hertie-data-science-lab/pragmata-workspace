# Reproducibility

`reproducibility/` holds one **dated bundle per operation** that produced the live instance
(chronological, migration-style):

1. `2026-05-initial-import/` — the original build + import: the partition manifests,
   `checksums.sha256` pinning the full source corpus + pre-prune backup (external), and
   `provenance.md` (querygen model/dates, the non-determinism caveat).
2. `2026-07-01-annotation-curation/` — the curation (21,346 → 4,244 records): the
   `curation_record.md`, the per-dataset keep-lists (the declared end state), and
   `apply_log.jsonl` (audit of what was deleted).

Reproduction is **declarative**: the keep-lists are the desired state, and the reusable tool
`scripts/annotation/prune_to_keeplist.py` reduces any superset to them (the
`kubectl apply --prune` / `terraform` model). `make reproduce-curation` chains it. Point
`ARGILLA_API_URL`/`ARGILLA_API_KEY` at the target, fetch the pinned artifacts, then:

```
make reproduce-curation                                     # preview = verify (expect delete 0, 0 missing)
make reproduce-curation MODE=structure APPLY=1              # import the full corpus, then prune to keep-lists
make reproduce-curation MODE=responses BACKUP=<dir> APPLY=1 # restore the backup, then prune to keep-lists
```

Both build a superset (import the full corpus, or restore the backup) then prune down to the
keep-lists — a plain re-import cannot reproduce the curated set on its own. Full detail in
the bundle's `README.md`.
