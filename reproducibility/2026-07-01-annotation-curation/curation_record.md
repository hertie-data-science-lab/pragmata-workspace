# Annotation corpus curation — record (2026-07-01)

Honest, complete record of the one-off curation that reduced the live Argilla
annotation instance from the full imported corpus to the final "essential" set.
Nothing is hidden: this documents what was imported, what was removed or added,
why, and under exactly what criteria. It pairs with the machine-readable artifacts:
`keep_lists/` + `apply_log.jsonl` here, and the original import manifests + corpus/backup
checksums in the stage-1 bundle `../2026-05-initial-import/`. Together they let the end
state be rebuilt exactly (see `README.md`).

## Why

Annotator capacity could not complete everything imported, and work was spread so
thin almost nothing was *finished* (3 complete retrieval panels across all
programmes at the start). We curated down to a set the fixed per-programme annotator
pool can realistically complete, optimised for complete cross-task training
**triples** plus a full inter-annotator-agreement (IAA) calibration set.

The unit that matters is the **query** ("record"): one publikationsbot query fans
out into three tasks — a **retrieval** panel of *k* chunks, one **grounding** record,
one **generation** record. A usable training example is a query complete in all
three (a "triple").

## Scope (single prune)

Original imported corpus **21,346 records → 4,244 live** (48 Argilla datasets across
8 programmes: Bildung-und-Next-Generation, Demokratie-und-Zusammenhalt,
Digitalisierung-und-Gemeinwohl, Europas-Zukunft, Gesundheit,
Nachhaltige-Soziale-Marktwirtschaft, Zentrum-fuer-Datenmanagement (ZfD),
Zentrum-fuer-Nachhaltige-Kommunen).

## Criteria (per programme, per task)

- **Calibration: untouched.** All 30 items/task kept exactly as-is — the IAA anchor,
  already heavily worked (150–360 submissions per 30-record set). ZfD's calibration
  was restored from backup. Calibration status = which Argilla dataset a record sits
  in (`<task>_calibration` vs `<task>_production`); there is no metadata flag.
- **Retrieval: ~70 complete panels** = all ~30 calibration-straddling panels (the 30
  calibration items open ~30 panels; kept **whole**, including their production
  chunks) + **40 pure-production panels** chosen cheapest-to-finish (≤5 remaining
  chunks, steered toward panels whose grounding+generation are already done). A panel
  = one `record_uuid` group; never split.
- **Grounding & generation: keep ALL completed production + top up unfinished to ~40.**
  Completed surplus is never discarded (e.g. Demokratie keeps 147 completed
  generation). Unfinished records have 0 submissions, so dropping the surplus loses
  no work.
- **Cross-task consistency:** the 40 pure-production queries are the same across all
  three tasks → 40 complete triples/programme.

## What was removed, and why

Only **retrieval production** discarded any *submitted* work: the non-consolidated
partial panels beyond the kept 70 (a deliberate capacity trade-off). Grounding,
generation, and all calibration lost **zero** submitted work.

Retrieval production submitted chunks dropped vs kept, per programme:

| programme | dropped | kept |
|---|---:|---:|
| Digitalisierung-und-Gemeinwohl | 213 | 107 |
| Europas-Zukunft | 56 | 29 |
| Gesundheit | 36 | 40 |
| Nachhaltige-Soziale-Marktwirtschaft | 27 | 32 |
| Bildung-und-Next-Generation | 21 | 11 |
| Demokratie-und-Zusammenhalt | 13 | 7 |
| Zentrum-fuer-Nachhaltige-Kommunen | 10 | 31 |
| Zentrum-fuer-Datenmanagement | 0 | 0 |

Digitalisierung dominates because it was the most-progressed programme (183 partly-
worked panels); a uniform 70-panel target keeps only its most-consolidated ones.

## What was added

- ZfD (which had been emptied earlier) was reseeded to the full target + its
  calibration restored, so it is ready if/when staffed.
- Records restored from the pre-prune backup where a kept query was missing from live.

## min_submitted changes (applied live)

Calibration completes only once `min_submitted` distinct annotators have done each
item; several programmes have fewer annotators than 3 (roster: seniors →
generation, juniors → retrieval+grounding).

| programme | juniors | seniors | ret/gnd cal | gen cal | change |
|---|---:|---:|---|---|---|
| Bildung, Gesundheit, ZfNK | 3 | 3 | 3 | 3 | none |
| Demokratie | 3 | 2 | 3→2 | 3→2 | ret+gnd+gen cal → 2 |
| Digitalisierung | 2 | 2 | 3→2 | 3→2 | ret+gnd+gen cal → 2 |
| Europas | 2 | 1 | 3→2 | 3 (⚠) | ret+gnd → 2; gen unfixable |
| NSM | 3 | 0 | 3 | — (⚠) | no seniors → no generation |
| ZfD | 0 | 0 | — | — (⚠) | no annotators at all |

## Staffing-blocked (cannot be fixed by settings)

- **Europas-Zukunft generation**: 1 senior — IAA needs ≥2, so generation calibration
  can never complete. Needs a 2nd senior.
- **NSM generation**: 0 seniors — no generation annotation possible.
- **ZfD**: 0 annotators — seeded but nothing progresses until staffed.
- Note: ZfNK generation's 3rd senior is the `test.senior` fixture; if that is not a
  real annotator, ZfNK generation calibration (min_submitted 3) may also be
  unachievable.

## Risks

- **Retrieval selection bias**: keeping cheapest-to-finish panels skews the kept set
  to small-*k* / already-progressed queries and drops the high-fan-out tail. Any
  retrieval metric computed on this data under-represents large panels — caveat when
  using it.
- **Low agreement**: retrieval α≈0.08, generation α≈−0.02 at last measurement — at or
  below chance. The complete triples may need a guideline/label review before serving
  training. Out of scope for this curation.

## Verification

Independently audited (Opus, re-derived from backup vs live, not from the executor's
scripts): **VERIFIED CORRECT**. 40/40 protected triples per programme; retrieval
67–70 whole panels (0 split); calibration id-sets byte-identical to backup incl.
restored ZfD; only submitted-work loss = 376 retrieval_production records / 1,157
responses (permitted); live ⊆ complete backup (0 records live-not-in-backup → all
removals recoverable).

One caveat for future operators: `apply_log.jsonl` records only the final prune's
deletions, not every backup→live removal — **use the pre-prune backup, not the log,
as the recovery source**.

## Provenance

- **Run date:** 2026-07-01.
- **Workspace git at curation time:** `485ce05` on `main`.
- **Tooling:** Python 3.12.13, `argilla` client 2.8.0; `pragmata` CLI from the
  `PRAGMATA_SRC` checkout (branch `demo-2026-05-26`).
- **Pre-prune backup** (full original, exact incl. responses): `20260701T185359Z_backup_pre_prune`
  (48 datasets, 21,346 records; `manifest.json` sha256 in `checksums.sha256`).
- **Source corpus / query generation:** `data/publikationsbot/<slug>_combined.jsonl`
  (pinned in `checksums.sha256`), generated via `configs/annotation/querygen_specs/` +
  `_runtime.yaml` (model `gpt-5.4`, `reasoning_effort: high`). Querygen is
  non-deterministic LLM output, so the corpus is the versioned input, not re-derived.
- **Authoritative record:** this file. `data/annotation/imports/curation_changelog.md`
  is a gitignored working copy generated during the run; if they ever disagree, this
  committed record wins.
