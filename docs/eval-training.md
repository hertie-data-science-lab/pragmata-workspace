# Eval training

Training the synthetic evaluators - the third part of the [eval pipeline](eval.md), beside
data transport and scoring human labels. One evaluator per task is fine-tuned on the pooled
human annotations, then applied to unlabelled data by `pragmata eval predict-labels`.

`scripts/eval/train_evaluators.py` drives it; the recommended configuration per task lives in
[`configs/eval/training/`](../configs/eval/training/) as a shared `_common.yaml` deep-merged
with one file per task, exactly as `querygen_specs/_runtime.yaml` composes with each spec. The
keys are pragmata's own `EvalTrainSettings` fields, so pragmata validates them directly. The
training itself runs on the GPU host: its dependencies are deliberately outside this
workspace's lock, see [The environment](#the-environment).

| Target | Does | Where it runs |
|---|---|---|
| `make eval-train-inputs` | pools the frozen export into `data/eval-inputs/training/<task>.csv` | either box, CPU-only |
| `make eval-train-seqlen` | reports how much of each task's input the default sequence length truncates | either box, CPU-only |
| `make eval-train TASK=<task>` | trains one evaluator into `data/eval/train_outputs/<run_id>/` | GPU host |

**Vocabulary** is the [data dictionary](eval-data-dictionary.md)'s, as everywhere else in
eval. Note that prose says *response* where the export column is named `answer`; the script
reads the column, the doc says the word.

## The environment

Two things are pinned separately, for different reasons.

**The pragmata source** is the eval pin already described in [Eval pipeline](eval.md) - the
third of the three pins. `PRAGMATA_EVAL_SRC` in `.env` points at a checkout, and the script
puts it first on `sys.path` so it shadows the installed annotation pragmata, which is a
frozen demo commit with no eval module at all. Nothing new is needed here: the scoring stage
already resolves the same pin the same way.

**The training extra is not in `uv.lock`, and the workspace venv cannot train.** This is not a
policy choice to work around - it is a hard incompatibility. The lock resolves
`torch 2.12.0+cu130`, which needs CUDA 13, while the GPU host's driver (535.309.01) caps at
CUDA 12.2. On `ds01` the workspace venv therefore reports:

```
torch: 2.12.0+cu130    built for CUDA: 13.0
cuda.is_available(): False    device count: 0
```

...even though `nvidia-smi` shows four A100s. Nor can the lock simply move: it freezes the
exact environment behind the published human-label numbers (see the `constraint-dependencies`
comment in `pyproject.toml`), so resolving a training stack into it would move packages the
alpha bootstrap runs on.

**So training runs in a container, not on the host** - and on `ds01` that is enforced, not
merely advised. The login environment sets `CUDA_VISIBLE_DEVICES=` (empty) and preloads
`libds01_gpu_notice.so`, whose message is *"Host GPU compute is disabled on this server. GPU
workloads must run inside containers."* Do not override it: the blanking is what routes GPU
work through the allocator that keeps users off each other's cards.

Create the container with the **workspace** mounted. Note `project-launch` will not do this -
it mounts `~/workspace/<project>`, and `~/workspace/pragmata` is a checkout of the *package*,
not this repo. Pass the path explicitly instead:

```bash
dashboard gpu          # check what is free first; the box is shared
container-create pragmata-eval-train pytorch --num-gpus=1 -w /home/shared/pragmata-workspace
container-start pragmata-eval-train
container-attach pragmata-eval-train      # or: docker exec -it <container> bash
```

The allocator assigns a free GPU and reports which. Inside, the checkout appears at
`/workspace`; nothing needs adjusting for that, because every path resolves from the script's
own location via `scripts/lib/workspace.py`, so the `make` targets behave identically there and
on the host.

Then build the training environment. **Run the installs from outside `/workspace`**: uv
otherwise picks up this repo's `pyproject.toml` and its `constraint-dependencies` pin
`torch==2.12.0`, and the resolution fails with *"you require torch==2.8.0 and torch==2.12.0"*.
`--no-config` makes that explicit:

```bash
cd ~
curl -LsSf https://astral.sh/uv/install.sh | sh && export PATH=$HOME/.local/bin:$PATH
uv venv --python 3.12 ~/train-venv          # the base image ships 3.10; pragmata needs 3.12
uv pip install --no-config --python ~/train-venv/bin/python \
    torch==2.8.0 --index-url https://download.pytorch.org/whl/cu126
uv pip install --no-config --python ~/train-venv/bin/python -r /workspace/configs/eval/train-requirements.txt
~/train-venv/bin/python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
# expect: 2.8.0+cu126 True   <- if this says False, stop; nothing below will use the GPU
```

