#!/bin/bash
# scripts/annotation/freeze.sh [date] [run_at]
#
# Cuts the canonical freeze: an immutable dated copy of data/annotation/exports/, plus the
# pin (configs/eval/freeze.conf) that points every eval report at it and at the one log
# snapshot taken beside it. Run as `make annotation-freeze` on its own — DATE and RUN_AT
# both derive from the export tree's own created_at; pass either explicitly to override.
#
# Why this is a target of its own rather than a step of annotation-export: daily.sh's 02:00
# cron re-runs the export every night, so freezing on export would mint a write-protected
# dated copy per night. A freeze asserts "these bytes back a published number" — an
# editorial decision, not a cron side effect.
#
# It stops one step short of done. The pin it writes only becomes canonical once COMMITTED,
# and a script must not make that commit: docs/report-deliverables.md step 1 forbids rewriting history
# after provenance files name a commit, so the operator owns it. The follow-ups — that
# commit, the regeneration, and the bundle re-pin — are printed at the end.
#
# Everything before the copy is a guard, and every guard is here because the mistake it
# catches is expensive: a freeze is immutable, published, and cited by report numbers. See
# docs/report-deliverables.md ("Cutting a new freeze") for the surrounding procedure.

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
cd_root

DATE="${1:-}"
RUN_AT="${2:-}"

# One export or freeze at a time. daily.sh's 02:00 cron rewrites data/annotation/exports/
# in place and pseudonymises it afterwards, so a freeze that overlapped it could copy a
# half-rewritten or not-yet-pseudonymised tree into an immutable dated directory. Held
# before the guards below, because they read that same tree. Same mechanism and same
# lock file as export.sh (and pipeline.sh's own): flock on a held fd, released by the
# kernel however the process dies.
exec 9>".export.lock"
flock -n 9 || fatal "an export or freeze is already running" 3

SRC="$DATA_DIR/annotation/exports"
FROZEN_ROOT="$DATA_DIR/annotation/exports-frozen"
PIN="configs/eval/freeze.conf"

# --- guards: all of them before anything is written ---

