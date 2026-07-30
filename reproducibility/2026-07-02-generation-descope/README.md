# Generation production descope - 2026-07-02

kind: lineage
status: retired
fetch: `argilla_backup/20260702T094309Z/` is a local recovery point only, not archived

> **RETIRED 2026-07-30 — declared but never applied, and its premise has since expired.**
> See [Status](#status) below. `kind:` stays `lineage` because that is what this bundle was;
> `status: retired` is what excludes it from replay composition, so the lineage now ends at
> the 2026-07-01 keep-lists (**4,244 records**). No records were deleted.

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

## Status

| Date | Event |
|---|---|
| 2026-07-02 | **Declared.** Premise: the 20 records to drop held zero responses of any status, drafts included (verified live that day, recorded under Verification below). Stage 2's principle — "drop unfinished, zero submissions loses no work" — applied cleanly. |
| 23-25 July | **Premise expired.** The two generation annotators returned and completed all 20, one submitted annotation each. `Digitalisierung-und-Gemeinwohl_generation/generation_production` went to 100/100 `completed`, 100 submitted responses. |
| 2026-07-30 | **Retired, with no deletion.** Applying it would have destroyed 20 completed human annotations — the exact loss its own principle promised to avoid. |

The amendment was **never applied to live**: it was recorded as a declared end state and no
prune was ever run with `--apply`. So there is nothing to undo.

Evidence that the premise expired, three independent sources agreeing:

- **Frozen export** `data/annotation/exports-frozen/2026-07-29/digitalisierung-und-gemeinwohl/generation.csv`
  — all 20 of the would-be-dropped `record_uuid`s present, each `record_status: completed`,
  `response_status: submitted`, with an `annotator_id` and answer content.
- **Live** (read-only, 2026-07-30) — 100 records, every one of the 20 carrying a submitted
  annotation.
- **`logs/annotation/log.jsonl`** — DIG generation flat at 160 submitted / 130 records /
  130 `completed` / **0 `pending`** from the 2026-07-27 snapshot through 2026-07-30, the
  production block at 100 records / 100 submitted.

The keep-list is **retained** as the historical record of what was declared. It is not a
statement about live and must not be applied.

## Reproduce

Nothing to replay: `status: retired` excludes this bundle from the composition, so
`repro-reproduce` skips it (and refuses `PIN=` pointing at it). The lineage ends at stage 2's
4,244 records, which is what live holds.

Had it stayed active, composition would have been an **override, not a union** — this
bundle's 80 ids replacing stage 2's 100 wholesale rather than merging with them.

## Verification (pre-apply, 2026-07-02 — superseded, see Status)

Queried live via the Argilla SDK: dataset has exactly 100 records (matches the stage-2
keep-list, no drift), `distribution=OverlapTaskDistribution(min_submitted=1)`, 80
`completed` / 20 `pending`. All 20 pending records confirmed to have zero responses of any
status (checked including `draft`) - none partially worked. Prune preview against this
amendment's keep-list reports `keep 80/80, delete 20/100`, matching expectation.

Pre-amendment backup: see `make annotation-backup` output for run timestamp `20260702T094309Z`
(`argilla_backup/20260702T094309Z/manifest.json`, gitignored - local recovery point only).