The `make` targets default to `.venv/bin/python`, which inside the container is the *host's*
venv and cannot train. Point them at the training venv per run:

```bash
cd /workspace
make eval-train TASK=retrieval PY=$HOME/train-venv/bin/python
```

`PRAGMATA_EVAL_SRC` must also resolve inside the container. It is not under `/workspace`, so
either mount it too (`container-create ... -d /home/shared/pragmata-eval`) or export a path to
a checkout that is. If the training extra is missing, `make eval-train` exits with an
instruction rather than a traceback.

## Run order

```bash
make transfer-pull PREFIX=exports-frozen/<FREEZE_DATE>   # if the tree is not already local
make eval-train-inputs                                  # -> data/eval-inputs/training/
make eval-train-seqlen                                   # confirm the truncation picture holds
make eval-train TASK=retrieval                           # then grounding, then generation
```

**`eval-train-inputs`** pools the frozen canonical export into one CSV per task. It derives
the programme list from the export tree via `eval_common.programmes()` rather than carrying
one, and filters to submitted responses. Both matter:

- A hardcoded list is how a directory named `monitor` once entered this pipeline. It was
  never a domain - it was a throwaway export directory `log.py` used to write, which anything
  globbing `exports/*/` read as an extra domain, silently double-counting the last real
  domain in the loop with stale values. Fixed upstream in `09e2a9a`; deriving the list
  removes the possibility of it returning.
- Exports run with `include_discarded=true`. A discarded response is an abstention carrying
  no labels, so pooling raw rows trains on nulls.

`zentrum-fuer-datenmanagement` contributes nothing: it was seeded in Argilla but never
staffed, and is in `EXCLUDED_PROGRAMMES` for every eval output. Seven programmes contribute.

**`eval-train-seqlen`** trains nothing and is worth re-running whenever the export moves
materially, because truncation is silent. It runs the staged CSV through pragmata's own
`import_eval_train_frame` and `build_tlmtc_frame`, so it measures **items** - each record's
responses consolidated by majority, which is the grain tlmtc trains on - rather than the
per-response rows of the CSV. Measured against the current canonical freeze:

| task | items | median tokens | over the 1024 default | at the configured length |
|---|---|---|---|---|
| retrieval | 1,561 | 690 | 10.6% | left at 1024 |
| generation | 713 | 690 | 15.8% | `3072` - 100% covered |
| **grounding** | **447** | **4,056** | **100.0%** | `6144` - 88.1% covered |

Every grounding item was being cut off at the default - the model never saw a complete input.
Raising it further has a steep cost: 8192 would still truncate 3.8%, at more memory than a
40GB A100 has at `batch_size=1`.

These differ slightly from the figures in the eval report, which were taken at per-response
grain against the superseded export. The item counts here are the honest denominator.

## What each task's configuration rests on

The base model is `jhu-clsp/mmBERT-base`, pinned explicitly in `_common.yaml` rather than
left to pragmata's default, so that a default moving upstream shows up as a conflict instead of
silently changing the model. mmBERT beat `answerdotai/ModernBERT-base` on every task tested,
most sharply on grounding, where it was part of what made training possible at all. The exports
are substantially German, which is the likely reason a multilingual base wins.

**Retrieval** is the one result to trust outright. mmBERT + hyperparameter tuning + threshold
optimization with `best_model_metric` pinned explicitly to `roc_auc_macro`. Pinning that
metric is load-bearing rather than cosmetic: `threshold_optimization` otherwise switches
checkpoint selection from AUC to F1 silently, which is exactly what made the same setting
degenerate on the other two tasks. `--threshold-type` selects global (default,
`roc_auc_macro` 0.769) or label-specific (0.752, but `f1_macro` 0.720 against 0.704) - both
are legitimate, global leads on the primary metric.

**Grounding** trains on three of its five labels. `support_present` and `source_cited` have
too few negative examples for any split ratio to give `tlmtc` full class support per label
across train/val/test, so it refuses the run outright:

| label | positive | negative |
|---|---|---|
| `support_present` | 672 | **2** |
| `source_cited` | 669 | **5** |

