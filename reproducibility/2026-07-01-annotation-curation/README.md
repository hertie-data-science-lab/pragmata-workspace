# Annotation curation — reproducibility bundle (2026-07-01)

Everything needed to **exactly rebuild** the curated Argilla annotation corpus, plus
a full honest account of what the curation did. The corpus was reduced from the full
imported set (21,346 records) to the final "essential" set (4,244 records) — see
[`curation_record.md`](curation_record.md) for what was removed/added, why, and under
what criteria, and the verification result.

## What's here

| Path | What it is |
|---|---|
| `curation_record.md` | The honest history: criteria, per-domain removals, min_submitted changes, staffing flags, risks, audit result. |
| `manifests/<SCOPE>/partition.meta.curated.json` | The 8 final curated partition manifests (calibration/production placement per surviving query). |
| `keep_lists/<workspace>__<dataset>.ids` | **Authoritative** — the exact record-ids that should be live per dataset (48 files, 4,244 ids). Snapshot of the verified end state. `_counts.json` summarises. |
| `checksums.sha256` | SHA256 of each curated corpus file + the pre-prune backup manifest — pins the external artifacts. |
| `target.json` | Selection spec (per-domain uuids, id-sets, k-buckets) — the "what/why". |
| `plan.json`, `apply_log.jsonl` | Machine-readable diff of the as-run prune (drop-lists / deletions). |
| `scripts/prune_to_keeplist.py` | Reproduction primitive: prune live to the keep-lists (standalone, SDK-only). |
| `scripts/select.py`, `prune.py`, `verify.py` | The as-run selection / drop-list prune / verification tools (auditable). |

## External artifacts (not in git — too large)

Pinned by `checksums.sha256`, stored as a release/archive alongside the repo:

- **Curated corpus** `data/publikationsbot/<slug>_combined.curated.jsonl` (8 files, ~52M) — the query corpus the annotators saw. Querygen is non-deterministic LLM output (`gpt-5.4`, see `configs/annotation/querygen_specs/_runtime.yaml`), so this file **is** the versioned input.
- **Pre-prune backup** `20260701T185359Z_backup_pre_prune` (~2.1G, 21,346 records with responses+status) — the exact original, for byte-faithful restore.

Fetch them, then verify: `sha256sum -c checksums.sha256`.

## Reproduce

Two recipes. Both finish by reconciling live to the keep-lists; `import.sh` and the
`import` subcommand are **not** modified.

Point `ARGILLA_API_URL`/`ARGILLA_API_KEY` at the target (ideally a scratch instance),
then from the repo root:

```
make reproduce-curation MODE=structure   # from the corpus (records only, no responses)
make reproduce-curation MODE=responses   # from the backup (exact, incl. annotations)
```

**MODE=structure** — rebuild the record structure from source:
1. `sha256sum -c reproducibility/2026-07-01-annotation-curation/checksums.sha256`
2. `scripts/annotation/import.sh <domain>` for each domain (fans every query into all
   3 tasks — this over-creates relative to the curated set).
3. `prune_to_keeplist.py --keep-lists .../keep_lists --apply` → deletes the over-created
   records, leaving exactly the curated set.

**MODE=responses** — restore the exact state incl. annotations:
1. `sha256sum -c .../checksums.sha256`
2. `scripts/annotation/argilla_backup.py restore <backup> --apply` (the full original).
3. `prune_to_keeplist.py --keep-lists .../keep_lists --apply` → prune back to the curated set.

Verify either with:
```
reproducibility/2026-07-01-annotation-curation/scripts/verify.py \
  --target reproducibility/2026-07-01-annotation-curation \
  --backup <pre-prune-backup>
```

## Why a prune step is needed (not a plain re-import)

Import fans every query into all three tasks and can only flag records
calibration-vs-production — it cannot express "this query has no record in task X".
The curated set is heterogeneous at the (query × task) level, so the only way to
reach it is: build a superset (import or restore) then delete down to the keep-lists.
The append-only import manifest also means a plain `import.sh` re-run would re-add
pruned queries — hence the separate prune step here rather than a change to import.
