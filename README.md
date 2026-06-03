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
[Annotator roster](#annotator-roster)) and `clean_for_import.sh` (stripping
run_bot.py extras). For anything non-standard, call the pragmata CLI directly.

## Make targets

```bash
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
  workspace.env        operational tunables (committed)
  users.json           annotator roster, no passwords (committed)
  users.secrets.json   username -> password overlay (gitignored)
annotation_configs/    per-domain pragmata annotation configs (committed)
querygen_specs/         per-domain querygen specs + _runtime.yaml (committed)
scripts/
  lib/common.sh        shared shell helpers (logging, env, guards, venv paths)
  lib/workspace.py     shared python helpers (paths, env loader, domains(), jsonl io)
  pipeline.sh          orchestrator: runs a slice of the stages (pre-flight, lock, parallelism)
  run_querygen.sh  run_bot.py  build_combined.py  setup.sh  import.sh   (stages)
  clean_for_import.sh  merge_yaml.py                                    (helpers)
```

All scripts share the same conventions via `scripts/lib/`: workspace-root
resolution, `.env` + `config/workspace.env` loading (existing environment wins),
stderr logging, and disk/env guards. See `scripts/lib/common.sh` for the shell
side and `scripts/lib/workspace.py` for the python side.

## Configuration

- **Secrets** live in `.env` (gitignored). Required keys:
  `ARGILLA_API_URL`, `ARGILLA_API_KEY` (annotation import/setup);
  `OPENAI_API_KEY`, `OPENAI_BASE_URL` (querygen). 
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