# Checked immediately if given, rather than waiting for the derived value below to fail
# the same regex — a hand-typed DATE should fail fast, before any other guard runs.
[[ -z "$DATE" || "$DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] \
  || fatal "DATE must be YYYY-MM-DD: $DATE"

# Likewise: an explicit DATE that's already frozen should fail before the derivation
# below reads every programme's meta.json and scans the log — a derived DATE isn't known
# yet, so it gets the same check again once resolved.
[[ -z "$DATE" || ! -e "$FROZEN_ROOT/$DATE" ]] \
  || fatal "already frozen: ${FROZEN_ROOT#"$WORKSPACE_ROOT"/}/$DATE — pick another DATE"

# A freeze cut from a dirty tree has no citable lineage of its own: every .provenance.json
# records the workspace commit it was generated at (docs/report-deliverables.md step 1).
[[ -z "$(git status --porcelain)" ]] \
  || fatal "working tree is dirty — commit or stash first, so the freeze can cite a commit"

[[ -d "$SRC" ]] || fatal "no export tree at ${SRC#"$WORKSPACE_ROOT"/} — run 'make annotation-export'"
shopt -s nullglob
programmes=("$SRC"/*/)
shopt -u nullglob
(( ${#programmes[@]} > 0 )) \
  || fatal "${SRC#"$WORKSPACE_ROOT"/} holds no programme dirs — nothing to freeze"

# DATE and RUN_AT are both optional: a freeze names and pairs one export moment, and the
# tool has what it needs to derive both itself. Every programme's
# annotation_export.meta.json carries created_at; DATE defaults to that moment's UTC
# calendar date, and RUN_AT to the first log snapshot taken after it — the run always
# exports before it logs (scripts/daily.sh), so that snapshot is the one taken beside it.
# Passing either explicitly still works — DATE to re-date a freeze, RUN_AT for a
# non-nightly export — but RUN_AT is then validated against the same pairing rather than
# only checked for existing in the log. resolve_freeze_pin also runs find_snapshot's
# schema-version check, so a mistyped or stale RUN_AT is caught here, not at report time.
resolved="$("$PY" - "$SRC" "$DATE" "$RUN_AT" <<'PYEOF'
import sys
from pathlib import Path

sys.path.insert(0, str(Path("scripts/lib").resolve()))
import workspace as ws

export_dir, date, run_at = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
resolved_date, resolved_run_at = ws.resolve_freeze_pin(
    export_dir, date or None, run_at or None
)
print(resolved_date)
print(resolved_run_at)
PYEOF
)" || fatal "DATE/RUN_AT could not be resolved against ${SRC#"$WORKSPACE_ROOT"/} — see above"
mapfile -t resolved_fields <<< "$resolved"
resolved_date="${resolved_fields[0]}"
resolved_run_at="${resolved_fields[1]}"
[[ -n "$DATE" ]] || log "derived DATE=$resolved_date from the export's created_at"
[[ -n "$RUN_AT" ]] || log "derived RUN_AT=$resolved_run_at from the export's created_at and logs/annotation/log.jsonl"
DATE="$resolved_date"
RUN_AT="$resolved_run_at"
DEST="$FROZEN_ROOT/$DATE"

# Never overwrite a published freeze: something already cites those bytes.
[[ ! -e "$DEST" ]] \
  || fatal "already frozen: ${DEST#"$WORKSPACE_ROOT"/} — pick another DATE"

# Before the copy, not after: transfer-push runs this same check, but by then the names
# would already sit in a write-protected tree that a published number cites.
python3 "$WORKSPACE_ROOT/scripts/lib/check_pseudonymised.py" "$SRC" \
  || fatal "refusing to freeze ${SRC#"$WORKSPACE_ROOT"/}: non-pseudonymised annotator identities — re-run 'make annotation-export'"

# --- the copy: write-protected, and the parent left as we found it ---

section "freeze: annotation/exports -> annotation/exports-frozen/$DATE"
mkdir -p "$FROZEN_ROOT"
# The parent is meant to be write-protected — that is what stops a stray cp landing a
# second tree beside a published freeze. So unlock it only if it is genuinely locked:
# chmod needs ownership, and in a checkout shared by POSIX ACL these dirs can belong to
# another user while still being group-writable to us.
[[ -w "$FROZEN_ROOT" ]] || chmod u+w "$FROZEN_ROOT" 2>/dev/null \
  || fatal "cannot write into ${FROZEN_ROOT#"$WORKSPACE_ROOT"/}: not writable, and owned by $(stat -c %U "$FROZEN_ROOT")"
# Anything that goes wrong between here and the write-protection leaves a $DEST that the
# "already frozen" guard would read as a real freeze on the next run — blocking the retry
# with a message that is simply untrue. So the partial tree goes, and the parent is
# re-locked, before we bail. chmod first: cp carries the source modes across, and rm needs
# write permission on the directories it descends.
abort_partial_freeze() {
  chmod -R u+w "$DEST" 2>/dev/null
  rm -rf "$DEST"
  chmod a-w "$FROZEN_ROOT" 2>/dev/null
  fatal "$1"
}
cp -r "$SRC" "$DEST" || abort_partial_freeze "copy failed: $SRC -> $DEST"
# The new tree first, then the parent. Fatal on the tree, because write-protection is the
# whole immutability guarantee a freeze makes — a writable dated copy is not a freeze.
chmod -R a-w "$DEST" \
  || abort_partial_freeze "could not write-protect ${DEST#"$WORKSPACE_ROOT"/} — a writable copy is not a freeze"
chmod a-w "$FROZEN_ROOT" 2>/dev/null \
  || warn "${FROZEN_ROOT#"$WORKSPACE_ROOT"/} stays writable (owned by $(stat -c %U "$FROZEN_ROOT")) — a stray copy could still land beside this freeze"
log "froze $(find "$DEST" -type f | wc -l) files into ${DEST#"$WORKSPACE_ROOT"/} (read-only)"

# --- the pin: written whole, so a re-freeze diffs as two changed values ---

cat > "$PIN" <<'PINEOF'
# The canonical freeze: the one export tree and the one log snapshot behind every
# published report number. Committed, because a report's provenance is only citable if
# the pin that produced it is in git.
#
# Written by `make annotation-freeze` — commit the change it makes. Editing by hand works
# but skips that command's guards; see docs/report-deliverables.md.
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
     make eval-deliverables

3. Re-pin the reproducibility bundle, which can only happen now: it pins the frozen
   inputs AND the report outputs together, and the outputs did not exist until step 2.
   repro-pin refuses an existing bundle dir, so remove the superseded one first, then
   restore the hand-written bundle README and commit:
     rm -rf reproducibility/<superseded>-eval-report
     make repro-pin KIND=freeze NAME=eval-report \\
       PATHS="data/annotation/exports-frozen/$DATE reports/eval/<report-date>"

Publishing (transfer-push) is a separate decision — see docs/report-deliverables.md.
EOF
