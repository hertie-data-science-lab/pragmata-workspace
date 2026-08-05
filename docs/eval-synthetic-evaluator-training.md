# Synthetic evaluator training

Training the synthetic evaluators. One evaluator per task is fine-tuned on the pooled human annotations, then applied to unlabelled data by `pragmata eval predict-labels`.

`scripts/eval/train_evaluators.py` drives it; the recommended configuration per task lives in [`configs/eval/training/`](../configs/eval/training/) as a shared `_common.yaml` deep-merged with one file per task, exactly as `querygen_specs/_runtime.yaml` composes with each spec. 

The training itself runs on the GPU host: its dependencies are deliberately outside this workspace's lock, see [The environment](#the-environment).

| Target | Does | Where it runs |
|---|---|---|
| `make eval-train-inputs` | pools the frozen export into `data/eval-inputs/training/<task>.csv` | either box, CPU-only |
| `make eval-train-seqlen` | reports how much of each task's input the default sequence length truncates | either box, CPU-only |
| `make eval-train TASK=<task>` | trains one evaluator into `data/eval/train_outputs/<run_id>/` | GPU host |

## The environment

Two things are pinned separately, for different reasons.

**The pragmata source** is the eval pin already described in [Eval pipeline](eval.md) - the third of the three pins. `PRAGMATA_EVAL_SRC` in `.env` points at a checkout, and the script puts it first on `sys.path` so it shadows the installed annotation pragmata, (which for the pilot is a frozen demo commit with no eval module at all). 

**The training extra is not in `uv.lock`, and the workspace venv cannot train.** This is because lock resolves `torch 2.12.0+cu130`, which needs CUDA 13, while spedcifically, Hertie's `ds01` GPU host's driver (535.309.01) caps at CUDA 12.2 - as training is intended to be run in a container, not on the host. Therfore, we create the container with the workspace mounted, then build the training environment which runs the installs from outside `/workspace` (otherwise uv picks up this repo's `pyproject.toml` and its `constraint-dependencies` pin `torch==2.12.0`, and the resolution fails  The full transitive pin set is
[`configs/eval/train-requirements.txt`](../configs/eval/train-requirements.txt), 

The `make` targets default to `.venv/bin/python`, which inside the container is the *host's* venv and cannot train. Point them at the training venv per run:

```bash
cd /workspace
make eval-train TASK=retrieval PY=$HOME/train-venv/bin/python
```

As such, `PRAGMATA_EVAL_SRC` is not needed inside the container**, and generally will not exist there - it is not under `/workspace`, and only the workspace is mounted. It does not have to: the training venv installs `pragmata[eval]` outright at the eval pin, which `train-requirements.txt` pins by commit, so there is one pragmata and nothing to shadow. 

The script tries a plain `import pragmata.api.eval` first and falls back to the `PRAGMATA_EVAL_SRC` checkout only when that import finds no eval module - which is the CPU VM's situation, where the installed pragmata is the frozen annotation pin. Either way it prints which one answered, so a run log names the commit it trained against. 

## Run order

```bash
make transfer-pull PREFIX=exports-frozen/<FREEZE_DATE>  # if the tree is not already local
make eval-train-inputs                                 # -> data/eval-inputs/training/
make eval-train-seqlen                                 # confirm the truncation picture holds
make eval-train TASK=retrieval PY=$HOME/train-venv/bin/python   # then grounding, generation
```

Only the last line needs `PY=`. The two before it are CPU-only, so the default `.venv/bin/python` runs them wherever they are invoked; `eval-train` is the one that would otherwise reach for the host's venv and fail the GPU check.

>**NB: Where the export tree is read from differs by box, and `eval-train-inputs` handles it.**
>- `transfer-pull` writes only under `data/transfer/` - it refuses any destination that would escape it
>- so on the GPU host the freeze arrives at `data/transfer/exports-frozen/<FREEZE_DATE>/`, 
>- while `eval_common` defaults to the CPU-side `data/annotation/exports-frozen/<FREEZE_DATE>/`. 
>- When the default is absent and the pulled copy is present, the target uses the pulled copy and says so. 
>- Both are the same freeze - the date comes from the committed pin either way - so this is only about where the bytes sit. An explicitly passed `EXPORTS=` is never silently substituted: if that tree is missing, the run
fails.

**`eval-train-inputs`** pools the frozen canonical export into one CSV per task. It derives the programme list from the export tree via `eval_common.programmes()` rather than carrying one, and filters to submitted responses. 

**`eval-train-seqlen`** trains nothing and is worth re-running whenever the export moves materially, because truncation is silent. It runs the staged CSV through pragmata's own `import_eval_train_frame` and `build_tlmtc_frame`, so it measures items (each record's responses consolidated by majority, which is the grain tlmtc trains on - rather than the per-response rows of the CSV). Measured against the current canonical freeze:

| task | items | median tokens | over the 1024 default | at the configured length |
|---|---|---|---|---|
| retrieval | 1,561 | 690 | 10.6% | left at 1024 |
| generation | 713 | 690 | 15.8% | `3072` - 100% covered |
| **grounding** | **447** | **4,056** | **100.0%** | `6144` - 88.1% covered |

Every grounding item was being cut off at the default - the model never saw a complete input. Raising it further has a steep cost: 8192 would still truncate 3.8%, at more memory than a 40GB A100 has at `batch_size=1`.

## What each task's configuration rests on

The base model is `jhu-clsp/mmBERT-base`, pinned explicitly in `_common.yaml` rather than left to pragmata's default. mmBERT beat `answerdotai/ModernBERT-base` on every task tested. The exports are substantially German, which is the reason a multilingual base wins.

## Provenance

Two records, one per side of the staging boundary.

1. `data/eval-inputs/training/<task>.csv` ships a `.provenance.json` naming the export rows and the code that pooled them, the freeze date it was pooled under, the programmes that actually contributed rows for that task, and `output_sha256` (CSV's own bytes). `make eval-train` refuses to start unless that record is present, names the freeze `configs/eval/freeze.conf` currently pins, and matches the CSV on disk. The same check gates `make eval-train-seqlen`.

2. Each training run then writes `train_provenance.workspace.json` into its own run directory (`data/eval/train_outputs/<run_id>/`), so the record travels with the checkpoints when they are pushed off the box. It records the workspace git sha (+ dirty flag), the resolved pragmata eval
source, the staged CSV with its sha256, the freeze date it was pooled under,and the full merged configuration as resolved. 
> NB: The two `.meta.json` files already in that directory cover the other side and not this one: `pragmata_train.meta.json` carries `run_id`, task and a timestamp, and tlmtc's `train_run_meta.json` carries the model-side settings it received (checkpoint, sequence length, label names, threshold type). Neither names which CSV, which freeze, or which commit of this workspace produced the metrics - as is captured in our `train_provenance.workspace.json`.