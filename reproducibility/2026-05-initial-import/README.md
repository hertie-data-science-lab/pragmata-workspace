# Initial import — 2026-05

Stage 1 of the live annotation instance's lineage: the original build + import of the
corpus into Argilla (before any curation). See [`provenance.md`](provenance.md) for how
the corpus was generated and the non-determinism caveat.

Pairs with the later curation: [`../2026-07-01-annotation-curation/`](../2026-07-01-annotation-curation/).

## Contents

| Path | What it is |
|---|---|
| `original_manifests/<SCOPE>/partition.meta.json` | The original partition manifests — exactly what was imported per domain (calibration/production placement, keyed by `record_uuid`, `partition_seed: 0`). |
| `checksums.sha256` | Pins the external artifacts: the full source corpus + the pre-prune backup (too large for git). |
| `provenance.md` | Querygen model/config, import dates, tool versions, and the non-determinism caveat. |

## External artifacts (not in git)

Pinned by `checksums.sha256`, stored as an external archive:

- **Source corpus** `data/publikationsbot/<slug>_combined.jsonl` (~549M) — the full imported
  corpus (8 domains).
- **Pre-prune backup** `20260701T185359Z_backup_pre_prune` (~2.1G, 21,346 records with
  responses) — the full instance immediately before the curation.

Fetch, then verify from the repo root:
`sha256sum -c reproducibility/2026-05-initial-import/checksums.sha256`.

## Reproduce stage 1

```
# fetch the pinned corpus, verify, then import the full set (fans every query into all 3 tasks)
sha256sum -c reproducibility/2026-05-initial-import/checksums.sha256
for d in configs/annotation/domains/*.yaml; do make import DOMAIN=$(basename $d .yaml); done
```

This rebuilds the full imported instance. To then reduce it to the curated set, continue
with stage 2 (`make reproduce-curation`).

> The corpus itself is **not regenerable to identical bytes** — querygen is
> non-deterministic LLM output over a live bot (see `provenance.md`). Reproduction fetches
> the pinned corpus; it does not re-run querygen.
