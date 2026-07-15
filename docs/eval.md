# Eval pipeline

The evaluation pipeline is a sibling of the [annotation pipeline](annotation.md). Its **data
transport** has shipped: `scripts/eval/sync.sh` plus the `make eval-push` / `eval-pull` /
`eval-verify` targets move eval data between the CPU annotation box and the GPU eval box over
Azure Blob - see [Eval data transport](eval-data-transport.md). The **stages themselves**
(`pragmata eval train|predict|score`) are not built yet.

When built the stages will **mirror the annotation pipeline** (`scripts/eval/` ↔
`scripts/annotation/`, `configs/eval/` ↔ `configs/annotation/`) and build on pragmata's
`eval` tool (the `tlmtc` extra), which writes artifacts to `data/eval/` alongside
`data/annotation/` and `data/querygen/`. The reserved `data/eval/`, `logs/eval/`,
`reports/eval/` layout and the `stage("eval")` seam in `scripts/lib/workspace.py` are already
in place.

There are no runnable eval **stages** or configs yet; the only runnable eval `make` targets
are the transport ones above.
