# pRAGmata implementation guide

## 1. Purpose

How to produce, annotate and evaluate a new **pRAGmata dataset**, end to end. This is the
handover counterpart to the per-topic docs: it walks the whole path once and links out for
detail rather than duplicating it. Fields marked **TODO (BSt)** need values that exist only
on the operator side and cannot be derived from the repositories.

Two repositories are used:

- [`pragmata-workspace`](https://github.com/hertie-data-science-lab/pragmata-workspace) -
  the project-specific configurations and scripts that connect the pipeline stages.
- [`pragmata`](https://github.com/bertelsmannstift/pragmata) - the Python package and
  command-line functions those scripts call.

The process is:

    query generation with Azure OpenAI
                  ↓
    questions sent to Publikationsbot
                  ↓
    answers and publication passages combined
                  ↓
    Argilla setup and import
                  ↓
    human annotation
                  ↓
    annotation export (pseudonymised)
                  ↓
    report deliverables scored on the CPU VM
                  ↓
    (planned) evaluator training on the GPU VM

The workspace implements question generation through annotation export, the transfer of
files between the annotation and evaluation machines, and the scoring of human labels into
the report deliverables. Evaluator **training and prediction** (`pragmata eval
train|predict`) are package interfaces without a tested project-specific process yet - see
[section 10](#10-run-the-evaluation).

## 2. How the system is arranged

The documented setup uses two virtual machines:

1. a **CPU annotation VM** in the Bertelsmann Stiftung Azure tenant; and
2. a **GPU evaluation VM** in the Hertie network.

The CPU VM runs the dataset-generation and annotation pipeline (Azure OpenAI,
Publikationsbot, Argilla), exports and pseudonymises the annotations, scores the human
labels into the report CSVs, and pushes exports to Azure Blob Storage. The GPU VM downloads
the exports from Blob, will run evaluator training, and returns predictions and checkpoints
through the same container.

The two VMs cannot connect directly. Code moves through GitHub (both boxes at the same
commit); data moves through Azure Blob Storage over HTTPS. See
[Eval data transport](eval-data-transport.md).

## 2.1 Deployment inventory

Complete this table with the resources used by the project. These details are required for
a rerun but are not available in the repositories. Do not duplicate the values later in the
guide; update this inventory when a resource or endpoint changes.

| Component | Details |
| --- | --- |
| **CPU annotation VM** | OS: Ubuntu 22.04 LTS • Working directory: `~/pragmata-workspace` (with the eval-pin `pragmata` checkout as a sibling) • Python: 3.12.13, uv-managed, in `pragmata-workspace/.venv` via `uv sync` • **TODO (BSt)**: Azure tenant, subscription, resource group, VM name, access method |
| **Azure OpenAI** | Key + base URL in `.env` (`OPENAI_API_KEY`, `OPENAI_BASE_URL`); base-URL format `https://<resource>.openai.azure.com/openai/v1/` • **TODO (BSt)**: tenant, subscription, resource group, resource name, model deployment name, key-secret location |
| **Publikationsbot** | Service URL in `.env` (`PUBLIKATIONSBOT_URL`); auth = Azure access token via `az` CLI • **TODO (BSt)**: hosting resource, access process, approved number of parallel requests |
| **Argilla** | URL + API key in `.env` (`ARGILLA_API_URL`, `ARGILLA_API_KEY`) • **TODO (BSt)**: tenant, subscription, resource group, host, persistent-storage location, backup location |
| **Azure Blob Storage** | Account/container/SAS in `.env` (`EVAL_BLOB_*`); container is private and IP-allowlisted; SAS is data-plane only, no `az login` • **TODO (BSt)**: subscription, resource group, SAS expiry and renewal process, IP-allowlist process |
| **GPU evaluation VM** | **TODO (Hertie)**: host, hosting environment, access method, OS, GPU and CUDA version, working directory |

## 3. Prepare the repositories and configuration

### 3.1 Record the code versions

The pipeline runs **two different commits of `pragmata`**, and they are obtained in two
different ways.

**The annotation pin needs nothing from you.** It is a git dependency pinned to an exact
SHA in `pragmata-workspace/pyproject.toml`, installed by `uv sync` in §3.2. That commit is
the one that built and imported the live Argilla instance, so annotation and export
behaviour stays frozen. To see it:

    grep 'pragmata.*git+' pyproject.toml

**The eval pin is a checkout you provide**, because two commits of one package cannot be
installed into the same environment. Clone `pragmata` a second time, check out the eval
pin, and point `.env` at its `src/`:

    git clone https://github.com/hertie-data-science-lab/pragmata-workspace.git
    git clone https://github.com/bertelsmannstift/pragmata.git pragmata-eval
    git -C pragmata-eval checkout pin/eval-report-2026-07

    # in pragmata-workspace/.env
    PRAGMATA_EVAL_SRC=<working-directory>/pragmata-eval/src

giving:

    <working-directory>/
    ├── pragmata-eval/         # a pragmata checkout - PRAGMATA_EVAL_SRC pin (eval stage)
    └── pragmata-workspace/    # annotation pin comes from pyproject.toml, not a checkout

Record the exact commits - not branch names:

    git -C pragmata-eval rev-parse HEAD
    git -C pragmata-workspace rev-parse HEAD

The pins behind the shipped deliverables are recorded in the reproducibility bundles (see
[`reproducibility/`](../reproducibility/README.md)); the eval one is named in
[`reproducibility/2026-07-30-eval-report/`](../reproducibility/2026-07-30-eval-report/).
The split is deliberate - see [Eval pipeline](eval.md#the-three-pins).

### 3.2 Create the Python environment

One command, from `pragmata-workspace`:

    uv sync

This creates `.venv/` from the committed `pyproject.toml` and `uv.lock`, installing all
126 packages at exactly the versions the shipped report numbers were produced with -
including `pragmata` itself, from git at its pinned SHA. Python is uv-managed: the version
is fixed by `.python-version` (3.12.13) and does not have to be installed beforehand.

Two prerequisites, both worth checking before the first run:

- [uv](https://docs.astral.sh/uv/getting-started/installation/) on the PATH.
- **A GitHub SSH key with read access to `bertelsmannstift/pragmata`.** The annotation pin
  is a `git+ssh://` dependency, so a machine without a configured key cannot `uv sync` at
  all - it fails rather than falling back. Verify with `ssh -T git@github.com`, which
  should greet you by username. **TODO (BSt):** which key/account the operator uses, and
  how it is provisioned on a new VM.

Do not `pip install` into this environment. The lock is what makes a re-run reproducible -
`numpy` and `scipy` sit under the inter-annotator-agreement bootstrap, so an unplanned
upgrade can move published numbers. To change a dependency deliberately: edit
`pyproject.toml`, run `uv lock`, regenerate the deliverables into a scratch `OUT=`, and
diff them against the shipped CSVs before accepting the change.

The single venv runs both stages ([why](eval.md#the-three-pins)); `pandera` is in it for
the eval side. Outside Python, the scripts expect `/bin/bash`, `make`, `jq` and the Azure
CLI on the PATH. Two eval scripts (`corpus_catalog.py`, `vectorstore_inventory.py`) run in
their own uv-managed environments, declared inline in the files rather than in `.venv`.

### 3.3 Create the local configuration

From `pragmata-workspace`:

    cp .env.example .env
    cp configs/annotation/users.json.example configs/annotation/users.json
    cp configs/annotation/users.secrets.json.example configs/annotation/users.secrets.json

Complete `.env` with the values from the deployment inventory. The variable definitions and
annotator-file formats are in [Configuration](configuration.md); secrets, user files, data,
logs, reports and backups are intentionally excluded from git.

The committed run settings live in:

- [`configs/settings.conf`](../configs/settings.conf) - operational tunables, including the
  querygen counts (`N_BASELINE`, `N_EDGECASE`) and the IAA bootstrap parameters
- [`configs/annotation/querygen_specs/`](../configs/annotation/querygen_specs/) - per-domain
  query instructions, plus `_runtime.yaml` for model/batching/timeout
- [`configs/annotation/domains/`](../configs/annotation/domains/) - the Argilla
  workspace/dataset structure, one YAML per domain

Review these files before a run rather than copying their values here.

## 4. Start a clean dataset run

The pipeline uses fixed output paths:

    data/querygen/runs/<specification>/
    data/publikationsbot/<specification>.jsonl
    data/publikationsbot/<domain>_combined.jsonl

The Publikationsbot client resumes existing output files by skipping query IDs already
written, and the combine stage includes matching `_batch` files from earlier runs - so
existing output can be reused or mixed into a new dataset. The repository does not provide
a reset command.

**TODO (BSt):** document and use one approved clean-run procedure (new workspace directory
per run, or archive-and-clear selected output directories), the archive location for the
previous run, and the naming convention for the new one. Do not delete an earlier run until
its data, configuration, logs and checksums have been archived.

## 5. Test the deployment

### 5.1 Check the local installation

    .venv/bin/python --version
    .venv/bin/pragmata --help
    make help
    make plan                    # preview the pipeline without running it

The orchestrator (`scripts/pipeline.sh`) runs the five stages

    querygen-run → bot-run → combine-run → annotation-setup → annotation-import

with stage and domain filters, a lock file, per-stage timing, and continue-on-error with a
final summary. The slice tokens accepted by `FROM=`/`TO=`/`ONLY=` are exactly the make
target names. Always review the return code of every stage.

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
Blob access from both VMs with the smoke test in
[Eval data transport](eval-data-transport.md#one-time-setup-on-each-box).

Do not begin the full run until Azure OpenAI, Publikationsbot, Argilla, and Blob (from both
VMs) all connect.

## 6. Generate and review the candidate dataset

Run query generation, Publikationsbot and combine without importing into Argilla:

    make pipeline TO=combine-run
    make pipeline TO=combine-run FILTER=gesundheit,europas-zukunft   # selected domains
    make pipeline TO=combine-run JOBS=<approved number>              # bot parallelism

The final candidate dataset per domain is `data/publikationsbot/<domain>_combined.jsonl`.

Before import:

1. confirm that every pipeline stage succeeded;
2. review the error and no-retrieval files;
3. check the record count for each domain;
4. confirm that each record contains `query`, `answer`, `chunks`, `context_set` and
   `language`;
5. inspect a sample of questions, answers and publication passages; and
6. confirm that the files contain only records from the current run.

**TODO (BSt):** the minimum conditions for approving a candidate dataset for import.

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

**TODO (BSt):** when the annotations count as ready for evaluation (required completion
level; treatment of calibration, discarded and disputed records).

Export the annotations:

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

## 9. Transfer the export to the GPU VM

On the CPU VM:

    make transfer-push SRC=data/annotation/exports PREFIX=exports

On the GPU VM:

    make transfer-pull PREFIX=exports       # -> data/transfer/exports/, verified
    make transfer-verify PREFIX=exports     # re-check any time

Every push writes a SHA-256 manifest and prints a snapshot pin; every pull re-verifies on
the receiving end. The push refuses a tree carrying non-pseudonymised identities. Full
detail: [Eval data transport](eval-data-transport.md).

## 10. Run the evaluation

**Scoring human labels has shipped and runs on the CPU VM.** Three targets produce the
report deliverables into `reports/eval/<date>/`, each CSV with a provenance sidecar and the
data dictionary beside it:

    make eval-report     # annotation_operations, annotation_label_summary, retrieval_manifest
    make eval-score      # eval_metric_estimates.csv, via `pragmata eval score`
    make eval-catalog    # corpus_catalog.csv (needs an active `az login`)

They read pinned inputs, never the live export tree; the pin model, the vocabulary and the
refresh procedure are in [Eval pipeline](eval.md), and every column of every CSV is defined
in the [data dictionary](eval-data-dictionary.md).

**Evaluator training and prediction are not built yet.** The `pragmata` package defines the
command shapes (`pragmata eval train-evaluator|predict-labels|score`), and the GPU box
consumes staged input by explicit path, e.g.:

    pragmata eval train-evaluator \
      --labeled-data-path data/transfer/exports/<domain>/<task>.csv \
      --task <retrieval|grounding|generation> \
      --config <evaluation-config>

**TODO (joint):** the tested GPU-side process - the pragmata commit used on the GPU VM, the
environment installation command, the evaluation configuration files, the mapping from each
export to the three task inputs, the per-task training commands, the prediction and scoring
commands, expected output directories, and the conditions for accepting an evaluation
result. `scripts/eval/score_synthetic_predictions.py` is the reserved name for scoring the
evaluator's predictions once they exist. The full process is not reproducible until these
values have been completed and tested against a real export.

## 11. Return and archive the results

On the GPU VM, upload the evaluation outputs:

    make transfer-push SRC=<prediction-tree> PREFIX=predictions
    make transfer-push SRC=<checkpoint-tree> PREFIX=checkpoints

On the CPU VM:

    make transfer-pull PREFIX=predictions
    make transfer-pull PREFIX=checkpoints

**Checkpoints must be pulled off before the GPU VM is torn down** - everything else is
reproducible from pinned inputs and code; checkpoints are not.

The workspace excludes data, secrets, logs, reports and backups from git; a completed run
is archived outside the working repositories. Archive: the exact git commits, non-secret
configuration, generated-question CSVs, Publikationsbot results/errors/no-retrieval files,
combined candidate datasets, the Argilla backup, the (pseudonymised) annotation exports,
Blob manifests and snapshot pins, evaluation configurations, training logs, predictions,
scores, and checkpoints. The dated bundles under
[`reproducibility/`](../reproducibility/README.md) are the committed half of this record -
each pins its artefacts by SHA-256 and `make repro-verify` re-checks them.

**TODO (BSt):** the permanent storage location and the run-directory naming convention.

## 12. When the rerun is complete

The rerun is complete when:

1. all resources in the deployment inventory have been identified;
2. both VMs and all external services are accessible;
3. the exact code versions and environment installation commands are recorded;
4. the run starts with clean local output directories;
5. the small generation test succeeds;
6. the candidate dataset is generated and approved;
7. the records are imported into the intended Argilla destination;
8. annotation is completed and exported (pseudonymised);
9. the export is transferred and verified on the GPU VM;
10. evaluator training, prediction and scoring are completed;
11. predictions and checkpoints are returned; and
12. the full run is archived and its bundle pinned.

The remaining documentation gaps, all marked **TODO** above: CPU and GPU VM provisioning,
the Azure resource names, the GitHub SSH key the repositories are cloned with, shared
Argilla deployment and operation, the clean-run procedure, the candidate-dataset and
annotation acceptance rules, the GPU-side evaluation process, and the permanent archive
location.
