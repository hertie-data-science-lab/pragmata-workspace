# data/ (gitignored)

Pipeline inputs and outputs. **Everything here except this README and the
`.gitkeep` markers is gitignored** - it's large and some of it carries PII. 
This file documents the expected structure so a fresh clone knows what 
belongs where and how to obtain it.

```
data/
├── querygen/            LLM querygen cache + run dirs (non-deterministic; regenerable)
├── publikationsbot/     source query corpora (<slug>_combined.jsonl) + querygen intermediates
└── annotation/
    ├── imports/         per-scope partition manifests (partition.meta.json, keyed by record_uuid)
    └── exports/         annotation outputs, per-task CSVs  ← PII (annotator_id); never commit
         LLM querygen cache + run dirs (non-deterministic; regenerable)
```

## How to populate

- **Reproduce the curated annotation experiment** (recommended): see
  [`reproducibility/2026-07-01-annotation-curation/`](../reproducibility/2026-07-01-annotation-curation/).
  Fetch the pinned corpus/backup artifacts (checksums in that bundle), then
  `make reproduce-curation`.
- **Regenerate from scratch**: run the pipeline (`make pipeline`) - querygen →
  bot → combine → setup → import. Note querygen is non-deterministic LLM output.

The curated corpus (`*_combined.curated.jsonl`, ~52M) and the Argilla backups
(~2.1G) are too large for git; they live as external release/archive artifacts
pinned by SHA256 in the reproducibility bundle.
