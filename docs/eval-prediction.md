# Eval prediction

Applying the synthetic evaluators - the fourth part of the [eval pipeline](eval.md), after
data transport, scoring human labels and [training](eval-training.md). A trained evaluator is
run over an unlabelled population, and its output is either scored into corpus metrics (the
twin of the human-label scoring) or used to describe the evaluator itself.

Three scripts, all under `scripts/eval/`:

| Target | Does | Where it runs |
|---|---|---|
| `make eval-predict-inputs POPULATION=<p>` | stages the unlabelled per-task CSVs into `data/eval-inputs/predict/<p>/` | either box, CPU-only |
| `make eval-predict TASK=<t> POPULATION=<p> RUN_ID=<id>` | runs one evaluator, filing output as `data/eval/prediction_outputs/<run_id>-<p>/` | GPU host, training venv |
| `make eval-score-synthetic POPULATION=<p>` | `synthetic_metric_estimates.<p>.csv`, via `pragmata eval score --prediction-id` | either box, CPU-only |
| `make eval-evaluator-report` | `evaluator_metrics.csv` | either box, CPU-only |
| `make eval-evaluator-report PART=calibration` | `evaluator_calibration.csv` - needs per-item probabilities, so it re-predicts | GPU host, training venv |

**Vocabulary** is the [data dictionary](eval-data-dictionary.md)'s, as everywhere else in
eval, and it defines every column of the three CSVs above.

**There are no YAML configs for this stage, deliberately.** Training's parameters live in
`configs/eval/training/` because they are pins behind published numbers. Prediction's two
choices - which population, which evaluator run - are CLI arguments instead, because a
prediction is a *use* of a pinned model rather than a new pin, and pinning the arguments before
the final run would freeze a choice nobody has made yet. What each run actually used is
recorded per run instead, in `predict_provenance.workspace.json`.

## The environment

The same one training uses, for the same reason: inference loads the fine-tuned model through
`tlmtc`, so it needs `pragmata[eval]` and a CUDA `torch` the driver can run, neither of which
is in this workspace's `uv.lock`. [Eval training - the environment](eval-training.md#the-environment)
has the whole picture, including why the lock cannot simply move. Nothing new is needed here:

```bash
container-attach pragmata-eval-train
cd /workspace
make eval-predict TASK=retrieval POPULATION=annotated RUN_ID=<id> PY=$HOME/train-venv/bin/python
```

`eval-predict` and `eval-evaluator-report PART=calibration` refuse to start on an interpreter
that cannot see a GPU, naming the two ways of getting that wrong (the host's venv inside the
container; site policy on the host outside one). `--use-cpu` is the deliberate escape hatch for
a smoke test.

**Batch size.** `tlmtc`'s default is 32, which is fine for retrieval and generation.
Grounding runs at `sequence_length=6144` and wants `BATCH_SIZE=4`; the same memory ceiling that
forced `batch_size=1` for its *training* applies here, less severely.

## The two populations

They answer different questions, and the difference is the whole point of having both.

**`annotated`** is the frozen canonical export with the labels stripped - the same rows the
human-label metrics in `eval_metric_estimates.csv` describe. Pooled exactly as
`eval-train-inputs` pools them (programmes derived from the tree, submitted responses only,
`source_domain` written rather than trusted), so each synthetic metric can be read straight
beside its human counterpart. Two things differ from the training staging, both forced:

- **Every label column is dropped.** pragmata's prediction contract does not merely ignore
  them, it *rejects* them - `validate_eval_predict_frame` refuses the task's label columns and
  anything named `label_*`. That is the right call: an input carrying the answers invites
  scoring a model against its own input.
- **Rows are reduced to one per item.** The export carries one row per annotator, and with the
  labels gone those are exact duplicates of the same text. The grain is `eval_common.ITEM_KEYS`,
  which is what pragmata consolidates to anyway, so the population is unchanged - only the
  redundancy is. It lands on 1,561 retrieval / 447 grounding / 713 generation items, the same
  counts `make eval-train-seqlen` reports.

