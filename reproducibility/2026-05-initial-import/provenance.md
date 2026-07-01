# Initial import — provenance

## What this stage is

The original build of the corpus and its import into Argilla, per domain:
`querygen → bot → combine → import`. Output: the `original_manifests/` (the partition
assignments) and the live records in Argilla. This is the state the
[2026-07-01 curation](../2026-07-01-annotation-curation/) later pruned.

## Import dates (per domain, from the manifests' `created_at`)

| scope | imported |
|---|---|
| DEM, DIG, ZFK | 2026-05-29 |
| BIL, EUR | 2026-05-30 |
| GES, NSM, ZFD | 2026-05-31 |

`partition_seed: 0` for all scopes.

## Query generation (stage input)

- Specs: `configs/annotation/querygen_specs/*.yaml` + `_runtime.yaml` (committed).
- Model: `gpt-5.4`, `reasoning_effort: high` (planning + realization), `batch_size: 15`,
  `near_duplicate_tolerance: 0.95` (see `_runtime.yaml`).
- **Non-deterministic**: querygen is LLM output and the bot runs against a live service, so
  the corpus **cannot be regenerated to identical bytes**. The exact corpus is therefore
  pinned by SHA256 in `checksums.sha256` and treated as the versioned input — not re-derived.

## Tooling

- Python 3.12.13; `argilla` client 2.8.0; `pragmata` CLI from `PRAGMATA_SRC`
  (branch `demo-2026-05-26`).
- Workspace git when this record was written: `485ce05` on `main`.

## What "reproduce" means here

Fetch the pinned corpus (verify against `checksums.sha256`) and re-import it with the
committed configs; a fresh import with `partition_seed: 0` reproduces the same
calibration/production placement recorded in `original_manifests/`.
