Evaluation-stage configs. Mirrors `configs/annotation/`, and holds one file.

- `freeze.conf` — the canonical freeze pin every report number rests on, written by
  `make annotation-freeze` and read by `scripts/eval/eval_common.py`.

Nothing here configures training or prediction, and for training that is deliberate: its
per-task parameters are pins behind published numbers rather than operator knobs, so they live
in `scripts/eval/train_evaluators.py` where a change shows up in a diff. See
`docs/eval-training.md`. Prediction has no workspace glue at all yet (`docs/eval.md`).
