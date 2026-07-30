# Eval report data freeze - 2026-07-29

The **canonical annotation export** for the BSt report. Every human-annotation and
fairness-audit number in the report is derived from this snapshot and no other, so the
figures stay reproducible while the live instance keeps moving.

This is the first **run-provenance** bundle rather than an instance-lineage stage (see
[`../README.md`](../README.md#two-kinds-of-reproducibility-artifact)): it pins one run's
inputs by SHA256 and is not replayed in sequence with the numbered stages.

## Why a copy rather than stopping the cron

`scripts/daily.sh` runs nightly (`0 2 * * *`) and chains export -> `log.py`. It stays
**enabled**. Killing it would also stop `log.py`, which is the only source of
per-response submission timestamps (Argilla REST - the export CSVs and the SDK both drop
them), and would blind us to annotation resuming. Resumption is exactly what would
rescue retrieval coverage for the four short programmes below.

So the live tree at `data/annotation/exports/` keeps being overwritten, and this frozen
copy is what the report reads.

## Contents

| Path | What it is |
|---|---|
| `checksums.sha256` | Pins all 41 frozen files (repo-relative paths). |
| this README | Cutoff, counts, and the blob snapshot pin. |

The export tree itself is **not in git** - it carries PII (`annotator_id` in every CSV,
annotator names in `iaa/report.json` `pairwise_kappa`), and `data/README.md` labels it
never-commit. Two durable copies exist instead:

- **Local, read-only:** `data/annotation/exports-frozen/2026-07-29/` (`chmod a-w`).
- **Azure Blob:** `fileshare01/exports-frozen/2026-07-29/` via
  `scripts/transfer/sync.sh push`, with its own `MANIFEST.sha256`.

The blob prefix is a **sibling** of `exports/`, not nested inside it. `sync.sh pull`
fetches with `--pattern "<prefix>/*"`, so a dated snapshot under `exports/2026-07-29/`
would be dragged down by every routine `make transfer-pull PREFIX=exports`. Keeping the
frozen snapshot beside the rolling tree keeps the two independent.

Verify locally from the repo root:

```
sha256sum -c reproducibility/2026-07-29-eval-report-freeze/checksums.sha256
```

Or re-fetch and verify the blob copy (round-trip confirmed 2026-07-29):

```
make transfer-pull PREFIX=exports-frozen/2026-07-29
```

## Pins

| Pin | Value |
|---|---|
| Local tree (sha256 of `checksums.sha256`) | `5a6b5a76042741a79bc9789947325f46db7bde92fb4a27e8741dec4f2f16dcfd` |
| Blob snapshot (sha256 of `MANIFEST.sha256`) | `9cbaefad8a49b117f52884d83dd4d41bf351738f8075dee1b7f2873ff8aaeba4` |
| Files | 41 |

The two differ by design and are not comparable: `checksums.sha256` lists
repo-relative paths, `sync.sh`'s manifest lists tree-relative paths (`./domain/file`).
Each pins the same bytes under its own naming.

## Cutoff

Export `created_at` runs `2026-07-29T02:00:08Z` - `02:00:58Z` (one per domain, written
by that night's cron). Paired log snapshot: `logs/annotation/log.jsonl`
`run_at = 2026-07-29T02:01:25Z`.

Annotation had been **flat for four days** at the cutoff - submitted responses and
complete panels unchanged since the `2026-07-25` snapshot (3462 / 181), after a burst
between 23 and 25 July that took complete panels 151 -> 173 -> 181. Idle, but not
provably finished.

Instance totals at cutoff: 3462 submitted responses, 2510 completed records of 4244,
35 annotators.

## Counts

| Programme | retrieval rows | grounding | generation | panels | complete |
|---|---:|---:|---:|---:|---:|
| bildung-und-next-generation | 279 | 139 | 169 | 68 | 5 |
| demokratie-und-zusammenhalt | 394 | 105 | 165 | 70 | 70 |
| digitalisierung-und-gemeinwohl | 167 | 94 | 160 | 69 | 2 |
| europas-zukunft | 362 | 104 | 110 | 68 | 68 |
| gesundheit | 207 | 44 | 63 | 69 | 3 |
| nachhaltige-soziale-marktwirtschaft | 167 | 44 | 99 | 70 | 0 |
| zentrum-fuer-datenmanagement | 0 | 0 | 0 | 70 | 0 |
| zentrum-fuer-nachhaltige-kommunen | 349 | 138 | 104 | 67 | 33 |
| **TOTAL** | **1925** | **668** | **870** | **551** | **181** |

Rows are annotator-response rows, so a record annotated by three people contributes
three. `panels` counts retrieval `record_uuid`s; `complete` is the STRICT
(submitted-only) `panel_complete`.

Two things a reader must not miss:

- **`zentrum-fuer-datenmanagement` has 70 panels and zero annotations.** It is a real
  programme with imported records that nobody annotated - it must appear as an explicit
  `n=0` row in every report table, not vanish.
- **Retrieval panel completeness is 33% overall and very uneven.** Only demokratie
  (70/70), europas-zukunft (68/68) and kommunen (33/67) have enough complete panels to
  report retrieval metrics. See `reports/eval-pipeline-results.md` for what that means
  for the metric taxonomy.
