# Configuration

### Secrets
Secrets live in `.env` (gitignored). Copy `.env.example` and fill in. Required keys: 
- `ARGILLA_API_URL`, `ARGILLA_API_KEY` (annotation import/setup); 
- `OPENAI_API_KEY`, `OPENAI_BASE_URL` (querygen);
- `PUBLIKATIONSBOT_URL` (bot); 
- `PRAGMATA_EVAL_SRC` (path to the eval-pin `pragmata` checkout - the annotation pragmata needs no key, it is git-pinned in `pyproject.toml`); 
- `EVAL_BLOB_ACCOUNT`, `EVAL_BLOB_CONTAINER`, `EVAL_BLOB_SAS` (eval data transport only - see [Data transport](data-transport.md)). 

For Azure, set `OPENAI_API_KEY` to your Azure key and `OPENAI_BASE_URL` to `https://<resource>.openai.azure.com/openai/v1/`.

The scripts under `scripts/` load `.env` themselves (via `scripts/lib/common.sh`), but for ad-hoc `pragmata`/`make` commands typed directly, install [direnv](https://direnv.net) and add its hook to your shell rc (e.g. `eval "$(direnv hook bash)"`), then run `direnv allow` here - the committed `.envrc` auto-loads `.env` on `cd`.

### Python dependencies
Python deps live in `pyproject.toml` + `uv.lock` - committed. `uv sync` reproduces the environment exactly, including pragmata itself (git-pinned to an exact SHA used in the pilot). The lock carries a `constraint-dependencies` freeze of the versions that produced the shipped report numbers; see the comment there before upgrading anything.

### Operational tunables
These live in `configs/settings.conf` (queries-per-spec, bot concurrency, throttle, disk thresholds) - committed.

### The canonical freeze pin
This lives in `configs/eval/freeze.conf` (`FREEZE_DATE` `CANONICAL_SNAPSHOT_RUN_AT`) - committed, same `KEY=VALUE` format and same "existing environment wins" loader. Written by `make annotation-freeze`, read by the eval report scripts; it is what makes a published number cite one export tree and one log snapshot. See [Human annotation scoring](eval-human-annotation.md#the-three-pins).


### querygen runtime (model, reasoning effort, batching) 
These lives in `configs/annotation/querygen_specs/_runtime.yaml`, deep-merged with each per-spec YAML.

### The domain list
Derived from `configs/annotation/domains/*.yaml` - add a domain by adding its config + spec, nothing else to update.

# Domain deployment config

One YAML per programme in `configs/annotation/domains/`, read by `annotation setup` and `annotation import`. 

```yaml
    # Annotation deployment config for a BSt domain.
    dataset_id: ""                    # suffix on the Argilla DATASET names (see below)
    partition_scope: XYZ              # identity of the calibration/production ledger
    locale: de
    calibration_fraction: 0.2         
    calibration_max_records: 30       # absolute cap; wins over the fraction when it binds
    constraint_severity:              # per logical constraint on the labels: warn shows the
      evidence_requires_relevance: warn | block          
      evidence_excludes_misleading: warn | block         
      contradiction_requires_unsupported: warn | block   
      fabricated_requires_cited: warn | block            
    workspaces:                       # one Argilla WORKSPACE per (domain, task)
      {domain}_retrieval:             # the workspace name, used verbatim
        tasks:
          retrieval: {}               # {} = inherit the calibration settings above; per-task overrides go here
      {domain}_grounding:
        tasks:
          grounding: {}
      {domain}_generation:
        tasks:
          generation: {}
```
See the[Implementation Guide](implementation-guide.md#71-define-the-workspaces--dataset-shape) for further guidance.

## Annotator roster

`configs/annotation/users.json` is the roster - usernames, roles, and workspace assignments, no passwords. It is kept local (gitignored) since it carries annotator names. Passwords live in `configs/annotation/users.secrets.json` (also gitignored). Both have committed `.example` templates (dummy values) showing the expected shape - copy and fill in.

## Data & secrets

Not version-controlled (gitignored): `.venv/`, `.uv/`, `.env`, `configs/annotation/users.secrets.json`, `configs/annotation/users.json`, `data/`, `logs/`, `reports/`, `argilla_backup/`, `tmp/`, `*.log`. Everything tracked is scripts, configs, specs, and the `reproducibility/` bundle.

The volumes and data of the VM used to run this pipeline are to be archived within BSt's system.