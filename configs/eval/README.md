Evaluation-stage configs. Mirrors `configs/annotation/`.

- `freeze.conf` — the canonical freeze pin every report number rests on, written by
  `make annotation-freeze` and read by `scripts/eval/eval_common.py`.
- `training/` — one YAML per task for `make eval-train`, plus a shared `_common.yaml`
  deep-merged underneath it, exactly as `annotation/querygen_specs/_runtime.yaml` composes
  with each spec. The keys are `pragmata`'s own `EvalTrainSettings` fields, so the files are
  validated by it directly (and `extra="forbid"`, so a typo fails loudly). See
  [eval training](../../docs/eval-training.md).

The values in `training/` are **pins behind published numbers, not operator knobs**: every
metric in the eval report was produced at them, so changing one invalidates comparison and
means re-running all three tasks. Each is documented beside itself, including the levers that
were tested and did not help.

Two things deliberately stay out of these files: `base_dir` and `labeled_data_path`, which are
machine-dependent and passed as overrides, and grounding's label narrowing, which reassigns a
`pragmata` module mapping and has no config field to live in.

Prediction has no workspace glue at all yet, so nothing here configures it (`docs/eval.md`).
