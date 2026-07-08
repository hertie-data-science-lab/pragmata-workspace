# Evaluation-stage scripts

Pragmata's `eval` tool fine-tunes an evaluator (via `tlmtc`) and must run on a
**GPU box on the Hertie network**, while the annotation exports it consumes live
on this **CPU box in BSt's Azure tenant**. The two VMs are in different
organisations with no route to each other, so eval data moves over a shared
**Azure Blob** container both sides reach over HTTPS.

Pragmata writes eval artifacts to `data/eval/` via `tool_root('eval')`, a sibling
of `data/annotation/` and `data/querygen/`. Configs live in `configs/eval/`.

## Two planes

```
   CONTROL PLANE  git → GitHub (code only, same SHA both sides)
   CPU VM (BSt) ◄──── clone ────► GPU VM (Hertie)
        │                              │
        │   az blob upload/download    │
        ▼          (HTTPS 443)         ▼
     ┌────────────────────────────────────┐
     │  Azure Blob container (BSt-owned)   │
     │  exports/ (CPU→GPU)                 │
     │  predictions/, checkpoints/ (→CPU)  │
     └────────────────────────────────────┘
```

Neither VM reaches the other; both reach Blob. Direct box-to-box is blocked
structurally (BSt Azure VNet ↔ Hertie-internal `10.x`, no peering — confirmed by
a timed-out `nc 10.1.23.20:22`).

## `sync.sh` — the pipe

```
sync.sh push <src> <prefix>    # CPU→Blob: upload a tree + a sha256 manifest
sync.sh pull <prefix> [dest]   # Blob→box: download into data/transfer/<dest>, then verify
```

Every `push` writes a sorted per-file `sha256` manifest to `<prefix>/MANIFEST.sha256`
and prints a one-line **snapshot pin** (a single hash for the whole tree) for a
future reproducibility bundle. Every `pull` re-runs `sha256sum -c` on the
receiving end and fails loudly on any mismatch.

Driven from the `Makefile`:

```
make eval-push                       # whole exports tree → blob exports/ (default)
make eval-pull PREFIX=predictions    # blob predictions/ → data/transfer/predictions/
make eval-verify PREFIX=exports      # download the manifest and check it, no unpack
```

## Ownership invariant (staging seam)

`sync.sh` **reads** pragmata tool trees (`data/annotation/`, `data/eval/`) in
place and **writes only** to `data/transfer/` on the receiving box — never inside
a tool's own output tree. `pull` refuses any destination outside `data/transfer/`.
This keeps "did pragmata produce this, or did sync drop it here?" unambiguous, and
means a tool resetting its own dir can't clobber received data.

Eval then consumes staged input by **explicit path** — its `labeled_data_path` /
`unlabeled_data_path` are explicit by design ("not inferred from prior tool
outputs"), e.g.
`pragmata eval train --labeled-data-path data/transfer/exports/<topic>/<task>.csv`.
See `data/transfer/README.md`.

## Data sensitivity

Exports carry `annotator_id` — a **pseudonymous, name-derived handle**, not a
name or email (`data/README.md` labels the exports PII, "never commit"). They ship
as-is into a **private, IP-allowlisted** container; the roster
(`configs/annotation/users.json`) is gitignored and the GPU box never needs it —
eval consumes label columns, not identities.

## GPU box = cattle, except checkpoints

Any eval run is replayable from pinned inputs + pinned code, so the GPU box is
disposable — **except trained checkpoints**, which are expensive to reproduce and
must be `pull`ed off and pinned to a durable home before the box is torn down.

## Not here yet

The **eval pipeline itself** (`pragmata eval train|predict|score`) is unbuilt — a
separate effort in the pragmata repo. The **eval-run provenance bundle** and a
`pragmata eval` wrapper (this dir) land once that CLI exists and emits artifacts
with a stable schema to pin. This dir currently ships only the transport.
