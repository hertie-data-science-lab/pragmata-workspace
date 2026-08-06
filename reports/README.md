# reports/ (gitignored)

Rendered reporting snapshots. Gitignored (regenerable from `logs/` and the pinned eval inputs);
this README and `.gitkeep` mark the expected structure.

```
reports/
├── annotation/
│   ├── <date>/          report.md + plots for that run
│   └── _latest -> <date>  symlink to the most recent
└── eval/
    └── <date>/          one deliverable set, in its three subsets
        ├── data-dictionary.md      one copy for the set; every .provenance.json pins it
        ├── human-annotation/       annotation_label_summary, annotation_operations,
        │                           eval_metric_estimates
        ├── fairness-audit/         retrieval_manifest, corpus_catalog
        └── synthetic-evaluator/    evaluator_metrics, evaluator_calibration,
                                    synthetic_metric_estimates.<population>
```

`annotation/` is written by `make annotation-report` (`report_tables.py` + `plot_summary.py`)
from `logs/annotation/log.jsonl`. `eval/` is written by the `eval-*` deliverable targets, each
CSV beside its `.provenance.json`; the subsets and what the numbers mean are in
[Report deliverables](../docs/report-deliverables.md). Anything hand-written for a run - an
executive summary, a note - belongs at the dated run root beside the dictionary.

Three things are called reports; which is which is in the [top-level README](../README.md).
