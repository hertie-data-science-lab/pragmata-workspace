# Reproducibility

`reproducibility/` holds one **dated bundle per operation or run**, all in the same shape: `<YYYY-MM-DD>-<name>/` with a `README.md` (carrying a `kind:` header) and a generated `pins.sha256`. The bundle contract, the bundle list, the retention policy, the composition rules and the three `repro-*` targets all live in [`reproducibility/README.md`](../reproducibility/README.md); this page is the entrypoint and the replay how-to.

`kind: lineage` bundles are replayed in date order to rebuild the live Argilla instance; `kind: freeze` bundles are self-contained records of a single run, never replayed. A `status: retired` header drops a bundle out of replay while keeping it on the record. Which bundle is which is in [the bundle list](../reproducibility/README.md#the-bundles-in-order).

## Replaying the lineage

Reproduction is **declarative**: the keep-lists are the desired end state, and `scripts/annotation/prune_to_keeplist.py` reduces any superset to them (the `kubectl apply --prune` / `terraform` model). A plain re-import cannot reach the curated set on its own - import fans every query into all three tasks, so the superset has to be built and then pruned down.

`repro-reproduce` composes **every** active lineage bundle's keep-lists in date order before pruning, later dates overriding earlier ones per dataset, and reports what it skipped. It prints the composed end state it is working toward ([what that currently is](../reproducibility/README.md#the-bundles-in-order)); whether live still matches it is what the preview reports, and not something to assume.

Point `ARGILLA_API_URL`/`ARGILLA_API_KEY` at the target, fetch the pinned artefacts, then:

```
make repro-reproduce PIN=2026-07-01-annotation-curation                                     # preview = verify live
make repro-reproduce PIN=2026-07-01-annotation-curation MODE=structure APPLY=1              # import the full corpus, then prune
make repro-reproduce PIN=2026-07-01-annotation-curation MODE=responses BACKUP=<dir> APPLY=1 # restore the backup, then prune
```

Without `APPLY=1` nothing mutates: the preview reports the composed expectation and what a prune would delete. `APPLY=1` requires a `MODE=`: the prune reduces a **superset** to the keep-lists, so applying without rebuilding one first would delete live records down to a state the lineage never described.
