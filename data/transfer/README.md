# data/transfer/ (gitignored contents)

Staging landing zone for eval data moved over Azure Blob by
[`scripts/eval/sync.sh`](../../scripts/eval/sync.sh). Everything here except this
README and `.gitkeep` is gitignored — it moves via **Blob, not git** (git carries
code; this carries data).

```
data/transfer/
├── exports/        annotation exports pulled on the GPU box (input to eval)
├── predictions/    per-row model predictions pulled back on the CPU box
└── checkpoints/    trained evaluator checkpoints pulled off the GPU before teardown
```

Each subtree arrives with a `MANIFEST.sha256` and is verified (`sha256sum -c`) on
download; `make eval-verify PREFIX=<sub>` re-checks it locally any time.

**Ownership seam.** `sync.sh` **reads** pragmata's own tool trees
(`data/annotation/`, `data/eval/`) in place and **writes only here** — never into
a tool's output tree. Received data is always under `data/transfer/`, so it's
unambiguous which files a tool produced versus which sync dropped, and a tool
resetting its own dir can't nuke received data. Eval consumes what lands here by
**explicit path** (e.g. `--labeled-data-path data/transfer/exports/<topic>/<task>.csv`).