**`corpus`** is the curated corpus - `data/publikationsbot/*_combined.curated.jsonl`, a
superset of what was ever annotated. Its text columns are built the way pragmata builds the
Argilla fields at annotation import, read out of its own `record_builder`: retrieval pairs the
query with each *chunk's* text, grounding pairs the answer with `context_set`, generation pairs
the query with the answer. Note that `context_set` is a **field of the import record**, carried
through verbatim - pragmata does not assemble it out of the chunk texts.

`record_uuid` comes from pragmata's own `derive_record_uuid`, so a corpus row and the export
row for the same pair carry the *same* identity. That is what makes the two populations
comparable, and it is also why corpus retrieval panels are complete by construction: every
chunk of a pair is staged, so `--skip-incomplete-panels` has nothing to drop.

The population is "every curated record that satisfies pragmata's import contract", which is
the same set that could have been annotated - a record the contract rejects (a query whose
retrieval returned no chunks, say) could never have reached Argilla either. Rejects are counted
and recorded in the sidecar rather than silently dropped, and a file with nothing but rejects is
fatal.

A third population, `testsplit`, exists but is not staged by `eval-predict-inputs`:
`evaluator_report.py calibration` stages one per run from that run's own held-out split, which
is a property of a training run rather than of the corpus. `make eval-predict
POPULATION=testsplit` accepts it, so that pass can be repeated by hand, but `RUN_ID` must then
name the run the split was staged from: a split belongs to the run that held it out, and
predicting it with a different evaluator would fill that evaluator's directory with another
run's calibration data.

## Provenance and the freshness rule

