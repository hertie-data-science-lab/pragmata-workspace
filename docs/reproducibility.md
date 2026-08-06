# Reproducibility

`reproducibility/` holds one **dated bundle per operation or run**, all in the same shape: `<YYYY-MM-DD>-<name>/` with a `README.md` (carrying a `kind:` header) and a generated `pins.sha256`. The bundle contract, the retention policy and the composition rules live in [`reproducibility/README.md`](../reproducibility/README.md); this page is the entrypoint.

| Bundle | kind | Operation |
|---|---|---|
| `2026-05-initial-import/` | lineage | Original build + import: the partition manifests, `provenance.md` (querygen model/dates, the non-determinism caveat), and pins for the external corpus + pre-prune backup. |
| `2026-07-01-annotation-curation/` | lineage | The curation, 21,346 → 4,244 records: `curation_record.md`, the per-dataset keep-lists, `apply_log.jsonl`. |
| `2026-07-02-generation-descope/` | lineage, **retired** | Would have taken one dataset to 80 records. Declared 2026-07-02, never applied, retired 2026-07-30 once its premise expired (the annotators completed all 20 records it would have dropped). Excluded from replay. |
| `2026-07-29-eval-report-freeze/` | freeze, **superseded** | The first canonical annotation export for the BSt report. Superseded by `2026-07-30-eval-report/`; retained as an archived record and still verifies. Its export tree predates pseudonymisation, so it holds real annotator names and stays local. |
| `2026-07-30-eval-report/` | freeze, **superseded** | The first deliverable set built on the pseudonymised export. Superseded by `2026-08-06-eval-report/`; retained as an archived record and still verifies. |
| `2026-08-06-eval-report/` | freeze | The canonical data behind every number in the BSt report: the pseudonymised export, the tiered report CSVs with provenance and data dictionary, and the three evaluator training runs. Never replayed. |

`kind: lineage` bundles are replayed in date order to rebuild the live Argilla instance; `kind: freeze` bundles are self-contained records of a single run. A `status: retired` header drops a bundle out of replay while keeping it on the record.

## Targets

```
make repro-verify [PIN=<bundle-dir>]            # per-file OK/MISMATCH/ABSENT (Error 2 mismatch, Error 3 absent only)
make repro-pin NAME=<name> PATHS="<path ...>"   # create today's bundle: pins + a README stub
make repro-reproduce PIN=<bundle-dir>           # replay a lineage bundle (MODE= BACKUP= APPLY=)
```

`ABSENT` is expected for artefacts held outside git (the corpus, Argilla dumps, the PII-carrying export tree); verify prints the bundle's `fetch:` line so you know where to get them. `MISMATCH` always means something is wrong: a whole-tree run exits 2 if any single pin mismatched, however many bundles report only absences. Verifying nothing is a failure too: no bundles at all, or a bundle with an empty `pins.sha256`.

## Replaying the lineage

Reproduction is **declarative**: the keep-lists are the desired end state, and `scripts/annotation/prune_to_keeplist.py` reduces any superset to them (the `kubectl apply --prune` / `terraform` model). A plain re-import cannot reach the curated set on its own - import fans every query into all three tasks, so the superset has to be built and then pruned down.

`repro-reproduce` composes **every** active lineage bundle's keep-lists in date order before pruning, later dates overriding earlier ones per dataset, and reports what it skipped. The lineage currently ends at **4,244 records** across 48 datasets - the 2026-07-01 keep-lists alone, since the 2026-07-02 descope is retired. Whether live still matches that is what the preview reports; do not assume it.

Point `ARGILLA_API_URL`/`ARGILLA_API_KEY` at the target, fetch the pinned artefacts, then:

```
make repro-reproduce PIN=2026-07-01-annotation-curation                                     # preview = verify live
make repro-reproduce PIN=2026-07-01-annotation-curation MODE=structure APPLY=1              # import the full corpus, then prune
make repro-reproduce PIN=2026-07-01-annotation-curation MODE=responses BACKUP=<dir> APPLY=1 # restore the backup, then prune
```

Without `APPLY=1` nothing mutates: the preview reports the composed expectation and what a prune would delete. `APPLY=1` requires a `MODE=`: the prune reduces a **superset** to the keep-lists, so applying without rebuilding one first would delete live records down to a state the lineage never described.
