# Eval training

Training the synthetic evaluators - the third part of the [eval pipeline](eval.md), beside
data transport and scoring human labels. One evaluator per task is fine-tuned on the pooled
human annotations, then applied to unlabelled data by `pragmata eval predict-labels`.

`scripts/eval/train_evaluators.py` drives it; the recommended configuration per task lives in
[`configs/eval/training/`](../configs/eval/training/) as a shared `_common.yaml` deep-merged
with one file per task, exactly as `querygen_specs/_runtime.yaml` composes with each spec. How
much of that is actually validated is
[spelled out beside the files](../configs/eval/README.md) - the top level is, `train_kwargs` is
a verbatim passthrough to tlmtc and is not. The training itself runs on the GPU host: its
dependencies are deliberately outside this workspace's lock, see
[The environment](#the-environment).

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
`torch==2.12.0`, and the resolution fails with *"you require torch==2.9.1 and torch==2.12.0"*.
`--no-config` makes that explicit:

```bash
cd ~
curl -LsSf https://astral.sh/uv/install.sh | sh && export PATH=$HOME/.local/bin:$PATH
uv venv --python 3.12 ~/train-venv          # the base image ships 3.10; pragmata needs 3.12
uv pip install --no-config --python ~/train-venv/bin/python \
    torch==2.9.1 --index-url https://download.pytorch.org/whl/cu128
uv pip install --no-config --python ~/train-venv/bin/python \
    -r /workspace/configs/eval/train-requirements.txt
~/train-venv/bin/python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
# expect: 2.9.1+cu128 True   <- if this says False, stop; nothing below will use the GPU
```

**torch goes first, and from the cu128 index.** Installing `tlmtc[train]` first pulls the
default PyPI torch, which is a cu130 build the driver cannot use - the smoke test then says
`False` and nothing trains. The full transitive pin set is
[`configs/eval/train-requirements.txt`](../configs/eval/train-requirements.txt), 134 packages
frozen from a verified container. It carries the cu128 `--extra-index-url` itself, so it also
resolves standalone - the separate torch step above is about install *order*, not about reaching
the index.

Two notes on the version. cu128 wheels run on a CUDA 12.2 driver through CUDA 12.x
minor-version compatibility - verified on `ds01`, real GPU matmul included. And 2.9 is the
floor `tlmtc[train]` declares (`torch<3.0,>=2.9`): the `torch==2.8.0` in the original handover
notes silently violated that, which would surface as an obscure failure only once tlmtc reached
an API added in 2.9 - potentially hours into the grounding run.

The `make` targets default to `.venv/bin/python`, which inside the container is the *host's*
venv and cannot train. Point them at the training venv per run:

```bash
cd /workspace
make eval-train TASK=retrieval PY=$HOME/train-venv/bin/python
```

**`PRAGMATA_EVAL_SRC` is not needed inside the container**, and generally will not exist there -
it is not under `/workspace`, and only the workspace is mounted. It does not have to: the
training venv installs `pragmata[eval]` outright at the eval pin, which
`train-requirements.txt` pins by commit, so there is one pragmata and nothing to shadow. The
script tries a plain `import pragmata.api.eval` first and falls back to the `PRAGMATA_EVAL_SRC`
checkout only when that import finds no eval module - which is the CPU VM's situation, where
the installed pragmata is the frozen annotation pin. Either way it prints which one answered, so
a run log names the commit it trained against rather than leaving it to be inferred. If the
training extra is missing altogether, `make eval-train` exits with an instruction rather than a
traceback.

## Run order

```bash
make transfer-pull PREFIX=exports-frozen/<FREEZE_DATE>  # if the tree is not already local
make eval-train-inputs                                 # -> data/eval-inputs/training/
make eval-train-seqlen                                 # confirm the truncation picture holds
make eval-train TASK=retrieval PY=$HOME/train-venv/bin/python   # then grounding, generation
```

Only the last line needs `PY=`. The two before it are CPU-only, so the default `.venv/bin/python`
runs them wherever they are invoked; `eval-train` is the one that would otherwise reach for the
host's venv and fail the GPU check.

**Where the export tree is read from differs by box, and `eval-train-inputs` handles it.**
`transfer-pull` writes only under `data/transfer/` - it refuses any destination that would
escape it - so on the GPU host the freeze arrives at
`data/transfer/exports-frozen/<FREEZE_DATE>/`, while `eval_common` defaults to the CPU-side
`data/annotation/exports-frozen/<FREEZE_DATE>/`. When the default is absent and the pulled copy
is present, the target uses the pulled copy and says so. Both are the same freeze - the date
comes from the committed pin either way - so this is only about where the bytes sit. An
explicitly passed `EXPORTS=` is never silently substituted: if that tree is missing, the run
fails.

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

It prints per-label positive/negative counts at that same item grain too, and it is the only
target that can: `eval-train-inputs` counts response rows, and the gap between the two is what
decides whether a label is trainable at all. For grounding it also reports the two labels the
run drops, marked as such - their floor is the thing a future export has to lift.

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
are legitimate, global leads on the primary metric. It applies to retrieval only: `tlmtc` reads
`threshold_type` only when `threshold_optimization` is on, so passing it for grounding or
generation is refused up front rather than accepted and quietly dropped.

**Grounding** trains on three of its five labels. `support_present` and `source_cited` have
too few negative examples for any split ratio to give `tlmtc` full class support per label
across train/val/test, so it refuses the run outright. The counts that decide this are at
**item** grain - 447 grounding items, each one record's responses consolidated by majority,
which is what `tlmtc` splits:

| label | positive items | negative items | (response rows) |
|---|---|---|---|
| `support_present` | 445 | **2** | 672 / 2 |
| `source_cited` | 446 | **1** | 669 / 5 |

So the splitter has two negative examples for `support_present` and **one** for `source_cited`,
not two and five: the response-row counts are the larger, friendlier-looking numbers, and they
are not the constraint. `make eval-train-seqlen` prints the item counts and
`make eval-train-inputs` the row ones, so re-check both when the export moves.

This is a data floor, not a tuning problem - a later export added grounding rows and not one
new negative for either label. Note the consequence: the dropped pair is exactly what
`grounding_presence_rate` and `citation_presence_rate` rest on in the human-label scoring, so
the trained evaluator covers strictly less than those metrics do. Note also what the drop does
*not* change - both columns are still required in the staged CSV, because pragmata's grounding
input schema was built from the full label set at import time and the narrowing cannot reach it.
The two labels leave the training targets, not the input contract.

Of the three trained labels, only `unsupported_claim_present` has enough test support (31
positives) to trust. The other two land 2-4 test positives, where a single flipped prediction
moves AUC by 0.2-0.3; treat them as directional until a second seed or more annotation
confirms them. This run is the slow one - `batch_size=1` at `sequence_length=6144`, 2+ hours.

**Generation runs, and is not a trustworthy evaluator.** It is kept for reproducibility, not
recommended for use. `sequence_length=3072` gave a small real gain, but majority-class
collapse persists on the two highest-prevalence labels: `proper_action` and
`response_on_topic` read F1 0.94 and 0.88 while their AUC sits at 0.55-0.57, meaning the model
is still largely predicting the majority class. The cause is upstream of any model choice -
minority-class scarcity (29 negative items for `response_on_topic` out of 713, from 33 negative
response rows) compounded by annotator agreement at or near zero for these labels - and is not
fixable by further pipeline work.

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
nohup make eval-train TASK=grounding PY=$HOME/train-venv/bin/python \
  > logs/eval-train-grounding-$(date +%Y%m%d_%H%M).log 2>&1 &
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

Two records, one per side of the staging boundary, and they are load-bearing rather than
decorative - the second is checked, not just written.

`data/eval-inputs/training/<task>.csv` ships a `.provenance.json` naming the export rows and the
code that pooled them, the same convention the report CSVs use, plus the freeze date it was
pooled under, the programmes that actually contributed rows for that task, and `output_sha256` -
the CSV's own bytes. **`make eval-train` refuses to start unless that record is present, names
the freeze `configs/eval/freeze.conf` currently pins, and matches the CSV on disk.** Without
that check the freeze pin can move while a stale CSV trains on silently: nothing else downstream
re-reads where the staged rows came from. The same check gates `make eval-train-seqlen`, whose
answer a stale CSV falsifies in exactly the same way.

Each training run then writes `train_provenance.workspace.json` into its own run directory
(`data/eval/train_outputs/<run_id>/`), so the record travels with the checkpoints when they are
pushed off the box. The `.workspace.` infix marks it as this repository's file in a tree pragmata
otherwise owns. It records the workspace git sha and dirty flag, the resolved pragmata eval
source, the staged CSV with its sha256, the freeze date it was pooled under, the **full merged
configuration as resolved** - including a `--threshold-type` override - and, for grounding, the
narrowed label tuple the run actually trained on. The two sidecars already in that directory
cover the other side and not this one: `pragmata_train.meta.json` carries `run_id`, task and a
timestamp, and tlmtc's `train_run_meta.json` carries the model-side settings it received
(checkpoint, sequence length, label names, threshold type). Neither names which CSV, which
freeze, or which commit of this workspace produced the metrics. The same configuration is
echoed to stderr at run start, which puts it in the nohup log too.

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