Two records, one per side of the staging boundary, and the second is checked rather than just
written - the same discipline [training](eval-training.md#provenance) applies.

`data/eval-inputs/predict/<population>/<task>.csv` ships a `.provenance.json` naming its
inputs, the population, the grain, the labels it dropped, and `output_sha256` - the CSV's own
bytes. **`eval-predict` refuses to start unless that record is present, names the population
being asked for, and matches the CSV on disk.** For the `annotated` population it must also
name the freeze `configs/eval/freeze.conf` currently pins: without that check the pin can move
while a stale CSV is predicted on silently, since nothing else downstream re-reads where the
staged rows came from. For `testsplit` it must name the evaluator run being applied, which is
the check the paragraph above describes.

The corpus population has no freeze to check, so its record carries the equivalent: each source
JSONL's `sha256` beside the pin for it in
[`reproducibility/2026-07-01-annotation-curation/pins.sha256`](../reproducibility/2026-07-01-annotation-curation/pins.sha256),
and the comparison outcome. That outcome is recorded **either way** - a match means the
predicted population is the same corpus the annotations were drawn from, a mismatch means it is
not, and both are facts the numbers rest on. `pin_sha256: null` is a third case: the bundle
names no pin for that file at all.

Each completed prediction then writes `predict_provenance.workspace.json` into its own output
directory, so the record travels with the predictions when they are pushed off the box. It names
the population, the evaluator run and whether that run was given or resolved, the input CSV with
its `sha256` and the relevant half of its sidecar, the resolved `predict_kwargs`, and the
evaluator's `label_names`. The `.workspace.` infix marks it as this repository's file in a tree
pragmata otherwise owns.

## The output layout, and why it is not pragmata's

**`tlmtc` names its prediction directory after the evaluator run id, and overwrites.**
`resolve_prediction_paths` builds `prediction_outputs/<run_id>/`, and `predict_tlmtc` writes
`probabilities.csv` and `predictions.csv` into it through `mkdir(exist_ok=True)` and a plain
`to_csv`. There is no guard of any kind - no refusal, no versioning. Predicting a second
population with the same evaluator overwrites the first silently, and pragmata's
`pragmata_predict.meta.json` is rewritten to match, so afterwards nothing on disk says which
population the numbers describe. With three populations per evaluator that is a certainty, not
a risk.

So `eval-predict` **moves** a completed run's output tree to
`data/eval/prediction_outputs/<run_id>-<population>/`. The constraint that scheme had to
satisfy is that the result still scores, and it does: `eval score --prediction-id X` resolves
`prediction_outputs/X/pragmata_predict.meta.json` by directory name alone
(`resolve_eval_predict_meta_path`), validates the `task` recorded inside it, and scores the
`predictions.csv` beside it. It never compares the meta's own `run_id` field against the
directory name, so that field keeps naming the *evaluator* - the more useful of the two things
it could say, and what the workspace record beside it cross-references.

The staging directory is cleaned *before* a run rather than after, and the run says so: a
leftover `prediction_outputs/<run_id>/` can only be an interrupted run, because a completed one
always moves. Re-predicting the same (evaluator, population) is refused unless `--overwrite` is
passed.

## Run order

```bash
# CPU box, or the GPU box once the export tree is local
make eval-predict-inputs POPULATION=annotated
make eval-predict-inputs POPULATION=corpus        # needs the curated corpus, see below

# GPU container, one call per (task, population)
export PY=$HOME/train-venv/bin/python
make eval-predict TASK=retrieval  POPULATION=annotated RUN_ID=<retrieval-run>  PY=$PY
make eval-predict TASK=grounding  POPULATION=annotated RUN_ID=<grounding-run>  PY=$PY BATCH_SIZE=4
make eval-predict TASK=generation POPULATION=annotated RUN_ID=<generation-run> PY=$PY
# ...and the same three with POPULATION=corpus

make eval-evaluator-report PART=calibration PY=$PY    # re-predicts each run's own test split

# Either box
make eval-evaluator-report                             # evaluator_metrics.csv
make eval-score-synthetic POPULATION=annotated
make eval-score-synthetic POPULATION=corpus
```

`RUN_ID` is optional and should not be treated as such. Omitted, the script resolves the latest
evaluator for the task, prints that it did, and records it - which is right for a scratch run
and wrong for a published one, because "latest" is a property of the box rather than of the
numbers. Pass it for anything whose output leaves the machine.

## Getting the data in and out

The transport is the existing [Blob pipe](eval-data-transport.md); prediction adds no new
prefix, it uses the `predictions/` one that was reserved for it.

**The corpus population needs the curated corpus, which is produced on the CPU box.** It is not
part of the frozen export, so it travels separately:

```bash
# CPU box
make transfer-push SRC=data/publikationsbot PREFIX=publikationsbot
# GPU box
make transfer-pull PREFIX=publikationsbot     # -> data/transfer/publikationsbot/  (+verify)
```

`eval-predict-inputs POPULATION=corpus` looks in `data/publikationsbot/` first and falls back to
`data/transfer/publikationsbot/`, saying which it settled on - the same two-place rule
`eval-train-inputs` applies to the export tree. `CORPUS_DIR=` overrides both, and an explicitly
named directory is never silently substituted.

**Push the predictions off the box before it is torn down**, beside the checkpoints:

```bash
make transfer-push SRC=data/eval/prediction_outputs PREFIX=predictions
```

On the receiving box a `pull` lands at `data/transfer/predictions/`, because `sync.sh` refuses
any destination that would escape `data/transfer/`. **Scoring needs the tree at
`data/eval/prediction_outputs/` instead** - that path is not this workspace's choice, it is
where pragmata resolves `--prediction-id` - so move it in after verifying:

```bash
make transfer-pull PREFIX=predictions
mkdir -p data/eval/prediction_outputs
cp -a data/transfer/predictions/. data/eval/prediction_outputs/
```

The same applies to `checkpoints/` and `data/eval/train_outputs/`, which
`eval-evaluator-report` reads. Copying pragmata's own output into pragmata's own tool tree does
not break the [ownership rule](eval.md#ownership) - these files *were* written by pragmata, on
the other box - but the manual step is a rough edge rather than a design, and automating it is
not done.

## Scoring the predictions

`make eval-score-synthetic` is the twin of `make eval-score`: it calls the same
`pragmata eval score` CLI from the same eval pin, through the same `PYTHONPATH` shadow, and
builds its rows with the same code the human scorer uses - so
`synthetic_metric_estimates.<population>.csv` is column-for-column comparable with
`eval_metric_estimates.csv`, minus the four `alpha_*` columns and plus `evaluator_run_id`,
`prediction_id` and `population`.

The `alpha_*` columns are absent by definition rather than by omission: a prediction has one
label per item and no annotator disagreement to measure. What takes their place is not in this
file at all - it is the evaluator's own quality, in `evaluator_metrics.csv`. **Read the two
together.** A corpus rate produced by a model whose AUC is near chance is not a measurement.

`--prediction-id` is discovered rather than parsed out of directory names: every prediction
directory carries a `predict_provenance.workspace.json` naming its task, population and
evaluator, and discovery reads that. Two evaluators predicting the same (task, population) is
refused rather than resolved by recency - it names both and asks which.

### Grounding cannot be scored this way

Grounding trains on three of its five labels (`support_present` and `source_cited` have too few
negative items for any split; see [Eval training](eval-training.md#what-each-tasks-configuration-rests-on)),
so its `predictions.csv` carries three label columns. pragmata's `GROUNDING_SCORE_SCHEMA`
requires all five, and it is built from `LABEL_COLUMNS_BY_TASK` at module *import* time, so the
narrowing that makes training possible cannot reach it.

The scorer therefore checks the prediction's header up front and writes explicit `n = 0` rows
with `status = evaluator_labels_incomplete` for every grounding metric, rather than letting the
CLI reject the frame with a schema error that reads like a staging bug. The alternative -
inventing the two missing columns - would be fabricating labels. What would fix it is more
negative grounding annotation, not more pipeline.

### What the two populations' numbers mean

- **`annotated`** is directly comparable with the human numbers, and is **largely in-sample**.
  Each evaluator's train and validation splits are roughly three quarters of exactly these
  items (retrieval 1,152 of 1,561; grounding 335 of 447; generation 530 of 713), so a synthetic
  estimate here is optimistic about the evaluator by an unknown amount. It is the right population for
  "does the evaluator reproduce the human metric", not for "how good is the evaluator" - that
  is `evaluator_metrics.csv`, which is computed on held-out rows only.
- **`corpus`** is corpus scale with no human baseline at all. Its retrieval panels are complete
  by construction, so unlike the annotated population it drops nothing: the annotated retrieval
  run scores 181 of 464 panels (`n_panels_skipped = 283`), because annotation coverage is
  partial, while a corpus run scores every panel. For **grounding and generation the
  corpus-scale numbers carry the evaluator-quality caveat in full**: generation's evaluator
  shows majority-class collapse on its two highest-prevalence labels (F1 0.93 against an AUC of
  0.53), and grounding's is directional at best on two of its three trained labels. Treat those
  corpus rates as an indication of what a better evaluator would be measuring, not as a
  measurement.

## The evaluator report CSVs

`evaluator_metrics.csv` and `evaluator_calibration.csv` describe the **models**, on each run's
own held-out test split - the split `tlmtc` cut and reported on, so the same population the
training run's own metrics describe. Both are small: 409 / 112 / 183 test rows for retrieval /
grounding / generation.

`metrics` reads only what a completed run left on disk (`evaluation/label_metrics.json` and
`data/test.parquet`), so it needs neither a GPU nor pragmata. **Accuracy is not persisted by
`tlmtc`**, so it is derived from the metrics that are - the derivation, and the check that keeps
it honest, are in the [data dictionary](eval-data-dictionary.md#evaluator_metricscsv) and in
`_derived_accuracy`.

`calibration` needs per-item probabilities, which no training artifact holds. So it re-applies
each run to its own test split *through the same prediction plumbing everything else uses* -
staged input, freshness sidecar, population-named output directory, with `testsplit` as the
population. That reuse is the point: the probabilities behind the CSV are produced the way the
published predictions are, not by a private code path that could drift from them. The
probabilities are joined back to the held-out labels on a row-index column carried through
prediction, not zipped by position, and the join is checked to cover every test row exactly
once.

## What is not done

- The transfer-pull-then-move step above is manual, for both `predictions/` and `checkpoints/`.
- Nothing compares the two `synthetic_metric_estimates.*.csv` files against
  `eval_metric_estimates.csv` automatically; the comparison is the report author's, in the
  private report repository.
- Corpus prevalence for grounding is not produced by any route. The pragmata scoring path
  refuses it for the reason above, and computing it directly from `predictions.csv` was
  deliberately not done: it would produce a number the score CLI would not, from a code path
  nothing else uses, for three of the five labels the metric taxonomy asks about.
