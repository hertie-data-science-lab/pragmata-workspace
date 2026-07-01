# Eval pipeline

*Planned — not yet implemented.* The evaluation pipeline is a reserved sibling of the
[annotation pipeline](annotation.md). Today only the scaffolding exists: placeholder READMEs
under `scripts/eval/` and `configs/eval/`, and the reserved `data/eval/`, `logs/eval/`,
`reports/eval/` layout (the `stage("eval")` seam in `scripts/lib/workspace.py`).

When built it will **mirror the annotation pipeline** (`scripts/eval/` ↔
`scripts/annotation/`, `configs/eval/` ↔ `configs/annotation/`) and build on pragmata's
`eval` tool (the `tlmtc` extra), which writes artifacts to `data/eval/` alongside
`data/annotation/` and `data/querygen/`.

There are currently no runnable eval stages, configs, or `make` targets.
