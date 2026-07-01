# logs/ (gitignored)

Run logs and metrics history. Gitignored (machine-local run state); this README and
`.gitkeep` mark the expected structure.

```
logs/
└── annotation/
    └── log.jsonl        append-only annotation snapshots (counts, calibration α, cadence)
```

Written by `make log` / `scripts/annotation/log.py` (nightly via `scripts/daily.sh`).
Rendered into `reports/` by `make report`.
