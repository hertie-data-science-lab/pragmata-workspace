# configs/eval/

Evaluation-stage configs. Mirrors `configs/annotation/`.

- `freeze.conf` — the canonical freeze pin every report number rests on, written by
  `make annotation-freeze` and read by `scripts/eval/eval_common.py`.
- `training/` — one YAML per task for `make eval-train`, plus a shared `_common.yaml`
  deep-merged underneath it, exactly as `annotation/querygen_specs/_runtime.yaml` composes
  with each spec. See [synthetic evaluators](../../docs/synthetic-evaluators.md).

**How much of this is validated, exactly.** The merged mapping is passed to
`pragmata.api.eval.train_evaluator` as keyword arguments, so the two levels behave differently:

- **Top-level keys** (`checkpoint`, `sequence_length`, `scale_learning_rate`, ...) are that
  function's own parameters. A typo fails loudly, but as a `TypeError` on an unexpected keyword
  argument rather than as a `pydantic` validation error - `EvalTrainSettings` never sees the
  stray key, because the call site builds its override dict from the fixed signature. Values
  that reach the model are type-checked by it.
- **`train_kwargs` is an unvalidated passthrough** - `dict[str, Any]`, forwarded verbatim to
  `tlmtc.train_tlmtc`. `pragmata` checks only that it does not shadow an argument it manages
  itself (`checkpoint`, `sequence_length`, and the rest). Nothing here knows which keys `tlmtc`
  reads, so a key it ignores is accepted in silence and the run reports no error.

That silence is why `make eval-train` echoes the whole merged config to stderr at run start and
records it in `train_provenance.workspace.json` inside the run directory: a setting that did
nothing is at least visible beside the metrics it did not affect.

The values in `training/` are **pins behind published numbers, not operator knobs**: every
metric in the eval report was produced at them, so changing one invalidates comparison and
means re-running all three tasks. Each is documented beside itself, including the levers that
were tested and did not help.

Two things deliberately stay out of these files: `base_dir` and `labeled_data_path`, which are
machine-dependent and passed as overrides, and grounding's label narrowing, which reassigns a
`pragmata` module mapping and has no config field to live in. The narrowing reaches the training
targets only - the two dropped columns are still required in the staged CSV, because the input
schema was built from that mapping at import time. See `grounding.yaml`'s own header.

**Prediction has workspace glue but deliberately no configs here.** Its two choices - which
population to predict, and which evaluator run to apply - are CLI arguments
(`make eval-predict TASK= POPULATION= RUN_ID=`), not committed data, because a prediction is a
*use* of a pinned model rather than a new pin: until the final run there are no published numbers
for a file here to stand behind. Adding one would freeze a choice nobody has made yet. What each
run actually used is recorded per run instead, in `predict_provenance.workspace.json` inside the
prediction directory - population, evaluator run id and whether it was given or resolved, the
staged input CSV with its sha256, and the freeze date or curation-pin outcomes behind it.
See [synthetic evaluators](../../docs/synthetic-evaluators.md).
