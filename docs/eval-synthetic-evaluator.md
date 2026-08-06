# Synthetic evaluators

One evaluator per task is fine-tuned on the pooled human annotations, then applied to unlabelled populations by `pragmata eval predict-labels`. `scripts/eval/train_evaluators.py` trains; `scripts/eval/predict_evaluators.py` stages and applies; `score_synthetic_predictions.py` and `evaluator_report.py` turn the output into deliverables.

The recommended configuration per task lives in [`configs/eval/training/`](../configs/eval/training/) as a shared `_common.yaml` deep-merged with one file per task, exactly as `querygen_specs/_runtime.yaml` composes with each spec. Training and prediction both run on the GPU host: their dependencies are deliberately outside this workspace's lock, see [The environment](#the-environment).

| Target | Does | Where it runs |
|---|---|---|
| `make eval-train-inputs` | pools the frozen export into `data/eval-inputs/training/<task>.csv` | either box, CPU-only |
| `make eval-train-seqlen` | reports how much of each task's input the default sequence length truncates | either box, CPU-only |
| `make eval-train TASK=<task>` | trains one evaluator into `data/eval/train_outputs/<run_id>/` | GPU host |
| `make eval-predict-inputs POPULATION=<p>` | stages the unlabelled per-task CSVs into `data/eval-inputs/predict/<p>/` | either box, CPU-only |
| `make eval-predict TASK=<t> POPULATION=<p> RUN_ID=<id>` | applies one evaluator, filing output as `data/eval/prediction_outputs/<run_id>-<p>/` | GPU host |
| `make eval-score-synthetic POPULATION=<p>` | `synthetic_metric_estimates.<p>.csv`, via `pragmata eval score --prediction-id` | either box, CPU-only |
| `make eval-evaluator-report` | `evaluator_metrics.csv` | either box, CPU-only |
| `make eval-evaluator-report PART=calibration` | `evaluator_calibration.csv` - needs per-item probabilities, so it re-predicts | GPU host |

**Prediction has no YAML configs, deliberately.** Training's parameters are committed as data because they are pins behind published numbers: every metric in the report was produced at them. Prediction's two choices - which population, which evaluator run - are CLI arguments instead, because a prediction is a *use* of a pinned model rather than a new pin. What each run actually used is recorded per run, in `predict_provenance.workspace.json`.

## The environment

Two things are pinned separately, for different reasons.

**The pragmata source** is the eval pin already described in [Human annotation scoring](eval-human-annotation.md#the-three-pins) - the third of the three pins. `PRAGMATA_EVAL_SRC` in `.env` points at a checkout, and the script puts it first on `sys.path` so it shadows the installed annotation pragmata (which for the pilot is a frozen demo commit with no eval module at all).

**The training extra is not in `uv.lock`, and the workspace venv cannot train.** The lock resolves `torch 2.12.0+cu130`, which needs CUDA 13, while Hertie's `ds01` GPU host caps at CUDA 12.2 on its driver (535.309.01) - so training is intended to run in a container, not on the host. Create the container with the workspace mounted, then build the training environment running the installs from *outside* `/workspace` (otherwise uv picks up this repo's `pyproject.toml`, whose `constraint-dependencies` pin `torch==2.12.0`, and the resolution fails). The full transitive pin set is [`configs/eval/train-requirements.txt`](../configs/eval/train-requirements.txt).

The `make` targets default to `.venv/bin/python`, which inside the container is the *host's* venv and cannot train. Point them at the training venv per run:

```bash
container-attach pragmata-eval-train
cd /workspace
make eval-train TASK=retrieval PY=$HOME/train-venv/bin/python
```

As such, `PRAGMATA_EVAL_SRC` is not needed inside the container, and generally will not exist there - it is not under `/workspace`, and only the workspace is mounted. It does not have to: the training venv installs `pragmata[eval]` outright at the eval pin, which `train-requirements.txt` pins by commit, so there is one pragmata and nothing to shadow.

The script tries a plain `import pragmata.api.eval` first and falls back to the `PRAGMATA_EVAL_SRC` checkout only when that import finds no eval module - which is the CPU VM's situation, where the installed pragmata is the frozen annotation pin. Either way it prints which one answered, so a run log names the commit it trained against.

