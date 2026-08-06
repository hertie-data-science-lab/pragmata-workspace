# Synthetic evaluators

One evaluator per task, fine-tuned on the pooled human annotations, then applied to unlabelled populations. `train_evaluators.py` trains, `predict_evaluators.py` stages and applies, `score_synthetic_predictions.py` and `evaluator_report.py` turn the output into [deliverables](report-deliverables.md).

Per-task configuration lives in [`configs/eval/training/`](../configs/eval/training/) as a shared `_common.yaml` deep-merged with one file per task, the same shape as `querygen_specs/_runtime.yaml`. Training and prediction both need the GPU host, whose dependencies are deliberately outside this workspace's lock - see [The environment](#the-environment).

| Target | Does | Where it runs |
|---|---|---|
| `make eval-train-inputs` | pools the frozen export into `data/eval-inputs/training/<task>.csv` | either box, CPU-only |
| `make eval-train-seqlen` | reports how much of each task's input the default sequence length truncates | either box, CPU-only |
| `make eval-train TASK=<task>` | trains one evaluator into `data/eval/train_outputs/<run_id>/` | GPU host |
| `make eval-predict-inputs POPULATION=<p>` | stages the unlabelled per-task CSVs into `data/eval-inputs/predict/<p>/` | either box, CPU-only |
| `make eval-predict TASK=<t> POPULATION=<p> RUN_ID=<id>` | applies one evaluator into `data/eval/prediction_outputs/<run_id>-<p>/` | GPU host |
| `make eval-score-synthetic POPULATION=<p>` | `synthetic_metric_estimates.<p>.csv` | either box, CPU-only |
| `make eval-evaluator-report` | `evaluator_metrics.csv` | either box, CPU-only |
| `make eval-evaluator-report PART=calibration` | `evaluator_calibration.csv` - re-predicts, so it needs a GPU | GPU host |

**Prediction has no YAML configs.** Training's parameters are committed as data because they are pins behind published numbers; prediction's two choices - which population, which evaluator run - are CLI arguments, recorded per run in `predict_provenance.workspace.json`.

## The environment

Two things are pinned separately, for different reasons.

