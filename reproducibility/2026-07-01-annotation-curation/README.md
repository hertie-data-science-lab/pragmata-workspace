# Annotation curation — 2026-07-01

Stage 2 of the live annotation instance's lineage: the one-off curation that reduced the
imported corpus (**21,346 records**) to the final "essential" set (**4,244 records**).
Stage 1 (the original import + the pinned corpus/backup) is
[`../2026-05-initial-import/`](../2026-05-initial-import/). See
[`curation_record.md`](curation_record.md) for what was removed/added, why, under what
criteria, the verification result, and provenance.

## Contents

| Path | What it is |
|---|---|
| `curation_record.md` | The honest record: criteria, per-domain removals, min_submitted changes, staffing flags, risks, audit result, provenance (incl. the 2026-07-01 date). |
| `keep_lists/<workspace>__<dataset>.ids` | The **declared end state**: the exact record-ids to keep per dataset (48 files, 4,244 ids). `_counts.json` summarises. |
| `apply_log.jsonl` | Audit log of what the as-run prune deleted (not needed to reproduce; kept for the record). |

The corpus + backup pins live in stage 1's `checksums.sha256`; the reproduction **tool** is
first-class at `scripts/annotation/prune_to_keeplist.py`.

## Reproduce

Declarative: the keep-lists are the desired end state; reproduction builds the full
superset (stage 1) then prunes down to them. `import.sh` / the `import` subcommand are
untouched. Point `ARGILLA_API_URL`/`ARGILLA_API_KEY` at the target, then from the repo root:

```
make reproduce-curation                                     # preview = verify (expect delete 0, 0 missing)
make reproduce-curation MODE=structure APPLY=1              # import the full corpus (stage 1), then prune to keep-lists
make reproduce-curation MODE=responses BACKUP=<dir> APPLY=1 # restore the backup (stage 1), then prune to keep-lists
```

- **MODE=structure** rebuilds record structure from the corpus (no responses).
- **MODE=responses** restores the exact state incl. annotations from the backup.
- **No args** runs `prune_to_keeplist.py` in preview against the current instance — if it
  reports `delete 0` and no missing keep-ids, live already equals the declared state. That
  preview *is* the verification.

## Why a prune step (not a plain re-import)

Import fans every query into all three tasks and can only flag records
calibration-vs-production — it can't express "this query has no record in task X", and its
manifest is append-only. So the curated set (heterogeneous per query × task) is only
reachable by building the superset then deleting down to the keep-lists — the same
declarative "reduce to declared state" model as `kubectl apply --prune` / `terraform apply`
/ `rsync --delete`.