`eval-train`, `eval-predict` and `eval-evaluator-report PART=calibration` all refuse to start on an interpreter that cannot see a GPU, naming the two ways of getting that wrong (the host's venv inside the container; site policy on the host outside one). `--use-cpu` is the deliberate escape hatch for a smoke test. **Batch size:** tlmtc's default of 32 is fine for retrieval and generation, but grounding runs at `sequence_length=6144` and wants `BATCH_SIZE=4` - the same memory ceiling that forced `batch_size=1` for its training, less severely.

## Training

> **NB: Where the export tree is read from differs by box, and `eval-train-inputs` handles it.** `transfer-pull` writes only under `data/transfer/` - it refuses any destination that would escape it - so on the GPU host the freeze arrives at `data/transfer/exports-frozen/<FREEZE_DATE>/`, while `eval_common` defaults to the CPU-side `data/annotation/exports-frozen/<FREEZE_DATE>/`. When the default is absent and the pulled copy is present, the target uses the pulled copy and says so. Both are the same freeze - the date comes from the committed pin either way - so this is only about where the bytes sit. An explicitly passed `EXPORTS=` is never silently substituted: if that tree is missing, the run fails.

**`eval-train-inputs`** pools the frozen canonical export into one CSV per task. It derives the programme list from the export tree via `eval_common.programmes()` rather than carrying one, and filters to submitted responses.

**`eval-train-seqlen`** trains nothing and is worth re-running whenever the export moves materially, because truncation is silent. It runs the staged CSV through pragmata's own `import_eval_train_frame` and `build_tlmtc_frame`, so it measures items (each record's responses consolidated by majority, which is the grain tlmtc trains on - rather than the per-response rows of the CSV). Measured against the current canonical freeze:

| task | items | median tokens | over the 1024 default | at the configured length |
|---|---|---|---|---|
| retrieval | 1,561 | 690 | 10.6% | left at 1024 |
| generation | 713 | 690 | 15.8% | `3072` - 100% covered |
| **grounding** | **447** | **4,056** | **100.0%** | `6144` - 88.1% covered |

Every grounding item was being cut off at the default - the model never saw a complete input. Raising it further has a steep cost: 8192 would still truncate 3.8%, at more memory than a 40GB A100 has at `batch_size=1`.