**The pragmata source** is the third of the [three pins](report-deliverables.md#the-three-pins). `PRAGMATA_EVAL_SRC` in `.env` points at a checkout that the script puts first on `sys.path`, shadowing the installed annotation pragmata - which for the pilot is a frozen demo commit with no eval module at all. The script tries a plain `import pragmata.api.eval` first and only falls back to the checkout when that finds no eval module, printing which one answered, so a run log names the commit it trained against.

**The training extra is not in `uv.lock`, and the workspace venv cannot train.** The lock resolves `torch 2.12.0+cu130`, needing CUDA 13, while Hertie's `ds01` host caps at CUDA 12.2 on its driver (535.309.01) - so training runs in a container, not on the host. Create the container with the workspace mounted, then build the training environment with the installs run from *outside* `/workspace`, or uv picks up this repo's `pyproject.toml` and its `constraint-dependencies` pin of `torch==2.12.0` and the resolution fails. The full transitive pin set is [`configs/eval/train-requirements.txt`](../configs/eval/train-requirements.txt).

Inside the container `.venv/bin/python` is the *host's* venv and cannot train, so point the targets at the training venv per run:

```bash
container-attach pragmata-eval-train
cd /workspace
make eval-train TASK=retrieval PY=$HOME/train-venv/bin/python
```

`PRAGMATA_EVAL_SRC` is neither needed nor present in the container - it is not under `/workspace`, and only the workspace is mounted. It does not have to be: the training venv installs `pragmata[eval]` at the eval pin, which `train-requirements.txt` pins by commit, so there is one pragmata and nothing to shadow.

The three GPU targets refuse to start on an interpreter that cannot see one, naming the two ways of getting that wrong (the host's venv inside the container; site policy on the host outside one). `--use-cpu` is the deliberate escape hatch for a smoke test. **Batch size:** tlmtc's default of 32 suits retrieval and generation, but grounding runs at `sequence_length=6144` and wants `BATCH_SIZE=4` - the memory ceiling that forced `batch_size=1` for its training, less severely.

## Training

> **NB: where the export tree is read from differs by box, and `eval-train-inputs` handles it.** `transfer-pull` writes only under `data/transfer/`, so on the GPU host the freeze arrives at `data/transfer/exports-frozen/<FREEZE_DATE>/`, while the default is the CPU-side `data/annotation/exports-frozen/<FREEZE_DATE>/`. When the default is absent and the pulled copy is present, the target uses the pulled copy and says so. It is the same freeze either way - the date comes from the committed pin - so this is only about where the bytes sit. An explicitly passed `EXPORTS=` is never silently substituted: if that tree is missing, the run fails.

**`eval-train-inputs`** pools the frozen export into one CSV per task, deriving the programme list from the tree rather than carrying one, and filtering to submitted responses.

**`eval-train-seqlen`** trains nothing, and is worth re-running whenever the export moves materially, because truncation is silent. It measures items - each record's responses consolidated by majority, the grain tlmtc trains on - rather than the CSV's per-response rows:

| task | items | median tokens | over the 1024 default | at the configured length |
|---|---|---|---|---|
| retrieval | 1,561 | 690 | 10.6% | left at 1024 |
| generation | 713 | 690 | 15.8% | `3072` - 100% covered |
| **grounding** | **447** | **4,056** | **100.0%** | `6144` - 88.1% covered |

Every grounding item was being cut off at the default - the model never saw a complete input. Raising it further is steep: 8192 would still truncate 3.8%, at more memory than a 40GB A100 has at `batch_size=1`.

**What the configuration rests on.** The base model is `jhu-clsp/mmBERT-base`, pinned explicitly rather than left to pragmata's default: it beat `answerdotai/ModernBERT-base` on every task tested, and the exports are substantially German, which is why a multilingual base wins. Grounding trains **three of its five labels** - `support_present` and `source_cited` have too few negative items for any split to give tlmtc full class support. That narrowing is what makes grounding trainable, and also what stops its predictions being scorable ([below](#the-synthetic-estimates)).

## The two populations

They answer different questions.

**`annotated`** is the frozen export with the labels stripped - the same rows `eval_metric_estimates.csv` describes, pooled exactly as training pools them, so each synthetic metric reads straight beside its human counterpart. Two things differ from the training staging, both forced. Every label column is dropped, because pragmata's `validate_eval_predict_frame` does not merely ignore the task's labels, it *rejects* them. And rows are reduced to one per item: the export carries one row per annotator, and with the labels gone those are exact duplicates. The grain is the one pragmata consolidates to anyway, so the population is unchanged and only the redundancy is - landing on the same 1,561 / 447 / 713 items `eval-train-seqlen` reports.

**`corpus`** is the curated corpus, `data/publikationsbot/*_combined.curated.jsonl` - a superset of what was ever annotated. Its text columns are built by pragmata's own import code, so they match the Argilla fields exactly: retrieval pairs the query with each *chunk's* text, grounding pairs the answer with `context_set`, generation pairs the query with the answer. Note `context_set` is a field of the import record carried through verbatim, not assembled from the chunk texts.

Record identity comes from pragmata's own deriving function, so a corpus row and the export row for the same pair carry the *same* `record_uuid`. That is what makes the two populations comparable, and why corpus retrieval panels are complete by construction - every chunk of a pair is staged, so `--skip-incomplete-panels` has nothing to drop. The population is every curated record satisfying pragmata's import contract, which is the same set that could have been annotated: a record the contract rejects (a query whose retrieval returned no chunks, say) could never have reached Argilla either. Rejects are counted in the sidecar rather than silently dropped, and a file of nothing but rejects is fatal.

A third population, **`testsplit`**, is not staged by `eval-predict-inputs`: `evaluator_report.py calibration` stages one per run from that run's own held-out split, which is a property of a training run rather than of the corpus. `make eval-predict POPULATION=testsplit` accepts it so the pass can be repeated by hand, but `RUN_ID` must then name the run the split came from - predicting it with a different evaluator would fill that evaluator's directory with another run's calibration data.

## The output layout, and why it is not pragmata's

**tlmtc names its prediction directory after the evaluator run id, and overwrites it** - no refusal, no versioning, no guard. Predicting a second population with the same evaluator silently replaces the first, and `pragmata_predict.meta.json` is rewritten to match, so afterwards nothing on disk says which population the numbers describe.

So `eval-predict` **moves** a completed run's output to `data/eval/prediction_outputs/<run_id>-<population>/`. The result still scores: `eval score --prediction-id X` resolves the meta file by directory name alone and never checks it against the `run_id` recorded inside, so that field keeps naming the *evaluator*. The staging directory is cleaned *before* a run rather than after, so a leftover `prediction_outputs/<run_id>/` can only be an interrupted run; a completed one always moves. Re-predicting the same (evaluator, population) needs `--overwrite`.

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

Only the GPU lines need `PY=`. `RUN_ID` is optional and should not be treated as such: omitted, the script resolves the latest evaluator for the task, prints that it did, and records it - right for a scratch run, wrong for a published one, because "latest" is a property of the box rather than of the numbers. Pass it for anything whose output leaves the machine.

## Provenance and the freshness rule

Each stage writes two records, one per side of the staging boundary, and the input-side one is **checked** rather than merely written.

**The staged CSV** ships a `.provenance.json` naming its inputs, what was filtered or dropped, and `output_sha256` - the CSV's own bytes. Training's names the freeze date and the programmes that contributed rows; prediction's names the population, the grain, and the label columns it dropped. `eval-train` and `eval-predict` both refuse to start unless that record is present and matches the CSV on disk. `eval-train`, `eval-train-seqlen` and `eval-predict POPULATION=annotated` additionally require it to name the freeze `configs/eval/freeze.conf` currently pins: without that the pin could move while a stale CSV is silently predicted on, since nothing downstream re-reads where the staged rows came from. `POPULATION=testsplit` requires it to name the evaluator run being applied.

The corpus population has no freeze, so its record carries the equivalent: each source JSONL's `sha256` beside the pin for it in [`reproducibility/2026-07-01-annotation-curation/pins.sha256`](../reproducibility/2026-07-01-annotation-curation/pins.sha256), and the comparison outcome. That outcome is recorded **either way** - a match means the predicted population is the corpus the annotations were drawn from, a mismatch means it is not, and both are facts the numbers rest on. `pin_sha256: null` is a third case: the bundle names no pin for that file.

**The run output** then gets a `train_provenance.workspace.json` or `predict_provenance.workspace.json` in its own directory, so the record travels with the run when it is pushed off the box. Both name the workspace git sha and dirty flag, the resolved pragmata eval source, and the staged CSV with its sha256; training adds the freeze date and the full merged configuration, prediction adds the evaluator run and whether it was given or resolved, the resolved `predict_kwargs`, and the evaluator's `label_names`.

> NB: the `.meta.json` files pragmata and tlmtc already write cover the model side, not this one - `run_id`, task, timestamp, checkpoint, sequence length, label names, threshold type. None names which CSV, which freeze, or which commit of this workspace produced the metrics.

## Getting the data in and out

The transport is the [Blob pipe](data-transport.md); this stage adds one prefix, `publikationsbot/`.

**The corpus population needs the curated corpus**, which is produced on the CPU box and is not part of the frozen export, so it travels separately:

```bash
make transfer-push SRC=data/publikationsbot PREFIX=publikationsbot   # CPU box
make transfer-pull PREFIX=publikationsbot                            # GPU box, +verify
```

`eval-predict-inputs POPULATION=corpus` looks in `data/publikationsbot/` first and falls back to `data/transfer/publikationsbot/`, saying which it settled on - the same two-place rule training applies to the export tree. `CORPUS_DIR=` overrides both, and an explicitly named directory is never silently substituted.

**Push the checkpoints and predictions off the box before it is torn down.** Not optional housekeeping: everything else is reproducible from pinned inputs and code, but checkpoints are not - and the runs behind the report's own numbers were never pushed and went with the container.

```bash
make transfer-push SRC=data/eval/train_outputs      PREFIX=checkpoints
make transfer-push SRC=data/eval/prediction_outputs PREFIX=predictions
```

**A pulled tree cannot be consumed where it lands.** `sync.sh` refuses any destination escaping `data/transfer/`, while pragmata resolves evaluator runs and `--prediction-id` under `data/eval/`, so both trees have to be copied across after verifying, before `eval-score-synthetic` or `eval-evaluator-report` can read them:

```bash
make transfer-pull PREFIX=checkpoints && cp -a data/transfer/checkpoints/.  data/eval/train_outputs/
make transfer-pull PREFIX=predictions && cp -a data/transfer/predictions/. data/eval/prediction_outputs/
```

This does not break the [ownership rule](report-deliverables.md#ownership): these files *were* written by pragmata, on the other box.

## Reading the numbers

Every column is defined in the [data dictionary](data-dictionary.md). What the numbers *mean* is here.

### The synthetic estimates

`make eval-score-synthetic` is the twin of `make eval-score`: same CLI, same eval pin, same row-building code, so `synthetic_metric_estimates.<population>.csv` is column-for-column comparable with `eval_metric_estimates.csv` - minus the four `alpha_*` columns, plus `evaluator_run_id`, `prediction_id` and `population`. The `alpha_*` columns are absent by definition, not by omission: a prediction has one label per item and no annotator disagreement to measure. What replaces them is the evaluator's own quality, in `evaluator_metrics.csv`.

**Read the two files together.** A corpus rate produced by a model whose AUC is near chance is not a measurement. The intervals here cover sampling uncertainty over queries *only* - they say nothing about the evaluator being wrong, which is the dominant source of error for two of the three tasks.

- **`annotated` is largely in-sample.** Each evaluator's train and validation splits are roughly three quarters of exactly these items (retrieval 1,152 of 1,561; grounding 335 of 447; generation 530 of 713), so an estimate here is optimistic about the evaluator by an unknown amount. It is the right population for "does the evaluator reproduce the human metric" and the wrong one for "how good is the evaluator".
- **`corpus` is corpus scale with no human baseline at all**, and for grounding and generation it carries the evaluator-quality caveat in full: generation's evaluator shows majority-class collapse on its two highest-prevalence labels (F1 0.93 against an AUC of 0.53), and grounding's is directional at best on two of its three trained labels. Read those rates as an indication of what a better evaluator would be measuring.
- **Retrieval's `n` differs sharply between the two populations.** `--skip-incomplete-panels` drops 283 of 464 panels on `annotated`, exactly as for the human run, because annotation coverage is partial; corpus panels are complete by construction, so a corpus run drops none. The two `n` are not comparable without reading `n_panels_skipped` beside them.
- **Grounding rows are always `n = 0`.** Its evaluator trains three of five labels ([above](#training)) and pragmata's grounding score schema requires all five, built from the label map at import time, so the narrowing that makes training possible cannot reach it. The scorer checks the header up front and writes explicit `n = 0` rows with `status = evaluator_labels_incomplete`, rather than letting the CLI reject the frame with what reads like a staging bug. Inventing the two missing columns would be fabricating labels. The fix is more negative grounding annotation, not more pipeline.

### The evaluator metrics

`evaluator_metrics.csv` describes the **models**, on each run's own held-out test split - 409 / 112 / 183 rows for retrieval / grounding / generation. It reads only what a completed run left on disk, so it needs neither a GPU nor pragmata. Accuracy is not persisted by tlmtc and is derived from the metrics that are; the derivation and the check that keeps it honest are in the [dictionary](data-dictionary.md#evaluator_metricscsv).

- **Trust `roc_auc` over `f1`/`precision`/`recall`.** It is threshold-independent where the others all depend on the run's decision threshold.
- **`accuracy` is a weak summary on skewed labels, and most of these are skewed.** `generation/response_on_topic` has a true prevalence of 0.93, so always predicting positive scores 0.93; its AUC is 0.53.
- **`n` is small.** 112 grounding test rows means one flipped prediction moves a rate by ~0.9 points, and the two labels with 2-4 test positives move AUC by 0.2-0.3. Directional at best.
- **These are the evaluator's metrics, not the corpus's.** Nothing here describes the publikationsbot; it describes how well a model reproduces human labels on held-out annotated data.

### The calibration curve

`evaluator_calibration.csv` is one row per task × label × probability bin, on the same held-out splits. It needs per-item probabilities, which no training artifact holds, so it re-applies each run to its own test split *through the same prediction plumbing everything else uses* - staged input, freshness sidecar, population-named output directory. That reuse is the point: the probabilities behind the CSV are produced the way the published predictions are, not by a private code path that could drift. They are joined back to the held-out labels on a carried row index rather than zipped by position, and the join is checked to cover every test row exactly once.

- **`n` is the first column to read, not the last.** Retrieval's bins hold 9-70 rows (median 41.5), which supports a curve; grounding's one usable label spreads 112 rows over ten bins with seven holding 1-3 (median 3), which does not. A `frac_true` computed on 3 rows can take only four values. Read bins under roughly 20 rows as part of a trend, never individually.
- **Empty bins are skipped, not written as zeros** - a row of zeros would read as "right 0% of the time here", where an absent row is the absence of evidence it is. So a label whose predictions never leave the bottom bin has exactly one row, which is what majority-class collapse looks like and is itself the finding.
- **Calibration is a property of the scores, not of the decision.** A well-calibrated model can still have a badly chosen threshold, and vice versa.

## What is not done

- The transfer-pull-then-copy step above is manual, for both `predictions/` and `checkpoints/`.
- Nothing compares the two `synthetic_metric_estimates.*.csv` against `eval_metric_estimates.csv` automatically; that comparison, and the report it goes into, live in the private report repository.
- Corpus prevalence for grounding is not produced by any route. The scoring path refuses it for the reason above, and computing it directly from `predictions.csv` was deliberately not done: it would produce a number the score CLI would not, from a code path nothing else uses, for three of the five labels the taxonomy asks about.
