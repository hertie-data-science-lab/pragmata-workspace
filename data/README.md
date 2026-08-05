# data/ (gitignored)

Pipeline inputs and outputs. **Everything here except this README and the
`.gitkeep` markers is gitignored** - it's large and some of it carries PII. 
This file documents the expected structure so a fresh clone knows what 
belongs where and how to obtain it.

```
data/
├── querygen/            LLM querygen cache + run dirs (non-deterministic; regenerable)
├── publikationsbot/     source query corpora (<slug>_combined.jsonl) + querygen intermediates
├── annotation/
│   ├── imports/         per-scope partition manifests (partition.meta.json, keyed by record_uuid)
│   ├── exports/         annotation outputs, per-task CSVs  ← PII (free-text notes); never commit
│   │                    annotator_id is pseudonymised on export; see docs/eval.md
│   │                    one dir per domain config and nothing else: this tree is published
│   │                    by transfer-push and consumers read exports/*/ as the domain list
│   └── exports-frozen/  read-only (chmod a-w) freezes, one dir per <date>: the pinned
│                        inputs behind published numbers; see docs/eval.md
├── eval/                pragmata eval tool outputs; only what pragmata wrote, plus the two
│   │                    *.workspace.json provenance records named in docs/eval.md
│   ├── train_outputs/       one dir per evaluator training run, named <run_id>
│   ├── prediction_outputs/  one dir per prediction, named <run_id>-<population>: tlmtc names
│   │                        it <run_id> and overwrites, see docs/eval-prediction.md
│   └── scores/              one dir per score run, named <score_id>
├── eval-inputs/         workspace-staged CSVs handed to the eval tool; kept out of eval/ so
│   │                    that tree holds only what pragmata wrote. Each ships a
│   │                    .provenance.json the consumer checks before running
│   ├── <policy>/            filtered CSVs for `eval score --path` (calib-complete, ...)
│   ├── training/            pooled labelled CSVs for `eval train-evaluator`
│   └── predict/<population>/ unlabelled CSVs for `eval predict-labels`
└── transfer/            Blob staging for eval data (see transfer/README.md); moves via Blob, not git
```

## How to populate

- **Reproduce the curated annotation experiment** (recommended): see
  [`reproducibility/2026-07-01-annotation-curation/`](../reproducibility/2026-07-01-annotation-curation/).
  Fetch the pinned corpus/backup artifacts (`pins.sha256` in that bundle), then
  `make repro-reproduce PIN=2026-07-01-annotation-curation`.
- **Regenerate from scratch**: run the pipeline (`make pipeline`) - querygen-run →
  bot-run → combine-run → annotation-setup → annotation-import. Note querygen is
  non-deterministic LLM output.

The curated corpus (`*_combined.curated.jsonl`, ~52M) and the Argilla backups
(a few hundred MB under the retention policy in
[`reproducibility/README.md`](../reproducibility/README.md)) are too large for git;
they live as external release/archive artifacts pinned by SHA256 in the
reproducibility bundles.
