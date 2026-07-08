# Configuration

- **Secrets** live in `.env` (gitignored) — copy `.env.example` and fill in. Required keys:
  `ARGILLA_API_URL`, `ARGILLA_API_KEY` (annotation import/setup); `OPENAI_API_KEY`,
  `OPENAI_BASE_URL` (querygen); `PUBLIKATIONSBOT_URL` (bot); `PRAGMATA_SRC` (path to the
  `pragmata` checkout). For Azure, set `OPENAI_API_KEY` to your Azure key and
  `OPENAI_BASE_URL` to `https://<resource>.openai.azure.com/openai/v1/`.
- **Operational tunables** live in `configs/settings.conf` (queries-per-spec, bot
  concurrency, throttle, disk thresholds) — committed.
- **querygen runtime** (model, reasoning effort, batching) lives in
  `configs/annotation/querygen_specs/_runtime.yaml`, deep-merged with each per-spec YAML.
- **The domain list** is derived from `configs/annotation/domains/*.yaml` — add a domain by
  adding its config + spec, nothing else to update.

## Annotator roster

`configs/annotation/users.json` is the roster — usernames, roles, and workspace
assignments, **no passwords**. Kept **local (gitignored)** since it carries annotator names.
Passwords live in `configs/annotation/users.secrets.json` (also gitignored). Both have
committed `.example` templates (dummy values) showing the expected shape — copy and fill in.

## Data & secrets

Not version-controlled (gitignored): `.venv/`, `.env`,
`configs/annotation/users.secrets.json`, `configs/annotation/users.json`, `data/`, `logs/`,
`reports/`, `argilla_backup/`, `tmp/`, `*.log`. Everything tracked is scripts, configs,
specs, and the `reproducibility/` bundle.

### What's not committed, and how to obtain it

| Not in git | Why | How to get it |
|---|---|---|
| `data/publikationsbot/*_combined*.jsonl` | large (~52M curated / ~549M full) | fetch the corpus artifact pinned in `reproducibility/.../checksums.sha256`, or regenerate via `make pipeline` (querygen is non-deterministic) |
| `data/annotation/exports/` | annotator **PII** (real names, notes) | re-export from live Argilla (`make export`) |
| `argilla_backup/` | large (~2.1G) | the pre-prune snapshot is an external archive, pinned by `checksums.sha256` |
| `.env`, `users.json`, `users.secrets.json` | secrets / names | copy the committed `.example` templates and fill in |

The curated annotation corpus is reproducible from the `reproducibility/` bundle — see
[Reproducibility](reproducibility.md).
