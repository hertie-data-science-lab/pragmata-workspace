#!/bin/bash
# scripts/annotation/freeze.sh <date> <run_at>
#
# Cuts the canonical freeze: an immutable dated copy of data/annotation/exports/, plus the
# pin (configs/eval/freeze.conf) that points every eval report at it and at the one log
# snapshot taken beside it. Run as `make annotation-freeze DATE=<date> RUN_AT=<run_at>`.
#
# Why this is a target of its own rather than a step of annotation-export: daily.sh's 02:00
# cron re-runs the export every night, so freezing on export would mint a write-protected
# dated copy per night. A freeze asserts "these bytes back a published number" — an
# editorial decision, not a cron side effect.
#
# It stops one step short of done. The pin it writes only becomes canonical once COMMITTED,
# and a script must not make that commit: docs/eval.md step 1 forbids rewriting history
# after provenance files name a commit, so the operator owns it. The follow-ups — that
# commit, the regeneration, and the bundle re-pin — are printed at the end.
#
# Everything before the copy is a guard, and every guard is here because the mistake it
# catches is expensive: a freeze is immutable, published, and cited by report numbers. See
# docs/eval.md ("Cutting a new freeze") for the surrounding procedure.

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
cd_root

DATE="${1:-}"
RUN_AT="${2:-}"
[[ -n "$DATE" && -n "$RUN_AT" ]] \
  || fatal "usage: make annotation-freeze DATE=<YYYY-MM-DD> RUN_AT=<snapshot run_at>"

SRC="$DATA_DIR/annotation/exports"
FROZEN_ROOT="$DATA_DIR/annotation/exports-frozen"
DEST="$FROZEN_ROOT/$DATE"
PIN="configs/eval/freeze.conf"

# --- guards: all of them before anything is written ---

# The date names the freeze directory, and eval_common.py picks the newest freeze by
# sorting those names — which only orders them if they are all shaped the same way.
[[ "$DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || fatal "DATE must be YYYY-MM-DD: $DATE"

# A freeze cut from a dirty tree has no citable lineage of its own: every .provenance.json
# records the workspace commit it was generated at (docs/eval.md step 1).
[[ -z "$(git status --porcelain)" ]] \
  || fatal "working tree is dirty — commit or stash first, so the freeze can cite a commit"

# Never overwrite a published freeze: something already cites those bytes.
[[ ! -e "$DEST" ]] \
  || fatal "already frozen: ${DEST#"$WORKSPACE_ROOT"/} — pick another DATE"

[[ -d "$SRC" ]] || fatal "no export tree at ${SRC#"$WORKSPACE_ROOT"/} — run 'make annotation-export'"
shopt -s nullglob
programmes=("$SRC"/*/)
shopt -u nullglob
(( ${#programmes[@]} > 0 )) \
  || fatal "${SRC#"$WORKSPACE_ROOT"/} holds no programme dirs — nothing to freeze"

# A mistyped snapshot pin is otherwise caught only at report time, long after the freeze is
# published. find_snapshot is the lookup the reports themselves use, so this accepts exactly
# what they will accept — the schema-version check included.
"$PY" - "$RUN_AT" <<'PYEOF' || fatal "RUN_AT does not name a snapshot in logs/annotation/log.jsonl: $RUN_AT"
import sys
from pathlib import Path

sys.path.insert(0, str(Path("scripts/lib").resolve()))
import workspace as ws

ws.find_snapshot(sys.argv[1])
PYEOF

# Before the copy, not after: transfer-push runs this same check, but by then the names
# would already sit in a write-protected tree that a published number cites.
python3 "$WORKSPACE_ROOT/scripts/lib/check_pseudonymised.py" "$SRC" \
  || fatal "refusing to freeze ${SRC#"$WORKSPACE_ROOT"/}: non-pseudonymised annotator identities — re-run 'make annotation-export'"

# --- the copy: write-protected, and the parent left as we found it ---

section "freeze: annotation/exports -> annotation/exports-frozen/$DATE"
mkdir -p "$FROZEN_ROOT"
chmod u+w "$FROZEN_ROOT" || fatal "cannot unlock ${FROZEN_ROOT#"$WORKSPACE_ROOT"/} to write into it"
cp -r "$SRC" "$DEST" || { chmod a-w "$FROZEN_ROOT"; fatal "copy failed: $SRC -> $DEST"; }
# The new tree first, then the parent: the parent's own write bit is what stops a stray cp
# from landing another freeze beside this one.
chmod -R a-w "$DEST" || warn "could not write-protect ${DEST#"$WORKSPACE_ROOT"/}"
chmod a-w "$FROZEN_ROOT" || warn "could not re-protect ${FROZEN_ROOT#"$WORKSPACE_ROOT"/}"
log "froze $(find "$DEST" -type f | wc -l) files into ${DEST#"$WORKSPACE_ROOT"/} (read-only)"

# --- the pin: written whole, so a re-freeze diffs as two changed values ---

cat > "$PIN" <<'PINEOF'
# The canonical freeze: the one export tree and the one log snapshot behind every
# published report number. Committed, because a report's provenance is only citable if
# the pin that produced it is in git.
#
# Written by `make annotation-freeze DATE=<date> RUN_AT=<run_at>` — commit the change it
# makes. Editing by hand works but skips that command's guards; see docs/eval.md.
#
# The snapshot is pinned by timestamp rather than taken as "the latest": the nightly cron
# appends one every night, so a report re-run months later must still read the line it was
# built from.

PINEOF
printf 'FREEZE_DATE=%s\nCANONICAL_SNAPSHOT_RUN_AT=%s\n' "$DATE" "$RUN_AT" >> "$PIN" \
  || fatal "could not write $PIN"
log "pinned $PIN -> $DATE @ $RUN_AT"

section "next"
cat >&2 <<EOF
1. Commit the pin. Until it is committed the freeze is not canonical, and another
   checkout still resolves the old date:
     git add $PIN && git commit -m 'chore(eval): pin the $DATE freeze'

2. Regenerate the deliverables on the clean tree:
     make eval-report eval-score eval-catalog

3. Re-pin the reproducibility bundle, which can only happen now: it pins the frozen
   inputs AND the report outputs together, and the outputs did not exist until step 2.
   repro-pin refuses an existing bundle dir, so remove the superseded one first, then
   restore the hand-written bundle README and commit:
     rm -rf reproducibility/<superseded>-eval-report
     make repro-pin KIND=freeze NAME=eval-report \\
       PATHS="data/annotation/exports-frozen/$DATE reports/eval/<report-date>"

Publishing (transfer-push) is a separate decision — see docs/eval.md.
EOF
