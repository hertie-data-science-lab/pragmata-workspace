# Reproducibility

`reproducibility/` holds one **dated bundle per operation or run**, all in the same shape:
`<YYYY-MM-DD>-<name>/` with a `README.md` (carrying a `kind:` header) and a generated
`pins.sha256`. The bundle contract, the retention policy and the composition rules live in
[`reproducibility/README.md`](../reproducibility/README.md); this page is the entrypoint.

| Bundle | kind | Operation |
|---|---|---|
| `2026-05-initial-import/` | lineage | Original build + import: the partition manifests, `provenance.md` (querygen model/dates, the non-determinism caveat), and pins for the external corpus + pre-prune backup. |
| `2026-07-01-annotation-curation/` | lineage | The curation, 21,346 → 4,244 records: `curation_record.md`, the per-dataset keep-lists, `apply_log.jsonl`. |
| `2026-07-02-generation-descope/` | lineage | Amends one dataset to 80 records, taking the lineage end state to **4,224**. |
| `2026-07-29-eval-report-freeze/` | freeze | The canonical annotation export behind the BSt report's human-annotation and fairness numbers. Never replayed. |

`kind: lineage` bundles are replayed in date order to rebuild the live Argilla instance;
`kind: freeze` bundles are self-contained records of a single run.

## Targets

```
make repro-verify [PIN=<bundle-dir>]            # per-file OK/MISMATCH/ABSENT (Error 2 mismatch, Error 3 absent only)
make repro-pin NAME=<name> PATHS="<path ...>"   # create today's bundle: pins + a README stub
make repro-reproduce PIN=<bundle-dir>           # replay a lineage bundle (MODE= BACKUP= APPLY=)
```

`ABSENT` is expected for artefacts held outside git (the corpus, Argilla dumps, the
PII-carrying export tree); verify prints the bundle's `fetch:` line so you know where to
get them. `MISMATCH` always means something is wrong.

## Replaying the lineage

Reproduction is **declarative**: the keep-lists are the desired end state, and
`scripts/annotation/prune_to_keeplist.py` reduces any superset to them (the
`kubectl apply --prune` / `terraform` model). A plain re-import cannot reach the curated set
on its own — import fans every query into all three tasks, so the superset has to be built
and then pruned down.

`repro-reproduce` composes **every** lineage bundle's keep-lists in date order before
pruning, later dates overriding earlier ones per dataset. That is what makes the replay land
on the live 4,224-record state rather than stopping at the 2026-07-01 bundle's 4,244.

Point `ARGILLA_API_URL`/`ARGILLA_API_KEY` at the target, fetch the pinned artefacts, then:

```
make repro-reproduce PIN=2026-07-01-annotation-curation                                     # preview = verify live
make repro-reproduce PIN=2026-07-01-annotation-curation MODE=structure APPLY=1              # import the full corpus, then prune
make repro-reproduce PIN=2026-07-01-annotation-curation MODE=responses BACKUP=<dir> APPLY=1 # restore the backup, then prune
```

Without `APPLY=1` nothing mutates: the preview reports the composed expectation and what a
prune would delete, which doubles as the check that live still matches the lineage.
