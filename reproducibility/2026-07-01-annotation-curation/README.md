# Annotation curation - 2026-07-01

kind: lineage
fetch: the external corpus/backup archive pinned by `../2026-05-initial-import/pins.sha256`

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
| `keep_lists/<workspace>__<dataset>.ids` | The **declared end state as of this date**: the exact record-ids to keep per dataset (48 files, 4,244 ids). `_counts.json` summarises. |
| `apply_log.jsonl` | Audit log of what the as-run prune deleted (not needed to reproduce; kept for the record). |
| `pins.sha256` | Pins the eight curated corpus files this prune produced, plus the pre-prune backup manifest. |

The full corpus + backup pins live in stage 1's `pins.sha256`; the reproduction **tool** is
first-class at `scripts/annotation/prune_to_keeplist.py`.

## This is the end of the lineage

These keep-lists and `_counts.json` stand at their audited 2026-07-01 values — **4,244 ids**,
`Digitalisierung-und-Gemeinwohl_generation/generation_production` = 100 — and that is also
where the lineage ends, so it is what `repro-reproduce` converges to and what live holds.

The 2026-07-02 descope
([`../2026-07-02-generation-descope/`](../2026-07-02-generation-descope/)) would have amended
that one dataset to 80, but it was never applied and was retired on 2026-07-30 once the
annotators had completed all 20 records it would have dropped. It carries `status: retired`
and is excluded from composition.

## Reproduce

Declarative: the keep-lists are the desired end state; reproduction builds the full
superset (stage 1) then prunes down to them. `import.sh` / the `import` subcommand are
untouched. Point `ARGILLA_API_URL`/`ARGILLA_API_KEY` at the target, then from the repo root:

```
make repro-reproduce PIN=2026-07-01-annotation-curation                                     # preview = verify
make repro-reproduce PIN=2026-07-01-annotation-curation MODE=structure APPLY=1              # import the full corpus (stage 1), then prune
make repro-reproduce PIN=2026-07-01-annotation-curation MODE=responses BACKUP=<dir> APPLY=1 # restore the backup (stage 1), then prune
```

- **MODE=structure** rebuilds record structure from the corpus (no responses).
- **MODE=responses** restores the exact state incl. annotations from the backup.
- **No MODE/APPLY** runs `prune_to_keeplist.py` in preview against the current instance.
  Every active lineage bundle's keep-lists are composed first, latest date winning per
  dataset; with the descope retired that composition is this bundle's 4,244 ids. On a live
  instance at that state it reports `delete 0` and no missing keep-ids, and that preview
  *is* the verification.

## Why a prune step (not a plain re-import)

Import fans every query into all three tasks and can only flag records
calibration-vs-production - it can't express "this query has no record in task X", and its
manifest is append-only. So the curated set (heterogeneous per query × task) is only
reachable by building the superset then deleting down to the keep-lists - the same
declarative "reduce to declared state" model as `kubectl apply --prune` / `terraform apply`
/ `rsync --delete`.
