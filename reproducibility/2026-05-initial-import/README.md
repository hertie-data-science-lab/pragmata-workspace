# Initial import - 2026-05

kind: lineage
fetch: the external corpus/backup archive (see `provenance.md`); or regenerate the corpus with `make pipeline` — non-deterministic, so the bytes will not match

Stage 1 of the live annotation instance's lineage: the original build + import of the
corpus into Argilla (before any curation). See [`provenance.md`](provenance.md) for how
the corpus was generated and the non-determinism caveat.

Pairs with the later curation: [`../2026-07-01-annotation-curation/`](../2026-07-01-annotation-curation/).

## Contents

| Path | What it is |
|---|---|
| `original_manifests/<SCOPE>/partition.meta.json` | The original partition manifests - exactly what was imported per domain (calibration/production placement, keyed by `record_uuid`, `partition_seed: 0`). |
| `pins.sha256` | Pins the external artifacts: the full source corpus + the pre-prune backup (too large for git). |
| `provenance.md` | Querygen model/config, import dates, tool versions, and the non-determinism caveat. |

## External artifacts (not in git)

Pinned by `pins.sha256`, stored as an external archive:

- **Source corpus** `data/publikationsbot/<slug>_combined.jsonl` (~119M total) - the full
  imported corpus (8 domains).
- **Pre-prune backup** `20260701T185359Z_backup_pre_prune` (~250M, 21,346 records with
  responses) - the full instance immediately before the curation.

Fetch, then verify from the repo root: `make repro-verify PIN=2026-05-initial-import`
(the nine pins report `ABSENT` until the artifacts are fetched).

## Reproduce stage 1

```
# fetch the pinned corpus, verify, then import the full set (fans every query into all 3 tasks)
make repro-verify PIN=2026-05-initial-import
for d in configs/annotation/domains/*.yaml; do make annotation-import DOMAIN=$(basename $d .yaml); done
```

> The make targets were renamed after this bundle was frozen (`import` ->
> `annotation-import`, `checksums.sha256` -> `pins.sha256`); the recipe above is the
> current, runnable form. The artifacts and the pinned hashes are unchanged.

This rebuilds the full imported instance. To then reduce it to the curated set, continue with
`make repro-reproduce PIN=2026-07-01-annotation-curation` — which composes every active
lineage bundle's keep-lists in date order, so it lands on the end of the lineage (4,244
records) whatever later bundles exist.

> The corpus itself is **not regenerable to identical bytes** - querygen is
> non-deterministic LLM output over a live bot (see `provenance.md`). Reproduction fetches
> the pinned corpus; it does not re-run querygen.
