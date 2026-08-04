# Eval training

Training the synthetic evaluators - the third part of the [eval pipeline](eval.md), beside
data transport and scoring human labels. One evaluator per task is fine-tuned on the pooled
human annotations, then applied to unlabelled data by `pragmata eval predict-labels`.

`scripts/eval/train_evaluators.py` holds the recommended configuration per task and the two
diagnostics that justify it. It runs on the GPU host: the training dependencies are
deliberately outside this workspace's lock, see [The environment](#the-environment).

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

**So training runs in a container, not on the host.** `ds01` is a shared bare-metal box, and
its own workspace venv is the one described above. Launch a container with a GPU assigned:

```bash
project-launch --guided     # select the pragmata project, 1 GPU
```

Inside it, the checkout appears at `/workspace`. Nothing needs adjusting for that - every path
is resolved from the script's own location via `scripts/lib/workspace.py`, so the `make`
targets behave identically in the container and on the host. Then build the training
environment:

```bash
uv pip install -e "$PRAGMATA_EVAL_SRC/..[eval]"
uv pip install torch==2.8.0 --index-url https://download.pytorch.org/whl/cu126
python3 -c "import torch; print(torch.__version__, torch.cuda.is_available())"
# expect: 2.8.0+cu126 True   <- if this says False, stop; nothing below will use the GPU
```

Run the training commands with `PRAGMATA_EVAL_SRC` set the same way it is on the CPU VM. If
the extra is missing, `make eval-train` exits with that instruction rather than a traceback.

**Check the GPUs are free before claiming one.** The box is shared and other people's jobs run
on it; `nvidia-smi` shows per-GPU memory and utilisation. Take an idle one.

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
materially, because truncation is silent. Measured against the current canonical freeze:

| task | rows over the 1024 default | median tokens | coverage at the chosen length |
|---|---|---|---|
| retrieval | 9.9% | 685 | left at the 1024 default |
| generation | 15.6% | 681 | `sequence_length=3072` - 100% |
| **grounding** | **100.0%** | **4,129** | `sequence_length=6144` - 85.8% |

Every grounding row was being cut off at the default - the model never saw a complete input.
Raising it further has a steep cost: 8192 would still truncate 3.9%, at more memory than a
40GB A100 has at `batch_size=1`.

## What each task's configuration rests on

The base model is pragmata's own default, `jhu-clsp/mmBERT-base`, and **no `checkpoint`
override is passed anywhere**. That is deliberate: mmBERT beat `answerdotai/ModernBERT-base`
on every task tested, most sharply on grounding, where it was part of what made training
possible at all. The exports are substantially German, which is the likely reason a
multilingual base wins.

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
