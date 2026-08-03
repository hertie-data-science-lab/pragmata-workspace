# pRAGmata implementation guide (Bertelsmann Stiftung)

## 1. Purpose

How to run the full pRAGmata pipeline (produce, annotate and evaluate a new RAG system dataset) end to end. This is the handover counterpart to the per-topic docs: it walks the whole pipeline and cross references out for detail. 

Two repositories are used:

- [`pragmata-workspace`](https://github.com/hertie-data-science-lab/pragmata-workspace) - the BSt project-specific configurations and scripts that connect the pipeline stages.
- [`pragmata`](https://github.com/bertelsmannstift/pragmata) - the Python package those scripts call.

Additionally, access to `Publikationsbot` within the Bertelsmann network is required to generate the query-response dataset.

The process is:

    query generation with using structured LLM calls
                  ↓
    queries sent to Publikationsbot
                  ↓
    all queries, responses and publication/chunk data combined
                  ↓
    Argilla setup and unannotated data imported
                  ↓
    human annotation, split by domain/role 
                  ↓
    annotation data exported (pseudonymised)
                  ↓
    human annotation report deliverables scored (CPU VM)
                  +
    synthetic model (evaluator) training + prediction (GPU host)
                  ↓
    full report assembled (separate private repository)

The `pragmata-workspace` repository implements question generation through annotation export, and the transfer of files between the annotation (CPU-backed) and evaluation (GPU-backed) machines, as well as the the scoring of both human and synthetic labels into the report deliverables. NB: the synthetic evaluator fine-tuning and prediction are implemented in `pragmata` (`pragmata eval train-evaluator|predict-labels`, backed by `tlmtc`); assembling the final report happens in a separate repository and is out of scope here.

## 2. How the system is arranged

The documented setup uses instances of the same `pragmata-workspace` repository implemented across two machines:

1. a **CPU annotation VM** in the Bertelsmann Stiftung Azure tenant; and
2. a **GPU evaluation host** in the Hertie network - a shared bare-metal server, not a VM (selected for expediency; there's no constraint against both machines being run within the same network).

The CPU VM runs the dataset-generation and annotation pipeline (Azure OpenAI, Publikationsbot, Argilla), exports and pseudonymises the annotations, scores the human labels into report CSVs, and pushes exports to Azure Blob Storage. The GPU host downloads the exports from the Blob Storage, runs evaluator training (fine-tuning of a Hugging Face model) and prediction, and returns predictions and checkpoints through the same container (from which they are then pulled back into the CPU VM).

The two boxes do not connect directly. Code moves through GitHub (both need to be at the same commit); data moves through Azure Blob Storage over HTTPS. See [Eval data transport](eval-data-transport.md).

## 2.1 Deployment inventory

The `pragmata-workspace` repository is public, so the actual identifier values - tenant and subscription IDs, host, VM and storage-account names, the SAS expiry etc - are kept in a gitignored `docs/deployment-inventory.local.md`. 

| Component | Details |
| --- | --- |
| **CPU annotation VM** | Ubuntu 22.04 LTS, 4 vCPU, Sweden Central, in the Bertelsmann Stiftung tenant • Working directory: `~/pragmata-workspace` (with the eval-pin `pragmata` checkout as a sibling) • Python: 3.12.13, uv-managed, in `pragmata-workspace/.venv` via `make setup` • Access: SSH key auth as `azureuser`, password auth disabled • Tenant, subscription, resource group and VM name: recorded in the local inventory, as is the ingress path that publishes port 22 (the VM's NICs carry no public IP and no Bastion is visible in its subscription). |
| **Azure OpenAI** | Key + base URL in `.env` (`OPENAI_API_KEY`, `OPENAI_BASE_URL`); base-URL format `https://<resource>.openai.azure.com/openai/v1/` • Same tenant, subscription and resource group as the CPU VM; resource and deployment names in the local inventory, the deployment matching the model named in `querygen_specs/_runtime.yaml`.|
| **Publikationsbot** | Service URL in `.env` (`PUBLIKATIONSBOT_URL`); the production endpoint is an Azure Container App • Auth = an Azure AD bearer, fetched with `az account get-access-token --resource https://graph.microsoft.com`. See `scripts/annotation/run_bot.py`. NB the operator needs `az login` in the tenant and an authorised account • Parallelism in use: `N_PARALLEL_BOTS=4` (`configs/settings.conf`) - more than that caused service instability. |
| **Argilla** | URL + API key in `.env` (`ARGILLA_API_URL`, `ARGILLA_API_KEY`) • Co-located on the CPU annotation VM (not separately hosted): Docker Compose (project `annotation`) from the `pragmata` checkout's `deploy/annotation/docker-compose.dev.yml`, serving port 6900, with Postgres, Redis and Elasticsearch beside it • State: named Docker volumes on the VM's OS disk • Backups: `argilla_backup/<UTC-timestamp>/` in the workspace, from `make annotation-backup` • **TODO: what is the intended off-box backup destination -> volumes and backups share one OS disk today (on the VM), so when we lose the VM we will lose the annotation database, need BSt's archive process.** |
| **Azure Blob Storage** | Account/container/SAS in `.env` (`EVAL_BLOB_*`); container is private and IP-allowlisted; SAS is data-plane only, no `az login` • The SAS is container-scoped and HTTPS-only (and expires mid-2027) **TODO get details of subscription and resource group** |
| **GPU evaluation host** | A shared bare-metal GPU server in the Hertie Data Science Lab: Ubuntu 22.04.5 LTS (kernel 6.8), 4 × NVIDIA A100-PCIE-40GB, AMD EPYC 7742 (64C/128T), 503 GB RAM, 3.5 TB NVMe • Driver 535.309.01 (NB: this makes CUDA 12.2 the host ceiling). Evaluation runs in containers with runtime (`nvidia-container-toolkit` 1.19.1) • The checkout is shared between users by POSIX ACL, which is why `make setup` exists (see §3.2) |

## 3. Prepare the repositories and configuration

### 3.1 Recorded code versions

>NB: the following is for *exact* reproducibility of our pilot setup. For generally rerunning the pipeline with the pRAGmata tool to generate/annotate/evaluate new data, just `pip install pragmata` (inlcuded in `uv sync`). **TODO check i think i need to update uv later for this to work as it currently pins the annotation version?**

The pilot's specific pipeline runs two different commits of `pragmata`.

The annotation pin is a git dependency pinned to a SHA in `pragmata-workspace/pyproject.toml`, installed by `uv sync`. This commit built and imported the live Argilla instance.

The eval pin is a git checkout we provide at `pin/eval-report-2026-07` (as two commits of one package cannot be installed into the same environment). We clone `pragmata` a second time, checkout the eval pin, and point `.env` at its `src/`. This gives:

    <working-directory>/
    ├── pragmata-eval/         # a pragmata checkout - PRAGMATA_EVAL_SRC pin (eval stage)
    └── pragmata-workspace/    # annotation pin comes from pyproject.toml, not a checkout

### 3.2 Create the environment

**On the primary CPU-backed VM**: from `pragmata-workspace` run `make setup` (prerequisite: `uv` on PATH), this creates the `.venv/`; python is also uv-managed (the version is fixed by `.python-version` (3.12.13)). The single venv runs both stages ([why](eval.md#the-three-pins)) as it covers every Python entry point in the repository. Outside Python, the scripts expect `/bin/bash`, `make`, `jq` and the Azure CLI onc PATH.

> Use the target rather than a bare `uv sync`: it wraps `uv sync --frozen` and keeps uv's interpreter and wheel cache in `.uv/` in the checkout instead of `~/.local/share/uv` and `~/.cache/uv`. No practical difference on a single-user VM, but on a checkout shared between users by POSIX ACL (the GPU host) it is what makes `.venv` readable by all of them - uv writes those per-user paths mode 700/711. The `Makefile` carries the full reasoning; both paths are overridable from the environment.

> NB: for exact pilot reproducibility, a GitHub SSH key with read access to `bertelsmannstift/pragmata` (the annotation pin is a `git+ssh://` dependency) is also required. 
> 
> Here ends the exact reproducibility divergence. The rest is is the same as if for generally re-running the pipeline.

**The GPU host does not need the venv:** everything it runs from this repository is `scripts/transfer/sync.sh`: pull and verify are `bash` + Azure CLI + `sha256sum`, and push adds only the system `python3` for the pseudonymisation guard. Nothing there touches `.venv`. 

### 3.3 Create the local configuration

From `pragmata-workspace`:

    cp .env.example .env
    cp configs/annotation/users.json.example configs/annotation/users.json
    cp configs/annotation/users.secrets.json.example configs/annotation/users.secrets.json

Complete `.env` with the values from the deployment inventory. The variable definitions and annotator-file formats are in [Configuration](configuration.md); secrets, user files, data, logs, reports and backups are gitignored.

The committed run settings live in:

- [`configs/settings.conf`](../configs/settings.conf) - operational tunables, including the
  querygen counts (`N_BASELINE`, `N_EDGECASE`) and the IAA bootstrap parameters
- [`configs/annotation/querygen_specs/`](../configs/annotation/querygen_specs/) - per-domain
  query instructions, plus `_runtime.yaml` for model/batching/timeout
- [`configs/annotation/domains/`](../configs/annotation/domains/) - the Argilla
  workspace/dataset structure, one YAML per domain

## 4. Confirm the output tree is clean

The pipeline writes to fixed paths, not per-run ones:

    data/querygen/runs/<specification>/
    data/publikationsbot/<specification>.jsonl
    data/publikationsbot/<domain>_combined.jsonl

They are deliberately not per-run, this makes the stages resumable (the Publikationsbot client skips query IDs already present in its output file, and the combine stage absorbs matching `_batch` files from earlier runs). An interrupted run is therefore cheap to continue - but an *earlier* run left in place is silently mixed into the new one.

So before running anything, confirm the tree is clean:

    make reset                   # preview: what run output is still here
    make plan                    # should list the full stage sequence for every domain in scope

`make reset` with no arguments deletes nothing - it reports what the tree still holds. On a
fresh deployment it prints `nothing to delete` and there is nothing to do here. If an earlier
run's output is still in the tree - the case on the pilot CPU VM - archive and clear it first
([§11.3](#113-clear-the-tree-for-the-next-run)), then come back to this check.

## 5. Test the deployment

### 5.1 Check the local installation

    .venv/bin/python --version
    .venv/bin/pragmata --help
    make help
    make plan                    # preview the pipeline without running it

The orchestrator (`scripts/pipeline.sh`) runs the five stages

    querygen-run → bot-run → combine-run → annotation-setup → annotation-import

with stage and domain filters, a lock file, per-stage timing, and continue-on-error with a final summary. The slice tokens accepted by `FROM=`/`TO=`/`ONLY=` are exactly the make target names.

`make pipeline` with no arguments runs all five stages for every domain, end to end. The rest
of this guide deliberately drives it in slices instead - §5 to test each external service in
turn, §6 to generate and *review* the candidate dataset before anything reaches Argilla, §7 to
import - because the review gate in §6.2 sits between `combine-run` and `annotation-setup`.
Always review the return code of every stage.

### 5.2 Test question generation

Run one domain with reduced counts (the committed defaults in `settings.conf` are
production-sized):

    N_BASELINE=5 N_EDGECASE=2 make pipeline TO=querygen-run FILTER=gesundheit

Confirm the query CSVs appear under `data/querygen/runs/`.

### 5.3 Test Publikationsbot

    make bot-probe SPEC=gesundheit

This sends one question and dumps the raw response without writing to the result JSONL. For
a limited multi-question test, call the script directly with `--max-per-spec`.

### 5.4 Test the generation flow

    N_BASELINE=5 N_EDGECASE=2 make pipeline TO=combine-run FILTER=gesundheit JOBS=1

This exercises Azure OpenAI, Publikationsbot and the combine stage without touching
Argilla. Review:

    logs/annotation/pipeline.log
    data/publikationsbot/*.jsonl
    data/publikationsbot/*.errors.jsonl
    data/publikationsbot/*.no_retrieval.jsonl

### 5.5 Test Argilla and Blob Storage

Confirm the configured Argilla URL and API key work before running setup or import. Test
Blob access from both boxes with the smoke test in
[Eval data transport](eval-data-transport.md#one-time-setup-on-each-box).

Do not begin the full run until Azure OpenAI, Publikationsbot, Argilla, and Blob (from both
VMs) all connect.

## 6. Generate and review the candidate dataset

Run query generation, Publikationsbot and combine without importing into Argilla:

    make pipeline TO=combine-run
    make pipeline TO=combine-run FILTER=gesundheit,europas-zukunft   # selected domains
    make pipeline TO=combine-run JOBS=<approved number>              # bot parallelism

The final candidate dataset per domain is `data/publikationsbot/<domain>_combined.jsonl`.

### 6.1 The import contract

`pragmata` defines the import contract as a pydantic model, `QueryResponsePair`
(`src/pragmata/core/schemas/annotation_import.py`). One record is one query-response pair:

| Field | Rule |
| --- | --- |
| `query` | non-empty string |
| `answer` | non-empty string |
| `chunks` | at least one chunk |
| `chunks[].chunk_id`, `chunks[].doc_id` | non-empty strings |
| `chunks[].chunk_rank` | integer ≥ 1 (1-based retrieval position) |
| `chunks[].text` | non-empty string |
| `context_set` | non-empty string |
| `language` | optional; may be null. Any string is accepted - the ISO form (e.g. `"de"`) is a convention, not a validation |

The model forbids unknown fields at **both** levels - the record's five keys and each chunk's
four - which is why `scripts/annotation/import.sh` projects both with `jq` before calling
`pragmata annotation import`: `run_bot.py` writes provenance extras (per record, and `title` /
`score` per chunk) that would otherwise be rejected.

**Two properties the pipeline maintains but the schema does not check**, on the pinned commit
the import actually runs (`94e8219`): `chunk_id` is unique within a record, and `language`
holds an ISO code. `run_bot.py` derives `chunk_id` as `<doc_id>-c1`, one per retrieved
document, so duplicates cannot arise from the current pipeline - but a hand-built or
third-party file with repeated `chunk_id`s imports without complaint and collapses to one
retrieval item. Upstream `pragmata` has since added a uniqueness validator; it is not in this
pin.

**Invalid records are skipped, and the skip happens after the valid ones are already in
Argilla.** The import validates every record, imports the ones that pass, and only then
reports failures: `Validation errors: <n>` on stderr and exit code 1. There is no dry run and
no all-or-nothing mode, so a partial import is the failure mode to expect. Two consequences:

- The acceptance check is `Total records:` in the import output equalling the offered line
  count, with the command exiting 0:

      wc -l data/publikationsbot/<domain>_combined.jsonl      # records offered

- A non-zero exit means some records are in and some are not. Fix the rejected records and
  re-import the whole file: record IDs are content hashes and the import is an idempotent
  upsert, so the already-imported records are re-written in place rather than duplicated.

### 6.2 Review before import

1. confirm that every pipeline stage succeeded;
2. review the error and no-retrieval files;
3. check the record count for each domain;
4. confirm the records satisfy `QueryResponsePair` (§6.1). The import prints no
   confirmation on success - `Validation errors: <n>` appears on stderr *only* when there are
   failures - so the acceptance signal is the pair from §6.1: `Total records:` equal to the
   offered line count, and exit 0;
5. inspect a sample of questions, answers and publication passages; and
6. confirm that the files contain only records from the current run (§4).

**TODO (BSt):** the *editorial* acceptance thresholds, which the schema cannot express: the
target record count per domain, the tolerable share of no-retrieval and error records, and
who signs off on the sampled questions, answers and passages. Nothing in the repositories or
on the VM can supply these; for a descriptive baseline to set them against, use the shipped
run's own figures in `reports/eval/<date>/annotation_operations.csv` and
`retrieval_manifest.csv`.

## 7. Import the candidate dataset into Argilla

### 7.1 Confirm the destination

The workspace and dataset structure comes from `configs/annotation/domains/`. Before
importing, decide whether the new run belongs in new datasets, existing datasets, or a
separate Argilla instance - this prevents new records mixing unintentionally with an
earlier run.

### 7.2 Back up Argilla

    make annotation-backup

Backup and restore are documented in [Annotation pipeline](annotation.md#backup--restore).
Take a new backup before importing into an instance that holds existing records.

### 7.3 Create workspaces and users

    make annotation-setup DOMAIN=gesundheit    # one domain
    make pipeline ONLY=annotation-setup        # all domains

`setup.sh` reads the domain configuration and the local user files, merges the password
file and calls `pragmata annotation setup`; existing users and workspaces are skipped.
Auto-generated passwords print once - capture them into `users.secrets.json`
([Configuration](configuration.md#annotator-roster)).

### 7.4 Import the records

    make annotation-import DOMAIN=gesundheit   # one domain
    make pipeline FROM=annotation-setup        # all domains, after setup

After import, verify in Argilla that the expected workspaces and datasets exist, record
counts match the import output, production and calibration datasets are present, annotators
have the expected access, and questions, answers and passages display correctly.

## 8. Run annotation and export the labels

The annotation rules are defined in the
[pRAGmata annotation protocol](https://github.com/bertelsmannstift/pragmata/blob/main/docs/methodology/annotation-protocol.md).

Monitoring during annotation ([Annotation pipeline](annotation.md#logging--reporting)):

    make annotation-log        # append a snapshot to logs/annotation/log.jsonl
    make annotation-report     # render tables + plots for the latest snapshot
    make annotation-daily      # the nightly cron body: export -> log

### 8.1 When annotation counts as done

Argilla itself holds the threshold, and `pragmata` reads it live. There is no completion
target in `configs/` - `min_submitted` lives in each dataset's Argilla settings
(`dataset.settings.distribution`), because for a status report what the server enforces is the
source of truth. Read it with:

    .venv/bin/pragmata annotation status                    # all tasks, all workspaces
    .venv/bin/pragmata annotation status --by-dataset       # per dataset

Two conditions, both required:

- **`overlap_satisfied`** (operational) - every chunk has at least `min_submitted` submitted
  responses, per its own dataset's setting: typically 1 for production, 3 for calibration.
  This is what makes Krippendorff's α computable, and it is the sense in which Argilla itself
  calls a record "completed".
- **`panel_complete`** (metric-facing, strict) - for retrieval, all K chunks of a query panel
  have at least one **submitted** response. Discards are abstentions, not judgements, so they
  do not count toward completeness. A panel can therefore be `panel_complete` while still
  overlap-unsatisfied (say 1 of 3 calibration votes in), and vice versa.

Grounding and generation are one annotation item per record, so they have no panels - the
overlap condition alone applies. `--tag-partial-panels` stamps `needs_completion` on the
unresolved chunks of partial panels so annotators can filter to them in the UI; it is the only
write this command makes.

### 8.1.1 Panels that only ever received discards

The rule above cannot settle one case: a panel that can never reach `panel_complete` because
a chunk holds only discarded responses. Two facts narrow it to a confirmation rather than a
design decision.

**It has not yet happened.** Across the frozen export behind the shipped numbers (8
programmes) there is **one** discarded retrieval row in 1925, and **no** chunk record whose
only responses are discards.

**The pipeline already has a default: accepted as incomplete, kept in the denominator.**
`panel_totals()` in `scripts/eval/eval_common.py` reads `n_panels` from the export's own
`completeness_summary` rather than counting CSV rows, precisely so that panels nobody
completed stay in the denominator - the shipped figures are 181 complete panels of 551.
Nothing excludes a panel for want of a submitted response, and nothing re-issues it.

**TODO (BSt):** ratify that default, or specify re-issue or exclusion instead - which would
change the published completeness figures and must therefore be decided before a freeze.

### 8.2 Export the annotations

    make annotation-export                      # all domains
    make annotation-export DOMAIN=gesundheit    # one domain

The export writes per-task CSVs under `data/annotation/exports/`, one directory per domain,
and **pseudonymises annotator identities as it runs** - `annotator_id` and the IAA pairwise
keys hold Argilla user ids (UUIDs), never usernames
([Eval pipeline](eval.md#annotator-identities)). Exports use the domain as the export ID,
so a later export replaces that domain's snapshot; a dataset behind report numbers is
frozen to `data/annotation/exports-frozen/<date>/` instead
([Eval pipeline](eval.md#cutting-a-new-freeze)). Exports include discarded rows; consumers
requiring completed annotations filter `response_status == "submitted"`.

## 9. Transfer the export to the GPU host

On the CPU VM:

    make transfer-push SRC=data/annotation/exports PREFIX=exports

On the GPU host:

    make transfer-pull PREFIX=exports       # -> data/transfer/exports/, verified
    make transfer-verify PREFIX=exports     # re-check any time

Every push writes a SHA-256 manifest and prints a snapshot pin; every pull re-verifies on
the receiving end. The push refuses a tree carrying non-pseudonymised identities. Full
detail: [Eval data transport](eval-data-transport.md).

## 10. Run the evaluation

**Scoring human labels has shipped and runs on the CPU VM.** Three targets produce the
report deliverables into `reports/eval/<date>/`, each CSV with a `.provenance.json` and the
data dictionary beside it:

    make eval-report     # annotation_operations, annotation_label_summary, retrieval_manifest
    make eval-score      # eval_metric_estimates.csv, via `pragmata eval score`
    make eval-catalog    # corpus_catalog.csv (needs an active `az login`)

They read pinned inputs, never the live export tree; the pin model, the vocabulary and the
refresh procedure are in [Eval pipeline](eval.md), and every column of every CSV is defined
in the [data dictionary](eval-data-dictionary.md).

**Evaluator training and prediction are implemented in `pragmata` and run on the GPU host.**
`pragmata eval train-evaluator` fine-tunes a supervised evaluator through `tlmtc` (default
proxy `jhu-clsp/mmBERT-small`, target `jhu-clsp/mmBERT-base`) and writes a run directory, a
model directory and a run-metadata file; `pragmata eval predict-labels` applies a chosen
training run to an unlabelled CSV and writes probabilities and predictions. Both live behind
the `eval` extra (`pragmata[eval]` → `tlmtc[train]`), which is *not* in this workspace's lock
- the GPU box installs its own environment. They consume staged input by explicit path, e.g.:

    pragmata eval train-evaluator \
      --labeled-data-path data/transfer/exports/<domain>/<task>.csv \
      --task <retrieval|grounding|generation> \
      --config <evaluation-config>

    pragmata eval predict-labels \
      --unlabeled-data-path <unlabelled-csv> \
      --evaluator-run-id <run-id-from-training> \
      --task <retrieval|grounding|generation>

**The container is known; the environment inside it is not.** The precedent runs were made
from a lab container image built on 2026-07-10 by a project member off the platform's PyTorch
base - Python 3.10.12, `torch` 2.8.0+cu126, `transformers` 5.13.0, `peft` 0.19.1 - and those
dates bracket the 12 `checkpoints/` runs of 07-13 to 07-22. But **neither `pragmata` nor
`tlmtc` is baked into that image**, so both were installed into the running container and no
record of how survives in the image; the `tlmtc` 0.4.0/0.4.2 in the run metadata is the only
trace of the result. Note also that the image's CUDA 12.6 runtime exceeds the host driver's
12.2 ceiling and works only through the container `cuda-compat` layer (§2.1) - a hand-built
environment that installs a `torch` wheel newer than cu126 will fail to see the GPUs, exactly
as the host `.venv` does.

**What is otherwise missing is the project-side procedure, not the code.**

**Precedent exists, but it is not the procedure.** The Blob container's `checkpoints/`
prefix holds 12 completed training runs from 2026-07-13 to 07-22, each with a
`train_run_meta.json`. They pin down the shape a configuration has to reproduce: `tlmtc`
0.4.0 and 0.4.2, `sequence_length` 1024, `input_mode: paired_text`, transfer learning with
PEFT adapters, per-label threshold 0.5, no HPO, and the per-task label sets (3 for
retrieval; 5 for generation - `proper_action`, `response_on_topic`, `helpful`, `incomplete`,
`unsafe_content`). All 12 used `answerdotai/ModernBERT-base` as both proxy and target,
overriding the pin's defaults (`jhu-clsp/mmBERT-base` / `mmBERT-small`), so a rerun that
takes the defaults will not reproduce them.

Equally, what those runs are not: there is **no grounding run at all**; every run naming an
export names the single programme `demokratie-und-zusammenhalt` and three record no export
id, so their input cannot be traced; all 12 predate the pseudonymised freeze the shipped
report rests on; none records a pragmata commit, an environment or a command; and the
container has **no `predictions/` prefix**, so predictions have never been returned and
[§11.1](#111-return-the-evaluation-outputs)'s round trip is untested end to end.

**TODO (joint):** the tested GPU-side process - the pragmata commit used on the GPU host, the
environment installation command, the evaluation configuration files, the mapping from each
export to the three task inputs, the per-task training commands, the prediction and scoring
commands, expected output directories, and the conditions for accepting an evaluation
result. `scripts/eval/score_synthetic_predictions.py` is the reserved name for scoring the
evaluator's predictions once they exist. Until these values are filled in and run against a
real export, this step is not reproducible from the documentation alone even though every
command it needs exists.

The final report is assembled from these outputs in a separate private repository, outside
the scope of this guide.

## 11. Return and archive

### 11.1 Return the evaluation outputs

On the GPU host, upload the evaluation outputs:

    make transfer-push SRC=<prediction-tree> PREFIX=predictions
    make transfer-push SRC=<checkpoint-tree> PREFIX=checkpoints

On the CPU VM:

    make transfer-pull PREFIX=predictions
    make transfer-pull PREFIX=checkpoints

**Checkpoints must be pulled off before the container goes away** - everything else is
reproducible from pinned inputs and code; checkpoints are not. On the GPU host this is a
live deadline rather than a decommissioning event: the container auto-stops after 1 h idle
or 48 h of runtime and is removed 30 min after stopping (§2.1), so anything written inside
the container rather than to the mounted workspace is lost on that timer. Write checkpoints
to the mounted workspace and push them the same session.

### 11.2 Archive the completed run

The workspace excludes data, secrets, logs, reports and backups from git; a completed run
is archived outside the working repositories. Archive: the exact git commits, non-secret
configuration, generated-question CSVs, Publikationsbot results/errors/no-retrieval files,
combined candidate datasets, the Argilla backup, the (pseudonymised) annotation exports,
Blob manifests and snapshot pins, evaluation configurations, training logs, predictions,
scores, and checkpoints. The dated bundles under
[`reproducibility/`](../reproducibility/README.md) are the committed half of this record -
each pins its artefacts by SHA-256 and `make repro-verify` re-checks them.

What the Blob container already archives, and under what convention: `exports/`,
`exports-frozen/<date>/`, `checkpoints/<run-id>/` (a 32-hex training run id), `reports/` and
`analysis/iaa-summary/`. The committed pins live in `reproducibility/<date>-<name>/`.

**TODO (BSt):** the permanent storage location and run-directory naming convention for
everything the container does *not* hold - the dataset-run inputs (querygen and
Publikationsbot trees), the Argilla backup, and the evaluation configurations. The
`argilla_backup/` directories on the VM are local recovery points, not archives. This is the
one location §11.3 below copies each finished run into, so record it once, here.

### 11.3 Clear the tree for the next run

This is the other half of the cycle: the fixed output paths of §4 mean a finished run has to
leave the tree before the next one starts. Archive first, then clear - and do not delete an
earlier run until its data, configuration, logs and checksums are archived and the archive has
been verified.

A per-run output directory (a `RUN_ID` in the paths) would be tidier, but that means changing
the fixed paths in code; archive-and-clear is a procedure only, so it is the one documented
here.

1. **Pin the outgoing run** while its files are still in place, so the archive is verifiable:

       make repro-pin KIND=freeze NAME=dataset-run PATHS="data/querygen data/publikationsbot"

   This writes `reproducibility/<today>-dataset-run/` - a `pins.sha256` listing every artefact by SHA-256, plus a README stub to complete. `KIND=freeze` marks it a self-contained record of one run rather than a lineage step that gets replayed (the default is `lineage`). The bundle is committed; the data it pins is not. See [`reproducibility/`](../reproducibility/README.md).

2. **Copy the run out** to the archive location (§11.2), preserving the repo-relative paths, and keep together the four things a rerun needs:
    1. `data/querygen/runs/`
    2. `data/publikationsbot/` (incl the `.errors.jsonl` and `.no_retrieval.jsonl` files)
    3. `logs/annotation/`, and
    4. the non-secret configuration (`configs/`) plus the two git commits from §3.1.

3. **Verify the copy** before touching the originals. `pins.sha256` is plain `sha256sum` format over repo-relative paths, so from the archive root:

       sha256sum -c <workspace>/reproducibility/<today>-dataset-run/pins.sha256

   Every line must read `OK`. (`make repro-verify PIN=<bundle>` checks the *working tree*, so it confirms the originals, not the copy - useful as the before/after pair.)

4. **Decide on the planning carry-over**, because step 5 needs the answer.
   `data/querygen/<spec_fingerprint>.json` is not run output - it is the previous run's planning
   summary for that spec (its redundancy patterns and diversification targets), read back and
   fed into the next run's planning prompt. Left in place, the new run deliberately steers
   *away* from the questions the old one asked; removed, it plans from the spec alone. Either
   way the files are archived with the run, since they sit inside the pinned `data/querygen`
   tree from step 1. Pass `CARRYOVER=drop` in step 5 to delete them.

5. **Clear only then:**

       make reset PIN=<bundle> APPLY=1                    # keep the planning carry-over
       make reset PIN=<bundle> APPLY=1 CARRYOVER=drop     # drop it too

   `make reset` deletes `data/querygen/runs/` and every file in `data/publikationsbot/` except
   `.gitkeep`. It never touches `data/annotation/` - exports, and the frozen inputs behind
   published numbers ([Eval pipeline](eval.md#cutting-a-new-freeze)) - nor `logs/`,
   `argilla_backup/` or `reports/`.

   **Two guards stand between the command and data loss, and both must pass.** `PIN=` must
   name a bundle that (a) lists *every* file about to be deleted, and (b) verifies clean
   against the working tree. (a) matters because a pin can be internally consistent and still
   say nothing about half the tree: pin only `data/querygen` and every Publikationsbot file is
   unrecorded yet deletable. (b) matters because a `MISMATCH` means the pin is not a faithful
   record of what is there, so the difference would be lost. Without `APPLY=1` the command only
   previews, so `make reset` is always safe to run just to see what is still in the tree.

   A pin is not an archive - it records checksums inside the repository and copies nothing.
   Steps 2 and 3 are still yours to do.

Then re-run the §4 check to confirm the tree is clean.

## 12. When the rerun is complete

The rerun is complete when:

1. all resources in the deployment inventory have been identified;
2. both boxes and all external services are accessible;
3. the exact code versions and environment installation commands are recorded;
4. the previous run is archived and verified, and the output directories are clean (§11.3, §4);
5. the small generation test succeeds;
6. the candidate dataset imports with zero validation errors and is editorially approved
   (§6);
7. the records are imported into the intended Argilla destination;
8. annotation is complete by the §8.1 rule - overlap satisfied and panels complete - and
   exported (pseudonymised);
9. the export is transferred and verified on the GPU host;
10. evaluator training, prediction and scoring are completed;
11. predictions and checkpoints are returned; and
12. the full run is archived and its bundle pinned.

The remaining documentation gaps, all marked **TODO** above. The Azure resources are now
identified and recorded in the uncommitted `docs/deployment-inventory.local.md`; what is
still outstanding is:

- how the CPU VM is reached and re-provisioned, and where the Azure OpenAI key is held
  (§2.1);
- the Publikationsbot production hosting resource, its access-grant process and its approved
  request rate (§2.1);
- an off-box Argilla backup destination (§2.1);
- the Blob subscription and resource group, SAS renewal ownership before the mid-2027
  expiry, and the IP-allowlist process (§2.1);
- how the GPU-side container is built and `pragmata[eval]`/`tlmtc` installed inside it - the
  host itself is now documented (§2.1, §10);
- the GitHub account or deploy key a rerun clones with (§3.2);
- the archive location and run-naming convention for the dataset-run inputs, the Argilla
  backup and the evaluation configurations (§11.2);
- the editorial acceptance thresholds for a candidate dataset (§6);
- ratification of the default treatment of panels that only ever received discards
  (§8.1.1); and
- the tested GPU-side evaluation process, including a first returned `predictions/` tree
  (§10).
