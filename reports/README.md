# reports/ (gitignored)

Rendered reporting snapshots. Gitignored (regenerable from `logs/`); this README and
`.gitkeep` mark the expected structure.

```
reports/
└── annotation/
    ├── <date>/          report.md + plots for that run
    └── _latest -> <date>  symlink to the most recent
```

Written by `make report` (`report_tables.py` + `plot_summary.py`) from
`logs/annotation/log.jsonl`.
