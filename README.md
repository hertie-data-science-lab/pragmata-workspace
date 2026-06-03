# pragmata-workspace

Operational glue for running the [pragmata](https://github.com/) annotation
pipeline against the BSt (Bertelsmann Stiftung) publikationsbot. This repo holds
**scripts, configs, and specs** that are specific to the BSt operational setup
and deliberately do not belong in `pragmata` itself. It does **not** hold data
or outputs (those stay local and gitignored, see [Data & secrets](#data--secrets)).

## Pipeline

```
querygen_specs/ + _runtime.yaml
   │  run_querygen.sh   pragmata querygen (via pragmata_azure.py wrapper)
   ▼
querygen/runs/<stem>/synthetic_queries.csv        [gitignored data]
   │  run_bot.py        publikationsbot /stream -> import-ready JSONL
   ▼
publikationsbot_output/<stem>.jsonl               [gitignored data]
   │  build_combined.py pool successive runs + intersperse edgecases
   ▼
publikationsbot_output/<domain>_combined.jsonl    [gitignored data]
   │  clean_for_import.sh + setup_and_import.sh    pragmata annotation setup/import
   ▼
Argilla datasets (3 tasks × {production, calibration})
```

One orchestrator, `scripts/pipeline.sh`, runs any contiguous slice of the
stages over an optional domain filter, owning the cross-cutting concerns the
stage scripts don't: stage-aware pre-flight, a lockfile, bot parallelism, tee
logging, and continue-on-error.

| Invocation                   | Covers              |
| ---------------------------- | ------------------- |
| `pipeline.sh`                | full pipeline       |
| `pipeline.sh --to bot`       | querygen + bot      |
| `pipeline.sh --from combine` | combine + import    |
| `pipeline.sh --only import`  | import every domain |

`--filter` takes domains (querygen/bot expand each to `<domain>` +
`<domain>_edgecase`); `--dry-run` prints the plan without running. Each stage
script stays runnable on its own.

The import stage (`setup_and_import.sh`) is a deliberately thin wrapper over
pragmata's native `annotation setup` / `annotation import` — the only
workspace-specific step is `clean_for_import.sh` (stripping run_bot.py extras).
For non-standard imports, call the pragmata CLI directly with the flags you need.

## Quickstart

```bash
# one-time setup
cp .env.example .env                      # fill in Argilla + Azure OpenAI keys
cp config/users.example.json config/users.json   # fill in real annotators
az login --use-device-code                # publikationsbot auth (run_bot.py)

make help                                 # list targets

# single stages
make querygen                             # all specs   (SPECS=a,b to filter)
make bot                                  # all specs   (SPEC=x to filter)
make combine                              # all domains (DOMAINS="a b" to filter)
make import DOMAIN=gesundheit             # one domain

# or the orchestrated pipeline (preview any slice with `make plan ...`)
make pipeline                             # full pipeline, all domains
make pipeline TO=bot FILTER=gesundheit    # querygen + bot for one domain
tmux new -s pipeline 'make pipeline'      # unattended, survives disconnect
```

## Layout

```
config/
  workspace.env        operational tunables (committed; see below)
  users.json           Argilla users incl. passwords (gitignored)
  users.example.json   template
annotation_configs/    per-domain pragmata annotation configs (committed)
querygen_specs/         per-domain querygen specs + _runtime.yaml (committed)
scripts/
  lib/common.sh        shared shell helpers (logging, env, guards, venv paths)
  lib/workspace.py     shared python helpers (paths, env loader, domains(), jsonl io)
  pipeline.sh          orchestrator: runs a slice of the stages (pre-flight, lock, parallelism)
  run_querygen.sh  run_bot.py  build_combined.py  setup_and_import.sh   (stages)
  clean_for_import.sh  merge_yaml.py  pragmata_azure.py                 (helpers)
docs/                  design notes / specs
```

All scripts share the same conventions via `scripts/lib/`: workspace-root
resolution, `.env` + `config/workspace.env` loading (existing environment wins),
stderr logging, and disk/env guards. See `scripts/lib/common.sh` for the shell
side and `scripts/lib/workspace.py` for the python side.

## Configuration

- **Secrets** live in `.env` (Argilla + Azure OpenAI keys). Template:
  `.env.example`.
- **Operational tunables** live in `config/workspace.env` (queries-per-spec,
  bot concurrency, throttle, disk thresholds, the publikationsbot URL). Override
  any of them for a single run by exporting first, e.g.
  `N_PARALLEL_BOTS=8 make pipeline`, or pass `JOBS=8`.
- **querygen runtime** (model, reasoning effort, batching) lives in
  `querygen_specs/_runtime.yaml`, deep-merged with each per-spec YAML.
- **The domain list** is derived from `annotation_configs/*.yaml` — add a domain
  by adding its config + spec, nothing else to update.

## The pragmata_azure.py wrapper

pragmata's API-key registry does not yet include `azure_openai`.
`scripts/pragmata_azure.py` monkey-patches that registry at import time, loads
the workspace env, then dispatches to pragmata's CLI unchanged. Delete it once
pragmata supports `azure_openai` natively (a one-line registry change upstream).

## Data & secrets

Not version-controlled (gitignored): `.venv/`, `.env`, `config/users.json`,
`annotation/`, `publikationsbot_output/`, `querygen/`, `logs/`, `*.log`.
Everything tracked is scripts, configs, specs, and docs.
