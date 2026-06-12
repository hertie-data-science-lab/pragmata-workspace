# pragmata-workspace

Operational glue for running the [pragmata](https://github.com/) annotation
pipeline against the BSt (Bertelsmann Stiftung) publikationsbot. Holds
**scripts, configs, and specs** that are specific to the BSt operational setup
and deliberately do not belong in `pragmata` itself. It does **not** hold data
or outputs (those stay local and gitignored, see [Data & secrets](#data--secrets)).

## Pipeline

```
querygen_specs/ + _runtime.yaml
   │  run_querygen.sh   pragmata querygen (openai provider -> Azure v1 endpoint)
   ▼
querygen/runs/<stem>/synthetic_queries.csv        [gitignored data]
   │  run_bot.py        publikationsbot /stream -> import-ready JSONL
   ▼
publikationsbot_output/<stem>.jsonl               [gitignored data]
   │  build_combined.py pool successive runs + intersperse edgecases
   ▼
publikationsbot_output/<domain>_combined.jsonl    [gitignored data]
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
make monitor                              # compute snapshot -> logs/monitor.jsonl (no CLI tables)
make report-tables                        # render latest snapshot -> logs/analysis/<date>.md
make daily                                # export -> monitor -> analysis tables (the nightly job)
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
python scripts/merge_yaml.py querygen_specs/_runtime.yaml querygen_specs/<spec>.yaml > /tmp/m.yaml
pragmata querygen gen-queries --config-path /tmp/m.yaml --n-queries <N> --run-id <spec>

# bot (per spec) — NOT pragmata; scrapes the publikationsbot /stream endpoint
python scripts/run_bot.py --spec <spec>

# combine — NOT pragmata; pools runs + intersperses edgecases
python scripts/build_combined.py [<domain> ...]

# setup (per domain) — native pragmata, after merging the password overlay
jq --slurpfile s config/users.secrets.json \
  '$s[0] as $x | map(if $x[.username] then . + {password:$x[.username]} else . end)' \
  config/users.json > /tmp/u.json
pragmata annotation setup --users /tmp/u.json --config annotation_configs/<domain>.yaml

# import (per domain) — native pragmata, after stripping run_bot extras
jq -c '{query,answer,chunks,context_set,language}' \
  publikationsbot_output/<domain>_combined.jsonl > /tmp/c.jsonl
pragmata annotation import /tmp/c.jsonl --config annotation_configs/<domain>.yaml

# export (per domain) — native pragmata; submitted annotations -> per-task CSVs
# under annotation/exports/<domain>/ (gitignored)
pragmata annotation export --config annotation_configs/<domain>.yaml --export-id <domain> --base-dir .
```

```bash
# monitor - NOT a stage; reads live Argilla, appends logs/monitor.jsonl
scripts/monitor.py                 # all domains
scripts/monitor.py --domain <d>    # one domain (smoke test)
scripts/monitor.py --use-export    # reuse export.sh's per-domain CSVs for IAA
scripts/monitor.py --self-check    # offline cadence-guard check, no network
```

## Monitoring & analysis

A single nightly job — `scripts/daily.sh` (`make daily`) — chains three steps,
each runnable on its own:

```
export.sh            submitted annotations  -> annotation/exports/<domain>/  (overwrite per domain)
monitor.py --use-export   live counts + IAA + cadence -> append logs/monitor.jsonl
report_tables.py     latest snapshot        -> logs/analysis/<date>.md       (pure data tables)
```

- **`scripts/monitor.py`** computes the snapshot (counts / IAA / cadence) from
  live Argilla and appends one JSON line to `logs/monitor.jsonl`. It does **not**
  print tables to the terminal — it emits a one-line status; pass `--summary` for
  an ad-hoc table. Only IAA needs an export; `--use-export` reuses `export.sh`'s
  durable per-domain CSVs (degrades gracefully if absent) so the nightly job
  exports once, not twice.
- **`scripts/report_tables.py`** (`make report-tables`) renders a `monitor.jsonl`
  snapshot into deterministic markdown stats tables and writes
  `logs/analysis/<snapshot-date>.md` — pure data, no commentary (layer prose on
  top separately). `--line N` picks a snapshot; `--stdout` prints instead.
Three metrics (production vs calibration where it applies):
1. **Counts** — *submitted responses* (work units), *completed records* (met
   `min_submitted`), and *total records*. Record counts from Argilla
   `dataset.progress()`; response counts from the REST endpoint.
2. **Calibration agreement** — per-label Krippendorff α + an n_items-weighted
   mean, from pragmata's IAA over the calibration overlap.
3. **Cadence** — median seconds between consecutive submissions, **per-annotator**
   (true individual pace) and **global** (team throughput). A **session guard**
   drops gaps over `MONITOR_SESSION_GAP_MIN` (default 30 min) as pauses, listing
   each under `excluded_gaps` so nothing vanishes silently.
NB: 
- *Timestamps come from the REST endpoint.* Whereas Argilla SDK and export CSVs drop
  per-response submission times; the monitor reads each *response's* own `inserted_at` (nested in
  `responses[]`) + `user_id` - not the record-level `inserted_at` (the import
  time).
- *Daily cron* — one job runs export → monitor → analysis tables:
  ```cron
  0 2 * * * cd /home/azureuser/pragmata-workspace && bash scripts/daily.sh >> logs/runs/daily.log 2>&1
  ```

## Backup & restore

`scripts/argilla_backup.py` (`make backup`) takes a status-preserving snapshot of
**every** Argilla dataset - records, metadata, suggestions, and responses *with
their `submitted`/`draft`/`discarded` status* (the SDK's own `to_disk` drops
response status, so a naive dump can't restore annotations faithfully). Read-only;
writes a timestamped tree under `argilla_backup/<UTC-ts>/` plus a `manifest.json`.

```bash
make backup                                     # dump all datasets
make backup ARGS="restore argilla_backup/<ts>"  # restore a dump back into Argilla
```

`restore` recreates each dataset in its original workspace (or pass `--workspace
<ws>` to put them all in one), skipping any that already exist - it never
overwrites. Take a backup before any bulk or in-place edit of live annotation data.

## Layout

```
config/
  workspace.env        operational tunables (committed)
  users.json           annotator roster, no passwords (committed)
  users.secrets.json   username -> password overlay (gitignored)
annotation_configs/    per-domain pragmata annotation configs (committed)
querygen_specs/         per-domain querygen specs + _runtime.yaml (committed)
scripts/
  lib/common.sh        shared shell helpers (logging, env, guards, venv paths)
  lib/workspace.py     shared python helpers (paths, env loader, domains(), jsonl io)
  pipeline.sh          orchestrator: runs a slice of the stages (pre-flight, lock, parallelism)
  run_querygen.sh  run_bot.py  build_combined.py  setup.sh  import.sh  export.sh  (stages)
  daily.sh             nightly: export -> monitor -> analysis tables
  monitor.py           annotation monitor: progress, IAA, cadence -> logs/monitor.jsonl
  report_tables.py     render a monitor snapshot -> logs/analysis/<date>.md
  merge_yaml.py                                                        (helper)
logs/                  (gitignored)
  monitor.jsonl        metrics history (one snapshot per run, appended)
  analysis/<date>.md   daily stats tables (the deliverable)
  runs/                execution logs (run_bot.*, import.*, daily.log, ...)
```

All scripts share the same conventions via `scripts/lib/`: workspace-root
resolution, `.env` + `config/workspace.env` loading (existing environment wins),
stderr logging, and disk/env guards. See `scripts/lib/common.sh` for the shell
side and `scripts/lib/workspace.py` for the python side.

## Configuration

- **Secrets** live in `.env` (gitignored). Required keys:
  `ARGILLA_API_URL`, `ARGILLA_API_KEY` (annotation import/setup);
  `OPENAI_API_KEY`, `OPENAI_BASE_URL` (querygen). For Azure, set `OPENAI_API_KEY`
  to your Azure key and `OPENAI_BASE_URL` to `https://<resource>.openai.azure.com/openai/v1/`.
- **Operational tunables** live in `config/workspace.env` (queries-per-spec,
  bot concurrency, throttle, disk thresholds, the publikationsbot URL). 
- **querygen runtime** (model, reasoning effort, batching) lives in
  `querygen_specs/_runtime.yaml`, deep-merged with each per-spec YAML.
- **The domain list** is derived from `annotation_configs/*.yaml` - add a domain
  by adding its config + spec, nothing else to update.

## Annotator roster

`config/users.json` is the committed roster - usernames, roles, and workspace
assignments, **no passwords**. Passwords live in `config/users.secrets.json`
(gitignored).

## Data & secrets

Not version-controlled (gitignored): `.venv/`, `.env`, `config/users.secrets.json`,
`annotation/`, `publikationsbot_output/`, `querygen/`, `logs/`, `*.log`.
Everything tracked is scripts, configs, specs, and the annotator roster
(`config/users.json` - names + workspace assignments, no passwords).
