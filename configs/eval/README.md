Evaluation-stage configs. Mirrors `configs/annotation/`; otherwise still a stub — the
train/predict glue has none yet (see `docs/eval.md`).

- `freeze.conf` — the canonical freeze pin every report number rests on, written by
  `make annotation-freeze` and read by `scripts/eval/eval_common.py`.