This is a data floor, not a tuning problem - a later export added grounding rows and not one
new negative for either label. `make eval-train-inputs` prints these counts, so re-check them
when the export moves. Note the consequence: the dropped pair is exactly what
`grounding_presence_rate` and `citation_presence_rate` rest on in the human-label scoring, so
the trained evaluator covers strictly less than those metrics do.

Of the three trained labels, only `unsupported_claim_present` has enough test support (31
positives) to trust. The other two land 2-4 test positives, where a single flipped prediction
moves AUC by 0.2-0.3; treat them as directional until a second seed or more annotation
confirms them. This run is the slow one - `batch_size=1` at `sequence_length=6144`, 2+ hours.

**Generation runs, and is not a trustworthy evaluator.** It is kept for reproducibility, not
recommended for use. `sequence_length=3072` gave a small real gain, but majority-class
collapse persists on the two highest-prevalence labels: `proper_action` and
`response_on_topic` read F1 0.94 and 0.88 while their AUC sits at 0.55-0.57, meaning the model
is still largely predicting the majority class. The cause is upstream of any model choice -
minority-class scarcity (16 negative examples for `response_on_topic` in the whole training
set) compounded by annotator agreement at or near zero for these labels - and is not fixable
by further pipeline work.

## What not to reuse

Tested, found not to help, and deliberately absent from the script so nobody re-derives them
at the cost of a GPU day:

- **`threshold_optimization` on grounding or generation.** Degenerates into predicting
  positive almost everywhere (`pred_prevalence` 0.87-1.0 against a true 0.28) rather than a
  real precision/recall trade. It works on retrieval because there is strong signal
  underneath to redistribute; these two have too little.
- **`hyperparameter_tuning` on grounding or generation.** Grounding: macro AUC flat within
  noise, `f1_macro` worse. Generation: `f1_macro` up, AUC unchanged - a precision/recall
  rebalance, not better discrimination.
- **Oversampling minority-class rows by duplicating CSV lines.** Zero effect: pragmata
  deduplicates rows before training and silently undoes it.
- **Raising grounding's `batch_size` past 1** at `sequence_length=6144`. `batch_size=4`
  exhausted a 40GB A100. Check headroom with `nvidia-smi` before trying 2.

## Long runs

Grounding takes 2+ hours, so launch it detached. The GPU containers survive a disconnect -
SSH drop, laptop sleep - but not an idle timeout with no GPU activity, and not an explicit
stop:

```bash
touch /workspace/.keep-alive    # avoid the idle auto-stop during a long run
nohup make eval-train TASK=grounding > logs/eval-train-grounding-$(date +%Y%m%d_%H%M).log 2>&1 &
disown
```

Follow it with `tail -f` on that log and `nvidia-smi` - expect near-100% GPU utilisation while
it is genuinely training. Results land in `data/eval/train_outputs/<run_id>/evaluation/`.

**Push the outputs off the box before it is torn down**, per
[§11.1](implementation-guide.md#111-return-the-evaluation-outputs):

```bash
make transfer-push SRC=data/eval/train_outputs PREFIX=checkpoints
```

This is not optional housekeeping. The runs behind the report's own numbers were never pushed,
and went with the container - see below.

## Provenance

`data/eval-inputs/training/<task>.csv` ships a `.provenance.json` naming the export rows and
the code that pooled them, the same convention the report CSVs use.

**The report's published numbers are not reproducible from this repository, and the model
figures quoted above are the report's rather than a re-run's** (the truncation table is a
re-run - it was measured against the current pin). The report itself is
`reports/eval/pragmata-eval-report.md`, kept out of git the way
`docs/deployment-inventory.local.md` is, since this repository is public. The runs behind it
used:

- an export tree dated two days before the current canonical freeze, which no longer exists
  anywhere - no freeze was cut that day, and the live Blob `exports/` prefix it came from is
  overwritten by every push;
- a `monitor` directory in that tree, i.e. the double-counting bug above, so its row counts
  are inflated by a stale duplicate of one domain;
- an unpinned `main` of pragmata rather than the `PRAGMATA_EVAL_SRC` pin.

What survives is that the **blocking findings reproduce from the current pin**: grounding's
negative counts for the two dropped labels are the same to within two rows, so the data floor
is a property of the data and not of that particular export. Re-running the configurations
above against the canonical freeze therefore reproduces the *conclusions*; it will not
reproduce the *figures*, and the small differences are expected rather than a regression.
