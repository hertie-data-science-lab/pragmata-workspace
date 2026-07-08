# pragmata-workspace

Operational glue for running the [pragmata](https://github.com/) annotation pipeline against
the BSt (Bertelsmann Stiftung) publikationsbot. Holds **scripts, configs, and specs** that
are specific to the BSt operational setup and deliberately do not belong in `pragmata`
itself. It does **not** hold data or outputs (those stay local and gitignored, see
[Data & secrets](docs/configuration.md#data--secrets)).

```mermaid
flowchart LR
  qg[querygen] --> bot[publikationsbot] --> comb[combine] --> imp["setup + import"] --> arg[(Argilla datasets)]
  arg -. planned .-> ev[pragmata eval]
```

## Setup

Clone, then:

1. `cp .env.example .env` and fill in the keys (Argilla, LLM, publikationsbot,
   `PRAGMATA_SRC`). See [Configuration](docs/configuration.md).
2. `cp configs/annotation/users.json.example configs/annotation/users.json` and
   `cp configs/annotation/users.secrets.json.example configs/annotation/users.secrets.json`,
   then fill in the real roster + passwords. Both stay gitignored. See
   [Annotator roster](docs/configuration.md#annotator-roster).
3. Point `PRAGMATA_SRC` at a `pragmata` checkout (provides the `pragmata` CLI) and create the
   `.venv/` it expects.
4. `make help` lists the targets; preview a run with `bash scripts/pipeline.sh --dry-run`.

Data, logs, reports and Argilla backups are **not** committed — see
[Data & secrets](docs/configuration.md#data--secrets) and
[Reproducibility](docs/reproducibility.md).

## Documentation

- [Annotation pipeline](docs/annotation.md) — build flow, orchestrator, logging/reporting,
  backup/restore.
- [Eval pipeline](docs/eval.md) — planned sibling (stub today).
- [Reproducibility](docs/reproducibility.md) — dated lineage bundles + `reproduce-curation`.
- [Configuration](docs/configuration.md) — secrets, tunables, annotator roster, data &
  secrets.

## Layout

```
.env.example           template for .env (copy to .env and fill in)
configs/               committed configs & specs (settings.conf, annotation/, eval/ stub)
reproducibility/       committed lineage records (one dated bundle per operation)
scripts/               committed pipeline code (pipeline.sh, daily.sh, annotation/, lib/, eval/ stub)
data/  logs/  reports/ pipeline I/O and outputs (gitignored except README + .gitkeep)
argilla_backup/        status-preserving Argilla dumps (gitignored, local/external)
tmp/                   one-off local scratch (gitignored)
```

Each top-level directory has its own README with the detail. All scripts share conventions
via `scripts/lib/` (workspace-root resolution, `.env` + `configs/settings.conf` loading,
stderr logging, disk/env guards) — see `scripts/lib/common.sh` (shell) and
`scripts/lib/workspace.py` (python).
