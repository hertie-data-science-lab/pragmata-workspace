# pragmata-workspace

Operational glue for running the [pragmata](https://github.com/) annotation
pipeline against the BSt (Bertelsmann Stiftung) publikationsbot. Holds
**scripts, configs, and specs** that are specific to the BSt operational setup
and deliberately do not belong in `pragmata` itself. It does **not** hold data
or outputs (those stay local and gitignored, see [Data & secrets](#data--secrets)).

## Setup

Clone, then:

1. `cp .env.example .env` and fill in the keys (Argilla, LLM, publikationsbot,
   `PRAGMATA_SRC`). See [Configuration](#configuration).
2. `cp configs/annotation/users.json.example configs/annotation/users.json` and
   `cp configs/annotation/users.secrets.json.example configs/annotation/users.secrets.json`,
   then fill in the real roster + passwords. Both stay gitignored. See
   [Annotator roster](#annotator-roster).
3. Point `PRAGMATA_SRC` at a `pragmata` checkout (provides the `pragmata` CLI) and
   create the `.venv/` it expects.
4. `make help` lists the targets; preview a run with `bash scripts/pipeline.sh --dry-run`.

Data, logs, reports and Argilla backups are **not** committed — see
[Data & secrets](#data--secrets) and [Reproducibility](#reproducibility).

## Pipeline

```
configs/annotation/querygen_specs/ + _runtime.yaml
   │  run_querygen.sh   pragmata querygen (openai provider -> Azure v1 endpoint)
   ▼
data/querygen/runs/<stem>/synthetic_queries.csv                    [gitignored data]
   │  run_bot.py        publikationsbot /stream -> import-ready JSONL
   ▼
data/publikationsbot/<stem>.jsonl                          [gitignored data]
   │  build_combined.py pool successive runs + intersperse edgecases
   ▼
data/publikationsbot/<domain>_combined.jsonl               [gitignored data]
   │  setup.sh (provision) + import.sh (clean + load)   pragmata annotation
   ▼
Argilla datasets (3 tasks × {production, calibration})
```

One orchestrator, `scripts/pipeline.sh`, runs any contiguous slice of the
stages over an optional domain filter, owning the cross-cutting concerns the
stage scripts don't:
- stage-aware pre-flight,
- lockfile,
- bot parallelism,
- tee logging,
- continue-on-error.

| Invocation                   | Covers              |
| ---------------------------- | ------------------- |
| `pipeline.sh`                | full pipeline       |
| `pipeline.sh --to bot`       | querygen + bot      |
| `pipeline.sh --from combine` | combine + setup + import |
| `pipeline.sh --only setup`   | provision workspaces/users |
| `pipeline.sh --only import`  | import every domain |

`--filter` takes domains (querygen/bot expand each to `<domain>` +
`<domain>_edgecase`); `--dry-run` prints the plan without running. Each stage
script stays runnable on its own.

`setup.sh` (provision workspaces + users) and `import.sh` (clean + load) are
thin wrappers over pragmata's native `annotation setup` / `annotation import`.
The only workspace-specific bits are the password merge in `setup.sh` (see
[Annotator roster](#annotator-roster)) and `import.sh`'s inline `jq` projection
(stripping run_bot.py extras). For anything non-standard, call the pragmata CLI directly.

## Make targets

```bash
make help                                 # list targets

# single stages
make querygen                             # all specs   (SPECS=a,b to filter)
make bot                                  # all specs   (SPEC=x to filter)
make combine                              # all domains (DOMAINS="a b" to filter)
make setup                                # provision one domain (workspaces + users; DOMAIN= to filter)
make import                               # import one domain (DOMAIN= to filter)
make export                               # export current annotations to CSV (DOMAIN= to filter)
make log                                  # log a snapshot -> logs/annotation/log.jsonl (no CLI tables)
make report                               # render latest snapshot -> reports/annotation/<date>/ (md + plots)
make daily                                # export -> log.jsonl (the nightly logging job)
make backup                               # status-preserving backup of all Argilla datasets

# or the orchestrated pipeline (dry-run preview: bash scripts/pipeline.sh --dry-run)
make pipeline                             # full pipeline, all domains
make pipeline TO=bot FILTER=gesundheit    # querygen + bot for one domain
tmux new -s pipeline 'make pipeline'      # unattended, survives disconnect
```

## Under the hood (native commands)

Every stage is a thin wrapper. Here's the actual command each runs, per item -
so you can run any stage by hand or see exactly what's executed:

```bash
# querygen (per spec) — native pragmata, after merging _runtime + spec
python scripts/annotation/merge_yaml.py configs/annotation/querygen_specs/_runtime.yaml configs/annotation/querygen_specs/<spec>.yaml > /tmp/m.yaml
pragmata querygen gen-queries --config-path /tmp/m.yaml --n-queries <N> --run-id <spec>

# bot (per spec) — NOT pragmata; scrapes the publikationsbot /stream endpoint
python scripts/annotation/run_bot.py --spec <spec>

# combine — NOT pragmata; pools runs + intersperses edgecases
python scripts/annotation/build_combined.py [<domain> ...]

# setup (per domain) — native pragmata, after merging the password overlay
jq --slurpfile s configs/annotation/users.secrets.json \
  '$s[0] as $x | map(if $x[.username] then . + {password:$x[.username]} else . end)' \
  configs/annotation/users.json > /tmp/u.json
pragmata annotation setup --users /tmp/u.json --config configs/annotation/domains/<domain>.yaml

# import (per domain) — native pragmata, after stripping run_bot extras
jq -c '{query,answer,chunks,context_set,language}' \
  data/publikationsbot/<domain>_combined.jsonl > /tmp/c.jsonl
pragmata annotation import /tmp/c.jsonl --config configs/annotation/domains/<domain>.yaml --base-dir data/

# export (per domain) — native pragmata; submitted annotations -> per-task CSVs
# under data/annotation/exports/<domain>/ (gitignored)
pragmata annotation export --config configs/annotation/domains/<domain>.yaml --export-id <domain> --base-dir data/
```

```bash
# log - NOT a stage; reads live Argilla, appends logs/annotation/log.jsonl
scripts/annotation/log.py                 # all domains
scripts/annotation/log.py --domain <d>    # one domain (smoke test)
scripts/annotation/log.py --use-export    # reuse export.sh's per-domain CSVs for IAA
scripts/annotation/log.py --self-check    # offline cadence-guard check, no network
```

## Logging & reporting

Two halves, deliberately split:

- **Logging** is automatic and daily: the nightly job appends one snapshot to a JSONL.
- **Reporting** is manual: render the latest (or any) snapshot into markdown + plots.

The nightly job — `scripts/daily.sh` (`make daily`) — chains two logging steps,
each runnable on its own:

```
export.sh            submitted annotations  -> data/annotation/exports/<domain>/  (overwrite per domain)
log.py --use-export  live counts + IAA + cadence -> append logs/annotation/log.jsonl
```

Reporting is on demand (`make report`):

```
report_tables.py     latest snapshot -> reports/annotation/<date>/report.md   (pure data tables)
plot_summary.py      latest snapshot -> reports/annotation/<date>/*.png       (plots)
                     reports/annotation/_latest -> <date>/                     (symlink to newest)
```

- **`scripts/annotation/log.py`** (`make log`) computes the snapshot (counts / IAA /
  cadence) from live Argilla and appends one JSON line to `logs/annotation/log.jsonl`.
  It does **not** print tables to the terminal — it emits a one-line status; pass
  `--summary` for an ad-hoc table. Only IAA needs an export; `--use-export` reuses
  `export.sh`'s durable per-domain CSVs (degrades gracefully if absent) so the nightly
  job exports once, not twice.
- **`make report`** runs both reporters into one dated subdir and repoints `_latest`:
  - **`report_tables.py`** renders a `log.jsonl` snapshot into deterministic markdown
    stats tables (`report.md`) — pure data, no commentary (layer prose on top
    separately). `--line N` picks a snapshot; `--stdout` prints instead.
  - **`plot_summary.py`** renders the PNGs into the same dir: progress (burn-up +
    burn-down), label prevalence, pace, discards.
Three metrics (production vs calibration where it applies):
1. **Counts** — *submitted responses* (work units), *completed records* (met
   `min_submitted`), and *total records*. Record counts from Argilla
   `dataset.progress()`; response counts from the REST endpoint.
2. **Calibration agreement** — per-label Krippendorff α + an n_items-weighted
   mean, from pragmata's IAA over the calibration overlap.
3. **Cadence** — median seconds between consecutive submissions, **per-annotator**
   (true individual pace) and **global** (team throughput). A **session guard**
   drops gaps over `LOG_SESSION_GAP_MIN` (default 30 min) as pauses, listing
   each under `excluded_gaps` so nothing vanishes silently.
NB: 
- *Timestamps come from the REST endpoint.* Whereas Argilla SDK and export CSVs drop
  per-response submission times; log.py reads each *response's* own `inserted_at` (nested in
  `responses[]`) + `user_id` - not the record-level `inserted_at` (the import
  time).
- *Daily cron* — one job logs a snapshot (export → log.jsonl); reporting stays manual:
  ```cron
  0 2 * * * /home/azureuser/pragmata-workspace/scripts/daily.sh > /dev/null 2>&1
  ```

## Backup & restore

`scripts/annotation/argilla_backup.py` (`make backup`) takes a status-preserving snapshot of
**every** Argilla dataset - records, metadata, suggestions, and responses *with
their `submitted`/`draft`/`discarded` status* (the SDK's own `to_disk` drops
response status, so a naive dump can't restore annotations faithfully). Read-only;
writes a timestamped tree under `argilla_backup/<UTC-ts>/` plus a `manifest.json`.

```bash
make backup                                             # dump all datasets
make backup ARGS="restore argilla_backup/<ts>"          # preview restoring the FULL snapshot (dry-run)
make backup ARGS="restore argilla_backup/<ts> --apply"  # write it
```

`restore` restores the full snapshot (fields, metadata, suggestions, responses) back
into Argilla - creating any dataset that no longer exists, and writing onto ones that
still exist alike. It **always previews first** (record counts, and any response/metadata
that would be overwritten with a different live value) and only writes with `--apply`.

Narrow the scope with `--workspace WS` / `--dataset NAME` / `--record-id ID` (each
repeatable, AND'd together; omit for "everything in the manifest"), or restrict which
attributes get restored with `--only {metadata,suggestions,responses}` (fields are
always restored; omit for the full snapshot). Take a fresh backup before restoring onto
a live dataset - restoring reverts to that point in time, including any annotator
activity recorded after the snapshot for the records/attributes in scope (the preview
flags this before you apply).

## Reproducibility

`reproducibility/` holds one **dated bundle per operation** that produced the live
instance (chronological, migration-style):
1. `2026-05-initial-import/` — the original build + import: the original partition
   manifests, `checksums.sha256` pinning the full source corpus + pre-prune backup
   (external), and `provenance.md` (querygen model/dates, the non-determinism caveat).
2. `2026-07-01-annotation-curation/` — the curation (21,346 → 4,244 records): the honest
   `curation_record.md`, the per-dataset **keep-lists** (the declared end state), and
   `apply_log.jsonl` (audit of what was deleted).

Reproduction is **declarative**: the keep-lists are the desired state, and the reusable
tool `scripts/annotation/prune_to_keeplist.py` reduces any superset to them (the
`kubectl apply --prune` / `terraform` model). `make reproduce-curation` chains it; it
leaves `import.sh` / the `import` subcommand untouched. Point
`ARGILLA_API_URL`/`ARGILLA_API_KEY` at the target, fetch the pinned artifacts, then:

```
make reproduce-curation                                     # preview = verify (expect delete 0, 0 missing)
make reproduce-curation MODE=structure APPLY=1              # import the full corpus, then prune to keep-lists
make reproduce-curation MODE=responses BACKUP=<dir> APPLY=1 # restore the backup, then prune to keep-lists
```

Both build a superset (import the full corpus, or restore the backup) then prune down to
the keep-lists — a plain re-import cannot reproduce the curated set on its own (see
[Known limitations](#known-limitations)). Full detail in the bundle's `README.md`.

## Layout

```
.env.example           template for .env (committed; copy to .env and fill in)
configs/
  settings.conf        workspace-global operational tunables (committed, loaded for all scripts)
  annotation/          annotation-stage configs
    domains/           per-domain pragmata annotation task YAMLs (committed)
    querygen_specs/    per-domain querygen specs + _runtime.yaml (committed)
    users.json         annotator roster, no passwords (gitignored, local)
    users.secrets.json username -> password overlay (gitignored)
    *.example          committed templates for the two gitignored files above
reproducibility/       committed lineage records (one dated bundle per operation)
  2026-05-initial-import/          original manifests + checksums (corpus+backup) + provenance
  2026-07-01-annotation-curation/  curation record + keep-lists + apply_log
scripts/
  lib/common.sh        shared shell helpers (logging, env, guards, venv paths)
  lib/workspace.py     shared python helpers (paths, env loader, domains(), jsonl io)
  pipeline.sh          orchestrator: runs a slice of the stages (pre-flight, lock, parallelism)
  daily.sh             nightly logging: export -> log.jsonl
  annotation/
    run_querygen.sh  run_bot.py  build_combined.py  setup.sh  import.sh  export.sh  (stages)
    log.py             annotation logger: progress, IAA, cadence -> logs/annotation/log.jsonl
    report_tables.py   render a snapshot -> reports/annotation/<date>/report.md
    plot_summary.py    render summary plots -> reports/annotation/<date>/*.png
    argilla_backup.py  status-preserving dump/restore (make backup)
    prune_to_keeplist.py  reduce live Argilla to a keep-list (make reproduce-curation)
    merge_yaml.py      (helper)
data/                  (gitignored except README + .gitkeep scaffolds)
  annotation/
    exports/           export CSVs (one subdir per domain) — carries annotator PII
    imports/           import artifacts (partition manifests)
  querygen/runs/       querygen output (pragmata tool sibling of annotation/)
  publikationsbot/     bot output JSONL (sibling of annotation/)
logs/                  (gitignored except README + .gitkeep)
  annotation/
    log.jsonl          metrics history (one snapshot per run, appended)
    *.log              execution logs (run_bot.*, pipeline.log, ...)
reports/               (gitignored except README + .gitkeep)
  annotation/
    <date>/            one report per snapshot: report.md + plots (PNGs)
    _latest -> <date>/ symlink to the newest report
argilla_backup/        status-preserving Argilla dumps (gitignored, local/external)
tmp/                   one-off local scratch (gitignored)
```

All scripts share the same conventions via `scripts/lib/`: workspace-root
resolution, `.env` + `configs/settings.conf` loading (existing environment wins),
stderr logging, and disk/env guards. See `scripts/lib/common.sh` for the shell
side and `scripts/lib/workspace.py` for the python side.

## Configuration

- **Secrets** live in `.env` (gitignored) — copy `.env.example` and fill in. Required
  keys: `ARGILLA_API_URL`, `ARGILLA_API_KEY` (annotation import/setup);
  `OPENAI_API_KEY`, `OPENAI_BASE_URL` (querygen); `PUBLIKATIONSBOT_URL` (bot);
  `PRAGMATA_SRC` (path to the `pragmata` checkout). For Azure, set `OPENAI_API_KEY`
  to your Azure key and `OPENAI_BASE_URL` to `https://<resource>.openai.azure.com/openai/v1/`.
- **Operational tunables** live in `configs/settings.conf` (queries-per-spec,
  bot concurrency, throttle, disk thresholds). Committed and tracked.
- **querygen runtime** (model, reasoning effort, batching) lives in
  `configs/annotation/querygen_specs/_runtime.yaml`, deep-merged with each per-spec YAML.
- **The domain list** is derived from `configs/annotation/domains/*.yaml` - add a domain
  by adding its config + spec, nothing else to update.

## Annotator roster

`configs/annotation/users.json` is the roster - usernames, roles, and workspace
assignments, **no passwords**. It is kept **local (gitignored)**, not
version-controlled, since it carries annotator names. Passwords live in
`configs/annotation/users.secrets.json` (also gitignored). Both have committed
`.example` templates (dummy values) showing the expected shape — copy and fill in.

## Data & secrets

Not version-controlled (gitignored): `.venv/`, `.env`,
`configs/annotation/users.secrets.json`, `configs/annotation/users.json`, `data/`,
`logs/`, `reports/`, `argilla_backup/`, `tmp/`, `*.log`. Everything tracked is
scripts, configs, specs, and the `reproducibility/` bundle.

### What's not committed, and how to obtain it

| Not in git | Why | How to get it |
|---|---|---|
| `data/publikationsbot/*_combined*.jsonl` | large (~52M curated / ~549M full) | fetch the corpus artifact pinned in `reproducibility/.../checksums.sha256`, or regenerate via `make pipeline` (querygen is non-deterministic) |
| `data/annotation/exports/` | annotator **PII** (real names, notes) | re-export from live Argilla (`make export`) |
| `argilla_backup/` | large (~2.1G) | the pre-prune snapshot is an external archive, pinned by `checksums.sha256` |
| `.env`, `users.json`, `users.secrets.json` | secrets / names | copy the committed `.example` templates and fill in |

The curated annotation corpus is reproducible from the `reproducibility/` bundle —
see [Reproducibility](#reproducibility).

## Known limitations

These live in the `pragmata` pipeline (not this glue repo) and are why reproduction
needs a separate prune step rather than a plain re-import. Recommended future work:

- **Import can't express per-task membership.** Every query fans out into all three
  tasks; the manifest only flags each record calibration-vs-production. A curated set
  that is heterogeneous per (query × task) can't be expressed by import alone.
- **The partition manifest is append-only ("manifest lock").** It never shrinks or
  reshuffles, so deleting queries from the source and re-importing re-adds them. The
  on-disk `partition.meta.json` therefore drifts from the curated live state. A
  first-class `prune`/`reconcile` subcommand (making the manifest the source of truth)
  would remove the need for the external keep-lists here.
- **Exports overwrite in place.** `export.sh` writes `data/annotation/exports/<domain>/`
  with no timestamp/versioning or run lineage; a stale CSV is indistinguishable from a
  fresh one. Versioned export snapshots would fix this.
- **Three identifiers for one domain** (import `partition_scope` `BIL`, export
  `export_id` slug, Argilla `dataset_id` `""`), wired only by shell-script convention.
