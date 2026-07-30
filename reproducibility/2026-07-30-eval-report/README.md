# Eval report freeze - 2026-07-30

kind: freeze
fetch: `make transfer-pull PREFIX=exports-frozen/2026-07-30` (the Azure Blob copy of the
export tree). The report CSVs are regenerable: check out the workspace SHA below and run
`make eval-report eval-score eval-catalog`.

The **canonical data for the BSt report**, superseding
[`../2026-07-29-eval-report-freeze/`](../2026-07-29-eval-report-freeze/). Every
human-annotation and fairness-audit number in the report is derived from this freeze and no
other. It pins two things the earlier bundle kept apart:

- the **annotation export** it rests on, and
- the **report CSVs themselves**, with their provenance sidecars and the data dictionary that
  defines their columns.

This is a **freeze**, not a lineage stage (see [`../README.md`](../README.md)): it records one
run and is never replayed. `repro-reproduce` refuses it.

## Why it supersedes 2026-07-29

The 2026-07-29 freeze was taken before three changes that make the export different in kind,
not just later:

1. **Annotator identities are pseudonymised.** pragmata writes the Argilla username into
   `annotator_id`, and the usernames on this instance are `firstname.lastname`. Every export
   up to and including 2026-07-29 carried real names in all three task CSVs and in the
   `pairwise_kappa` keys of `iaa/report.json`. This tree carries Argilla user ids instead —
   verified: 0 exact username occurrences, and all 3469 `annotator_id` values plus all 78
   pairwise keys are UUIDs. The rewrite is forward-only, so the 2026-07-29 tree still holds
   names and stays local.
2. **The log snapshot is schema v3**, with the IAA bootstrap at 1000 resamples and a fixed
   seed, matching the metric CIs' resample count and making the label intervals reproducible.
3. **The report CSVs are pinned alongside the data**, so the deliverable and its input are one
   record.

The 2026-07-29 bundle is retained as an archived record and still verifies; its pinned
artefacts are untouched.

## Pins

| Pin | Value |
|---|---|
| Files pinned | 52 (41 export + 11 report) |
| Workspace git | `36d8a14d8c2a5e3c0dfea22a1ee3afcf65e2cad4` (branch `feature/eval-report-refresh`) |
| pragmata eval pin | branch `pin/eval-report-2026-07`, SHA `f0e355e`, clean |
| Log snapshot | `run_at = 2026-07-30T12:41:38.450281+00:00`, schema v3, IAA 1000 resamples / seed 0 |
| Blob snapshot (sha256 of `MANIFEST.sha256`) | `f33aff2a3baaa25df9cf60043f0c2fb60d8b49012c5d0543514781652bd4bdef` (41 files) |

