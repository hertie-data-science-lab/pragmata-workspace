# Configuration

- **Secrets** live in `.env` (gitignored) - copy `.env.example` and fill in. Required keys:
  `ARGILLA_API_URL`, `ARGILLA_API_KEY` (annotation import/setup); `OPENAI_API_KEY`,
  `OPENAI_BASE_URL` (querygen); `PUBLIKATIONSBOT_URL` (bot); `PRAGMATA_EVAL_SRC` (path to the
  eval-pin `pragmata` checkout - the annotation pragmata needs no key, it is git-pinned in
  `pyproject.toml`); `EVAL_BLOB_ACCOUNT`, `EVAL_BLOB_CONTAINER`, `EVAL_BLOB_SAS` (eval data
  transport only - see [Eval data transport](eval-data-transport.md)). For Azure, set
  `OPENAI_API_KEY` to your Azure key and
  `OPENAI_BASE_URL` to `https://<resource>.openai.azure.com/openai/v1/`.
  The scripts under `scripts/` load `.env` themselves (via `scripts/lib/common.sh`), but for
  ad-hoc `pragmata`/`make` commands typed directly, install [direnv](https://direnv.net) and
  add its hook to your shell rc (e.g. `eval "$(direnv hook bash)"`), then run `direnv allow`
  here - the committed `.envrc` auto-loads `.env` on `cd`.
- **Python dependencies** live in `pyproject.toml` + `uv.lock` - committed. `uv sync`
  reproduces the environment exactly, including pragmata itself (git-pinned to an exact
  SHA). The lock carries a `constraint-dependencies` freeze of the versions that produced
  the shipped report numbers; see the comment there before upgrading anything.
- **Operational tunables** live in `configs/settings.conf` (queries-per-spec, bot
  concurrency, throttle, disk thresholds) - committed.
- **querygen runtime** (model, reasoning effort, batching) lives in
  `configs/annotation/querygen_specs/_runtime.yaml`, deep-merged with each per-spec YAML.
- **The domain list** is derived from `configs/annotation/domains/*.yaml` - add a domain by
  adding its config + spec, nothing else to update.

## Annotator roster

`configs/annotation/users.json` is the roster - usernames, roles, and workspace
assignments, **no passwords**. Kept **local (gitignored)** since it carries annotator names.
Passwords live in `configs/annotation/users.secrets.json` (also gitignored). Both have
committed `.example` templates (dummy values) showing the expected shape - copy and fill in.

## Data & secrets

Not version-controlled (gitignored): `.venv/`, `.uv/` (uv's in-tree interpreter and wheel
cache - see the Makefile), `.env`,
`configs/annotation/users.secrets.json`, `configs/annotation/users.json`, `data/`, `logs/`,
`reports/`, `argilla_backup/`, `tmp/`, `*.log`. Everything tracked is scripts, configs,
specs, and the `reproducibility/` bundle.

### What's not committed, and how to obtain it

| Not in git | Why | How to get it |
|---|---|---|
| `data/publikationsbot/*_combined*.jsonl` | large (~52M curated / ~119M full) | fetch the corpus artifact pinned in `reproducibility/.../pins.sha256`, or regenerate via `make pipeline` (querygen is non-deterministic) |
| `data/annotation/exports/` | annotator **PII** (free-text notes; `annotator_id` is pseudonymised on export) | re-export from live Argilla (`make annotation-export`) |
| `argilla_backup/` | large (~250M for the pinned pre-prune snapshot) | that snapshot is an external archive, pinned by `pins.sha256`; the rest are local recovery points, retained per `reproducibility/README.md` |
| `.env`, `users.json`, `users.secrets.json` | secrets / names | copy the committed `.example` templates and fill in |

The curated annotation corpus is reproducible from the `reproducibility/` bundle - see
[Reproducibility](reproducibility.md).
