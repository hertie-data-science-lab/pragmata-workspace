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

## Domain deployment config

One YAML per programme in `configs/annotation/domains/`, read by `annotation setup` and
`annotation import`. Annotated, using the Bildung programme:

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

### The three levels

Argilla nests **workspace → dataset → record**, and the config sets the first two:

    Bildung-und-Next-Generation_retrieval     workspace   (from the `workspaces:` key)
      ├── retrieval_production                dataset     (task + purpose + dataset_id)
      └── retrieval_calibration               dataset

One workspace per task is what lets annotators be granted one task without seeing the others;
the production/calibration split within it is what makes Krippendorff's α computable, since
calibration is the overlapped subset. Both dataset names are unconditional - a task always has
both - so an empty `calibration_fraction` gives you an empty calibration dataset, not none.

**The annotation item is not the record, and differs by task.** Grounding and generation have
one item per `record_uuid`; **retrieval has one item per `(record_uuid, chunk_id)`**. That is
the whole reason panels exist for retrieval and not for the other two, and why
`panel_complete` is a retrieval-only condition (implementation guide §8.1).

### partition_scope, and the `dataset_id` name collision

`partition_scope` names the ledger that records which items went to calibration and which to
production. It does two things:

- picks the manifest directory: `data/annotation/imports/<scope>/partition.meta.json`, with
  an empty scope mapping to `default/`;
- is written **inside** that manifest, and `load_partition_manifest` **refuses to load** a
  manifest whose stored scope differs from the caller's. That guard is why each programme has
  its own three-letter scope (`BIL`, `DEM`, `DIG`, `EUR`, `GES`, `NSM`, `ZFD`, `ZFK`) - it
  makes cross-wiring two programmes' calibration budgets a hard error rather than a silent
  mixing.

**Careful when reading `partition.meta.json`: its scope is stored under the key `dataset_id`.**
The schema declares `partition_scope: SafePathSegment = Field(alias="dataset_id")` for on-disk
backwards compatibility, so a manifest reads `"dataset_id": "BIL"` - that is the partition
scope, *not* the config's `dataset_id` (which is `""` in every shipped domain).

### Relaxed constraints

A logical constraint is a binary implication on label values - answer one question a given way
and another is constrained. `warn` shows the annotator a warning; `block` prevents submitting.
The same definitions drive the annotator-time UI widget *and* export-time validation, off one
declaration, so the two cannot drift.

Worth flagging because it is a deliberate deviation from the package default: **all eight
shipped domains set every rule to `warn`**, while `pragmata` defaults three of the four to
`block`:

| Rule | pragmata default | Shipped domains |
| --- | --- | --- |
| `evidence_requires_relevance` | `block` | `warn` |
| `evidence_excludes_misleading` | `warn` | `warn` |
| `contradiction_requires_unsupported` | `block` | `warn` |
| `fabricated_requires_cited` | `block` | `warn` |

So the pilot's annotators could submit logically inconsistent label combinations, and those
combinations reach the export as violation strings rather than being prevented at source. That
is a defensible choice - blocking mid-annotation is disruptive and the violations stay
auditable - but a rerun should decide deliberately rather than inherit it, and any analysis of
label consistency should expect violations to be present.

### Why the ledger exists

Assignments are **locked**: an item already in the manifest keeps its side on every later
import, so growing or re-running a batch never reshuffles records between the two datasets and
never invalidates agreement already collected. New items are bucketed by
`hash(seed || task || unit_id)` against `fraction * 2^32`; when a cap binds, the lowest digests
win calibration and the rest demote to production.

One consequence, documented in `assign_partitions` and worth knowing before you touch a cap:
because the lock is never broken, under a binding cap the final calibration set is a function
of `(corpus, seed, import order)`, not `(corpus, seed)` alone. Tightening
`calibration_max_records` on a later import cannot demote an item that is already in
calibration.

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
