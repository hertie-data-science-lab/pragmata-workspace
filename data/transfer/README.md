# data/transfer/ (gitignored contents)

Staging landing zone for eval data moved over Azure Blob by
[`scripts/transfer/sync.sh`](../../scripts/transfer/sync.sh). Everything here except this
README and `.gitkeep` is gitignored - it moves via **Blob, not git** (git carries
code; this carries data).

```
data/transfer/
├── exports/                annotation exports pulled on the GPU box (input to eval)
├── exports-frozen/<date>/  the pinned export tree behind published numbers
├── publikationsbot/        the curated corpus the `all-items` prediction population is staged from
├── predictions/            per-row model predictions pulled back on the CPU box
└── checkpoints/            trained evaluator checkpoints pulled off the GPU before teardown
```

The prefix list is not fixed - every push names its own, and `reports/eval/<date>/` travels the
same way. Which prefix goes which direction, and why, is in
[Data transport](../../docs/data-transport.md#the-subtrees).

Each subtree arrives with a `MANIFEST.sha256` and is verified (`sha256sum -c`) on
download; `make transfer-verify PREFIX=<sub>` re-checks it locally any time.

**Ownership seam.** `sync.sh` **reads** pragmata's own tool trees
(`data/annotation/`, `data/eval/`) in place and **writes only here** - never into
a tool's output tree. Received data is always under `data/transfer/`, so it's
unambiguous which files a tool produced versus which sync dropped, and a tool
resetting its own dir can't nuke received data.

**How eval reaches it.** For the export and corpus prefixes it **falls back** here on its own:
the staging scripts look in the tool tree first, use the `data/transfer/` copy when the default
is absent, and say which they settled on. `predictions/` and `checkpoints/` have to be **copied
across** into `data/eval/` after verifying, because that is where pragmata resolves runs and
`--prediction-id` - see
[Getting the data in and out](../../docs/synthetic-evaluators.md#getting-the-data-in-and-out).
