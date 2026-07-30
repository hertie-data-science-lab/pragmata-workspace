# Eval pipeline

The evaluation pipeline is a sibling of the [annotation pipeline](annotation.md). Two parts
of it are running today, and one is not built.

**Data transport** has shipped: `scripts/transfer/sync.sh` plus the `make transfer-push` /
`transfer-pull` / `transfer-verify` targets move eval data between the CPU annotation box and
the GPU eval box over Azure Blob - see [Eval data transport](eval-data-transport.md).

**Scoring human labels** has shipped. Three targets produce the report deliverables into
`reports/eval/<date>/`, each CSV with a provenance sidecar and a copy of the
[data dictionary](eval-data-dictionary.md):

```
make eval-report     # annotation_operations.csv, annotation_label_summary.csv, retrieval_manifest.csv
make eval-score      # eval_metric_estimates.csv, via `pragmata eval score`
make eval-catalog    # corpus_catalog.csv, from the publikationsbot vector store
```

They read the frozen canonical export and the log snapshot pinned in
`scripts/eval/eval_common.py`, never the live tree the nightly cron overwrites - see the
[freeze bundle](../reproducibility/2026-07-30-eval-report/). Eval uses its own pragmata pin
(`PRAGMATA_EVAL_SRC`), separate from the annotation pipeline's frozen demo pin, so the live
instance's export behaviour stays fixed while eval tracks upstream.

**Training and prediction** (`pragmata eval train|predict`) are not built yet. When they are,
they will mirror the annotation pipeline (`scripts/eval/` ↔ `scripts/annotation/`,
`configs/eval/` ↔ `configs/annotation/`) and build on pragmata's `eval` tool (the `tlmtc`
extra), which writes artifacts to `data/eval/` alongside `data/annotation/` and
`data/querygen/`. `scripts/eval/score_human_annotations.py` names the half of the scoring
that exists; its twin `score_synthetic_predictions.py` is reserved for the evaluator-model
run.
