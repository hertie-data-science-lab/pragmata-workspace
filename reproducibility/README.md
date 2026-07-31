# reproducibility/

One dated bundle per operation or run: `reproducibility/<YYYY-MM-DD>-<name>/`, flat, in
date order. Nothing else lives here.

## The contract

Every bundle holds exactly two required files:

- **`README.md`** — what it records, why, and how to verify it. Three header lines beside the
  title are machine-read:
  - `kind: lineage` — a step that is **replayed in date order** to rebuild the live Argilla
    instance. `kind: freeze` — a **self-contained record** of a run that happened once,
    **never replayed**.
  - `status: retired` — optional. Drops the bundle out of replay composition: it declared an
    end state that was never applied and is no longer wanted, so replaying it would move live
    away from where it actually is. `kind:` stays whatever it was — the bundle is still an
    honest record of a decision, just not a live one.
  - `fetch: <where to get them>` — printed when a pin comes out `ABSENT`, for artefacts
    that live outside git.

  One further header is **informational only**, read by people rather than by `bundle.py`:
  `superseded-by: <bundle>` — a later bundle has taken over this one's role. The superseded
  bundle keeps its pins and must keep verifying; it is an archived record, not a retired one.
- **`pins.sha256`** — *generated*, never hand-edited. One `<sha256>  <path>` line per file,
  paths relative to the repo root, `sha256sum` format (the same format family as
  `scripts/transfer/sync.sh`'s `MANIFEST.sha256`, which uses tree-relative paths instead).

Small volatile artefacts (a log line, a manifest) are **copied into** the bundle and pinned
there, not pinned where they sit — a bundle must stay verifiable after the live tree moves
on. Large artefacts (the corpus, Argilla dumps, the PII-carrying export tree) stay out of
git and are pinned in place, with a `fetch:` header saying where to get them.

Bundles are records: a bundle is never rewritten. A later decision gets its own dated
bundle, and the composition rules below make it win.

Retention for the gitignored Argilla dumps under `argilla_backup/`: keep dumps referenced
by a bundle, plus the most recent.

## The bundles, in order

| Bundle | kind | Operation |
|---|---|---|
| `2026-05-initial-import/` | lineage | The original build + import of the corpus (querygen → bot → combine → import). Holds the original partition manifests, `provenance.md`, and the pins for the external corpus + pre-prune backup. |
| `2026-07-01-annotation-curation/` | lineage | The curation: 21,346 → 4,244 records. Holds `curation_record.md`, the per-dataset keep-lists, the as-run audit log, and the curated-corpus pins. |
| `2026-07-02-generation-descope/` | lineage, **retired** | Would have taken `Digitalisierung-und-Gemeinwohl_generation/generation_production` 100 → 80. Declared 2026-07-02 on the premise that the 20 dropped records held no responses; never applied; retired 2026-07-30 once the annotators had completed all 20. Excluded from composition. |
| `2026-07-29-eval-report-freeze/` | freeze, **superseded** | The first canonical annotation export for the BSt report, pinning all 41 files. Superseded by `2026-07-30-eval-report/`; retained as an archived record and still verifies. Its export tree predates pseudonymisation and holds real annotator names, so it stays local. |
| `2026-07-30-eval-report/` | freeze, **superseded** | The first deliverable set on the pseudonymised export (41 files) plus its report CSVs (11 files). Superseded by `2026-07-31-eval-report/`, which pins the same export with the current report schema; retained as an archived record and still verifies. |
| `2026-07-31-eval-report/` | freeze | The canonical data behind every human-annotation and fairness number in the BSt report: the pseudonymised export (41 files) **and** the report CSVs with their `.provenance.json` files and data dictionary (11 files). |

The lineage composes to an end state of **4,244 records** across 48 datasets — the
2026-07-01 keep-lists, since the descope is retired. That matches live. Whether it still
does is what `repro-reproduce`'s preview tells you; it is not an assumption.

Composition is per keep-list **override, not union** — a later bundle's
`<workspace>__<dataset>.ids` replaces the earlier one wholesale, because a keep-list
declares that dataset's end state rather than adding to it. That rule is what makes a
descope reachable at all, so it stays even though no active bundle currently exercises it.

## The three targets

All of `scripts/repro/bundle.py`, behind `make`:

```
make repro-pin NAME=<name> PATHS="<path ...>"   # create today's bundle: pins + a README stub
make repro-verify [PIN=<bundle-dir>]            # check pins; default every bundle
make repro-reproduce PIN=<bundle-dir>           # replay a lineage bundle (MODE= APPLY= BACKUP=)
```

`repro-verify` reports every pin as `OK`, `MISMATCH` or `ABSENT`. `ABSENT` is the expected
result for the out-of-git artefacts — it prints the bundle's `fetch:` line. `MISMATCH` is
never expected. `bundle.py verify` exits **0** all OK, **2** on any mismatch, **3** on
absences only; through `make` those surface as `Error 2` / `Error 3` on stderr, since make
returns its own 2 for any failed recipe.

`repro-reproduce` refuses `kind: freeze`. For a lineage bundle it composes every lineage
bundle's keep-lists in date order, prints the expected end state, then delegates: the
existing `scripts/annotation/import.sh` (`MODE=structure`) or `argilla_backup.py restore`
(`MODE=responses BACKUP=<dir>`) builds the superset, and
`scripts/annotation/prune_to_keeplist.py` reduces it to the composed state. Without
`APPLY=1` it previews only, and that preview doubles as the verification of live.

Reusable tooling lives in `scripts/`, not in the bundles. See the
[Reproducibility](../docs/reproducibility.md) doc.