**What each task's configuration rests on.** The base model is `jhu-clsp/mmBERT-base`, pinned explicitly in `_common.yaml` rather than left to pragmata's default. mmBERT beat `answerdotai/ModernBERT-base` on every task tested. The exports are substantially German, which is the reason a multilingual base wins. Grounding trains **three of its five labels**: `support_present` and `source_cited` have too few negative items for any split ratio to give tlmtc full class support, so they are not trained at all. That narrowing is what makes grounding trainable, and it is also what stops its predictions being scorable - see [Reading the numbers](#reading-the-numbers).

## Prediction: the two populations

They answer different questions, and the difference is the whole point of having both.

**`annotated`** is the frozen canonical export with the labels stripped - the same rows the human-label metrics in `eval_metric_estimates.csv` describe, pooled exactly as `eval-train-inputs` pools them, so each synthetic metric can be read straight beside its human counterpart. Two things differ from the training staging, both forced. **Every label column is dropped**, because pragmata's `validate_eval_predict_frame` does not merely ignore the task's label columns, it *rejects* them - which is the right call, since an input carrying the answers invites scoring a model against its own input. And **rows are reduced to one per item**: the export carries one row per annotator, and with the labels gone those are exact duplicates of the same text. The grain is `eval_common.ITEM_KEYS`, which is what pragmata consolidates to anyway, so the population is unchanged and only the redundancy is - landing on the same 1,561 / 447 / 713 items `eval-train-seqlen` reports.

**`corpus`** is the curated corpus - `data/publikationsbot/*_combined.curated.jsonl`, a superset of what was ever annotated. Its text columns are built the way pragmata builds the Argilla fields at annotation import, read out of its own `record_builder`: retrieval pairs the query with each *chunk's* text, grounding pairs the answer with `context_set`, generation pairs the query with the answer. Note that `context_set` is a **field of the import record**, carried through verbatim - pragmata does not assemble it out of the chunk texts. `record_uuid` comes from pragmata's own `derive_record_uuid`, so a corpus row and the export row for the same pair carry the *same* identity: that is what makes the two populations comparable, and it is also why corpus retrieval panels are complete by construction, leaving `--skip-incomplete-panels` nothing to drop. The population is "every curated record that satisfies pragmata's import contract", which is the same set that could have been annotated - a record the contract rejects (a query whose retrieval returned no chunks, say) could never have reached Argilla either. Rejects are counted and recorded in the sidecar rather than silently dropped, and a file with nothing but rejects is fatal.

A third population, **`testsplit`**, exists but is not staged by `eval-predict-inputs`: `evaluator_report.py calibration` stages one per run from that run's own held-out split, which is a property of a training run rather than of the corpus. `make eval-predict POPULATION=testsplit` accepts it so that pass can be repeated by hand, but `RUN_ID` must then name the run the split was staged from - a split belongs to the run that held it out, and predicting it with a different evaluator would fill that evaluator's directory with another run's calibration data.

## The output layout, and why it is not pragmata's

**tlmtc names its prediction directory after the evaluator run id, and overwrites.** `resolve_prediction_paths` builds `prediction_outputs/<run_id>/`, and `predict_tlmtc` writes `probabilities.csv` and `predictions.csv` into it through `mkdir(exist_ok=True)` and a plain `to_csv` - no refusal, no versioning, no guard of any kind. Predicting a second population with the same evaluator overwrites the first silently, and `pragmata_predict.meta.json` is rewritten to match, so afterwards nothing on disk says which population the numbers describe. With three populations per evaluator that is a certainty, not a risk.

So `eval-predict` **moves** a completed run's output tree to `data/eval/prediction_outputs/<run_id>-<population>/`. The constraint that scheme had to satisfy is that the result still scores, and it does: `eval score --prediction-id X` resolves `prediction_outputs/X/pragmata_predict.meta.json` by directory name alone (`resolve_eval_predict_meta_path`), validates the `task` recorded inside it, and scores the `predictions.csv` beside it. It never compares the meta's own `run_id` field against the directory name, so that field keeps naming the *evaluator* - the more useful of the two things it could say, and what the workspace record beside it cross-references. The staging directory is cleaned *before* a run rather than after, and the run says so: a leftover `prediction_outputs/<run_id>/` can only be an interrupted run, because a completed one always moves. Re-predicting the same (evaluator, population) is refused unless `--overwrite` is passed.

## Run order

```bash
make transfer-pull PREFIX=exports-frozen/<FREEZE_DATE>  # if the tree is not already local
make eval-train-inputs                                 # -> data/eval-inputs/training/
make eval-train-seqlen                                 # confirm the truncation picture holds

export PY=$HOME/train-venv/bin/python                  # GPU container, from here down
make eval-train TASK=retrieval PY=$PY                  # then grounding, generation

make eval-predict-inputs POPULATION=annotated          # CPU-only, either box
make eval-predict-inputs POPULATION=corpus             # needs the curated corpus, see below
make eval-predict TASK=retrieval  POPULATION=annotated RUN_ID=<retrieval-run>  PY=$PY
make eval-predict TASK=grounding  POPULATION=annotated RUN_ID=<grounding-run>  PY=$PY BATCH_SIZE=4
make eval-predict TASK=generation POPULATION=annotated RUN_ID=<generation-run> PY=$PY
# ...and the same three with POPULATION=corpus
make eval-evaluator-report PART=calibration PY=$PY     # re-predicts each run's own test split

make eval-evaluator-report                             # evaluator_metrics.csv, either box
make eval-score-synthetic POPULATION=annotated
make eval-score-synthetic POPULATION=corpus
```

Only the GPU-host lines need `PY=`; the CPU-only targets run wherever they are invoked on the default `.venv/bin/python`.

`RUN_ID` is optional and should not be treated as such. Omitted, the script resolves the latest evaluator for the task, prints that it did, and records it - which is right for a scratch run and wrong for a published one, because "latest" is a property of the box rather than of the numbers. Pass it for anything whose output leaves the machine.

## Provenance and the freshness rule

Two records per stage, one per side of the staging boundary, and the second is checked rather than merely written.

**Training.** `data/eval-inputs/training/<task>.csv` ships a `.provenance.json` naming the export rows and the code that pooled them, the freeze date it was pooled under, the programmes that actually contributed rows for that task, and `output_sha256` (the CSV's own bytes). `make eval-train` refuses to start unless that record is present, names the freeze `configs/eval/freeze.conf` currently pins, and matches the CSV on disk; the same check gates `make eval-train-seqlen`. Each training run then writes `train_provenance.workspace.json` into its own run directory, so the record travels with the checkpoints when they are pushed off the box - the workspace git sha (plus dirty flag), the resolved pragmata eval source, the staged CSV with its sha256, the freeze date, and the full merged configuration as resolved.

> NB: The two `.meta.json` files already in that directory cover the other side and not this one. `pragmata_train.meta.json` carries `run_id`, task and a timestamp, and tlmtc's `train_run_meta.json` carries the model-side settings it received (checkpoint, sequence length, label names, threshold type). Neither names which CSV, which freeze, or which commit of this workspace produced the metrics.

**Prediction.** `data/eval-inputs/predict/<population>/<task>.csv` ships a `.provenance.json` naming its inputs, the population, the grain, the labels it dropped, and `output_sha256`. `eval-predict` refuses to start unless that record is present, names the population being asked for, and matches the CSV on disk. For the `annotated` population it must also name the freeze currently pinned: without that check the pin can move while a stale CSV is predicted on silently, since nothing else downstream re-reads where the staged rows came from. For `testsplit` it must name the evaluator run being applied.

The corpus population has no freeze to check, so its record carries the equivalent: each source JSONL's `sha256` beside the pin for it in [`reproducibility/2026-07-01-annotation-curation/pins.sha256`](../reproducibility/2026-07-01-annotation-curation/pins.sha256), and the comparison outcome. That outcome is recorded **either way** - a match means the predicted population is the same corpus the annotations were drawn from, a mismatch means it is not, and both are facts the numbers rest on. `pin_sha256: null` is a third case: the bundle names no pin for that file at all.

Each completed prediction then writes `predict_provenance.workspace.json` into its own output directory, naming the population, the evaluator run and whether it was given or resolved, the input CSV with its `sha256` and the relevant half of its sidecar, the resolved `predict_kwargs`, and the evaluator's `label_names`. The `.workspace.` infix marks it as this repository's file in a tree pragmata otherwise owns.

## Getting the data in and out

The transport is the existing [Blob pipe](data-transport.md); the model stage adds no new prefix beyond `publikationsbot/`.

**The corpus population needs the curated corpus, which is produced on the CPU box.** It is not part of the frozen export, so it travels separately:

```bash
make transfer-push SRC=data/publikationsbot PREFIX=publikationsbot   # CPU box
make transfer-pull PREFIX=publikationsbot                            # GPU box, +verify
```

`eval-predict-inputs POPULATION=corpus` looks in `data/publikationsbot/` first and falls back to `data/transfer/publikationsbot/`, saying which it settled on - the same two-place rule `eval-train-inputs` applies to the export tree. `CORPUS_DIR=` overrides both, and an explicitly named directory is never silently substituted.

**Push the checkpoints and predictions off the box before it is torn down.** This is not optional housekeeping: everything else is reproducible from pinned inputs and code, but checkpoints are expensive to regenerate, and the runs behind the report's own numbers were never pushed and went with the container.

```bash
make transfer-push SRC=data/eval/train_outputs      PREFIX=checkpoints
make transfer-push SRC=data/eval/prediction_outputs PREFIX=predictions
```

**A pulled tree cannot be consumed where it lands.** `sync.sh` refuses any destination that would escape `data/transfer/`, while pragmata resolves evaluator runs and `--prediction-id` under `data/eval/` - so both trees have to be copied across after verifying, before `eval-score-synthetic` or `eval-evaluator-report` can read them:

```bash
make transfer-pull PREFIX=checkpoints && cp -a data/transfer/checkpoints/.  data/eval/train_outputs/
make transfer-pull PREFIX=predictions && cp -a data/transfer/predictions/. data/eval/prediction_outputs/
```

Copying pragmata's own output into pragmata's own tool tree does not break the [ownership rule](eval-human-annotation.md#ownership) - these files *were* written by pragmata, on the other box - but the manual step is a rough edge rather than a design. The export prefixes need none of this: the scripts that read them fall back to the `data/transfer/` copy themselves.

## Reading the numbers

`make eval-score-synthetic` is the twin of `make eval-score`: same `pragmata eval score` CLI, same eval pin, same `PYTHONPATH` shadow, and the same row-building code, so `synthetic_metric_estimates.<population>.csv` is column-for-column comparable with `eval_metric_estimates.csv` - minus the four `alpha_*` columns and plus `evaluator_run_id`, `prediction_id` and `population`. The `alpha_*` columns are absent by definition rather than by omission: a prediction has one label per item and no annotator disagreement to measure. `--prediction-id` is discovered rather than parsed out of directory names, by reading each prediction directory's `predict_provenance.workspace.json`; two evaluators predicting the same (task, population) is refused rather than resolved by recency.

`evaluator_metrics.csv` and `evaluator_calibration.csv` describe the **models**, on each run's own held-out test split - 409 / 112 / 183 rows for retrieval / grounding / generation. `metrics` reads only what a completed run left on disk, so it needs neither a GPU nor pragmata; **accuracy is not persisted by tlmtc** and is derived from the metrics that are, with the derivation and the check that keeps it honest in the [data dictionary](deliverables-data-dictionary.md#evaluator_metricscsv). `calibration` needs per-item probabilities, which no training artifact holds, so it re-applies each run to its own test split *through the same prediction plumbing everything else uses* - staged input, freshness sidecar, population-named output directory. That reuse is the point: the probabilities behind the CSV are produced the way the published predictions are, not by a private code path that could drift from them.

Every column of all three CSVs is defined in the [data dictionary](deliverables-data-dictionary.md). Three things about how to read them belong here:

- **`annotated` is largely in-sample.** Each evaluator's train and validation splits are roughly three quarters of exactly these items (retrieval 1,152 of 1,561; grounding 335 of 447; generation 530 of 713), so a synthetic estimate here is optimistic about the evaluator by an unknown amount. It is the right population for "does the evaluator reproduce the human metric", not for "how good is the evaluator" - that is `evaluator_metrics.csv`, computed on held-out rows only.
- **`corpus` is corpus scale with no human baseline at all**, and for grounding and generation it carries the evaluator-quality caveat in full: generation's evaluator shows majority-class collapse on its two highest-prevalence labels (F1 0.93 against an AUC of 0.53), and grounding's is directional at best on two of its three trained labels. Treat those corpus rates as an indication of what a better evaluator would be measuring, not as a measurement. Retrieval is the exception in the other direction: corpus panels are complete by construction, so a corpus run drops none, where the annotated run scores 181 of 464 panels (`n_panels_skipped = 283`) because annotation coverage is partial. The two `n` values are not comparable without reading `n_panels_skipped` beside them.
- **Grounding cannot be scored through pragmata at all.** Its evaluator trains three of five labels (above), pragmata's `GROUNDING_SCORE_SCHEMA` requires all five, and that schema is built from `LABEL_COLUMNS_BY_TASK` at module *import* time, so the narrowing that makes training possible cannot reach it. The scorer therefore checks the prediction's header up front and writes explicit `n = 0` rows with `status = evaluator_labels_incomplete` for every grounding metric, rather than letting the CLI reject the frame with a schema error that reads like a staging bug. The alternative - inventing the two missing columns - would be fabricating labels. What would fix it is more negative grounding annotation, not more pipeline.

## What is not done

- The transfer-pull-then-copy step above is manual, for both `predictions/` and `checkpoints/`.
- Nothing compares the two `synthetic_metric_estimates.*.csv` files against `eval_metric_estimates.csv` automatically; the comparison, and the report it goes into, live in the private report repository.
- Corpus prevalence for grounding is not produced by any route. The pragmata scoring path refuses it for the reason above, and computing it directly from `predictions.csv` was deliberately not done: it would produce a number the score CLI would not, from a code path nothing else uses, for three of the five labels the metric taxonomy asks about.
