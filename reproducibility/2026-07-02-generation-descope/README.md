# Generation production descope - 2026-07-02

kind: lineage
fetch: `argilla_backup/20260702T094309Z/` is a local recovery point only, not archived

Stage 3 of the live annotation instance's lineage, amending
[`../2026-07-01-annotation-curation/`](../2026-07-01-annotation-curation/) (stage 2) for a
single dataset. That bundle is an independently-audited historical record (verified at 100
records for this dataset) and is left untouched - this is a separate, dated amendment on top
of it, not an edit to it.

## What changed and why

`Digitalisierung-und-Gemeinwohl_generation/generation_production` reached its 2026-07-01
target of 100 records with **80 `completed`** (min_submitted=1 met) and **20 `pending`**
with zero responses of any status, including drafts (verified live, 2026-07-02) - i.e. the
80 completed already exceed the ~40 production baseline used elsewhere in stage 2, and the
remaining 20 are untouched. Per the same "drop unfinished, zero submissions loses no work"
principle stage 2 used, the target is descoped from 100 → 80: the two generation annotators
for this domain are treated as done for this dataset.

Calibration (`generation_calibration`, 30/30 completed) is unaffected.

## Contents

| Path | What it is |
|---|---|
| `keep_lists/Digitalisierung-und-Gemeinwohl_generation__generation_production.ids` | The amended keep-list: 80 ids (the `completed` records only), a strict subset of stage 2's 100. |
| `pins.sha256` | Pins the pre-amendment backup manifest (`argilla_backup/20260702T094309Z/`), so the retention policy can see which dumps a bundle still needs. |

## Reproduce

One step - `repro-reproduce` composes every lineage bundle's keep-lists in date order,
latest winning per dataset, so this amendment is already part of the replay:

```
make repro-reproduce PIN=2026-07-02-generation-descope
```

The composition is an **override, not a union**: this bundle's 80 ids replace stage 2's
100 wholesale. Unioning them would give back the 4,244-record state and lose the descope.
Preview (no `APPLY=1`) reports `delete 0` against a live instance already at the composed
4,224-record state - that preview is the verification, same convention as stage 2.

## Verification (pre-apply, 2026-07-02)

Queried live via the Argilla SDK: dataset has exactly 100 records (matches the stage-2
keep-list, no drift), `distribution=OverlapTaskDistribution(min_submitted=1)`, 80
`completed` / 20 `pending`. All 20 pending records confirmed to have zero responses of any
status (checked including `draft`) - none partially worked. Prune preview against this
amendment's keep-list reports `keep 80/80, delete 20/100`, matching expectation.

Pre-amendment backup: see `make annotation-backup` output for run timestamp `20260702T094309Z`
(`argilla_backup/20260702T094309Z/manifest.json`, gitignored - local recovery point only).