The pragmata eval pin is `origin/main` plus the two eval-score PRs the scoring depends on
(#305 panel-completeness skipping, #304 score-by-path), both **pending review upstream**. If
they land in modified form, the numbers must be re-derived from the merged code, not assumed
to carry over. Every report sidecar records the pin's branch, SHA and clean/dirty state.

The snapshot's identity is pinned transitively: `annotation_operations.csv.provenance.json`
carries the sha256 of that one log **line** — not of the append-only log, whose whole-file
digest changes nightly — and that sidecar is itself pinned here.

`pins.sha256` and the Blob `MANIFEST.sha256` are not comparable by design: the first lists
repo-relative paths, the second tree-relative ones. Each pins the same bytes under its own
naming.

## Cutoff and totals

Export `created_at` runs `2026-07-30T12:38Z` - `12:39Z`; paired log snapshot at `12:41:38Z`.
Instance totals at cutoff: **3468 submitted responses, 2516 completed records of 4244, 35
annotators**, 181 complete retrieval panels.

`zentrum-fuer-datenmanagement` is in the export tree (70 imported panels, zero annotations)
but **excluded from every report table** — decided 2026-07-30. An all-blank row reads as a
measurement rather than as an absence. The 2026-07-29 README has been amended, since it
recorded the opposite intention.

## What changed against 2026-07-29

**Annotation barely moved.** It had already been flat for four days at the 2026-07-29 cutoff,
and only six further responses arrived:

| Quantity | 2026-07-29 | 2026-07-30 | Δ |
|---|---:|---:|---:|
| Submitted responses | 3462 | 3468 | +6 |
| Completed records | 2510 | 2516 | +6 |
| Annotated units | 2721 | 2721 | 0 |
| Live records | 4244 | 4244 | 0 |
| Complete retrieval panels | 181 | 181 | 0 |

All six landed on `digitalisierung-und-gemeinwohl` / grounding (94 → 100 responses), on
records that already had responses — so they completed six records without creating any new
unit.

**No metric estimate changed.** All 16 rows of `eval_metric_estimates.csv` carry identical
point estimates, identical intervals and identical `n` to 2026-07-29. The six new responses
did not flip any majority-consolidated label. The refresh's value is the pseudonymisation, the
schema and vocabulary work, and the pinning — **not** new numbers. The report's figures do not
move.

Two expectations going in did **not** hold, recorded so they are not re-assumed:

- `digitalisierung-und-gemeinwohl` / generation was expected to become newly units-eligible.
  It was already complete at 2026-07-29: 130 live, 130 completed, 130 units, on both dates.
- The retired 2026-07-02 generation descope was expected to bring records back in. Those
  records were never removed from live, so they were already in the 2026-07-29 export.

### Where agreement moved, and why

Worth separating, because the two causes look alike in a diff:

- **One alpha point estimate changed**, and it is a *data* effect, not a parameter one:
  `digitalisierung-und-gemeinwohl` / grounding / `unsupported_claim_present`, −0.2368 →
  −0.3111. The six new responses completed that programme's grounding calibration overlap, so
  `n_items_calibration` went 24 → 30 on all five of its grounding labels and `pct_agree` on
  this one moved 0.583 → 0.500. Alpha is analytic (`1 − Do/De`), so it moves only when the
  calibration data does — the other four labels' alphas did not.
- **56 of the 81 non-blank alpha intervals changed**, and that *is* the parameter effect: the
  bootstrap went 200 → 1000 resamples and is now seeded. Only the bounds are resampled.
- `alpha_min` in `eval_metric_estimates.csv` moved on 4 of 16 rows, all grounding, for the
  same two reasons combined. The metrics those columns sit beside did not move.
- `n_items` and `n_true` are unchanged on all 91 label rows.

### Per-table deltas

| CSV | Rows | Cols | What changed |
|---|---|---|---|
| `annotation_operations.csv` | 21 → 21 | 24 → 21 | Dropped `n_curated`, `session_gap_threshold_s` (now in the sidecar) and `n_panels_with_responses`; renamed the count columns to name their grain. Only the DIG/grounding row's numbers moved. |
| `annotation_label_summary.csv` | 91 → 91 | 15 → 12 | Dropped `n_responses`, `n_true_responses`, `status` — a blank `alpha` now carries that meaning, per the dictionary. Agreement changes as above. |
| `eval_metric_estimates.csv` | 16 → 16 | 20 → 18 | Dropped `alpha_min_ci_low` / `alpha_min_ci_high`. No point estimate, interval or `n` changed; 4 `alpha_min` values did. |
| `retrieval_manifest.csv` | 6189 → 6189 | 16 → 18 | Added `annotated` and `n_annotated_chunks` — 464 of 1143 queries have an annotated retrieval panel; renamed the querygen spec's `task` to `query_task`. |
| `corpus_catalog.csv` | 2946 → 2946 | 15 → 15 | Exactly 479 cells changed, all `first_author_gender`: blank → `unknown`, unifying the no-author encoding with `author_gender`. Nothing else differs. |

`zentrum-fuer-datenmanagement` is absent from both sides.

## Verify

```
make repro-verify PIN=2026-07-30-eval-report
```

The export tree lives outside git (PII: the free-text `notes` columns are annotator-authored),
so its pins report `ABSENT` on a fresh clone and print the `fetch:` line above. The report CSVs
sit under gitignored `reports/`, so the same applies there.

The Blob copy was verified **without pulling**, deliberately: a pull would create a third
writable copy of the export. Instead the remote `MANIFEST.sha256` was downloaded and compared
against a freshly recomputed local manifest — 41/41 files identical, for both this prefix and
the rolling `exports/` one.
