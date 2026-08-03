#!/bin/bash
# scripts/repro/reset.sh [--pin <bundle>] [--apply] [--drop-carryover]
#
# Clears one dataset run's generated output so the next run starts on a clean tree.
# The pipeline writes to fixed, non-per-run paths (that is what makes its stages
# resumable), so an earlier run left in place is silently merged into the next one.
# This is the mechanised form of the archive-and-clear procedure in
# docs/implementation-guide.md §11.3.
#
# Deletes, and only these:
#   data/querygen/runs/                   the whole tree
#   data/publikationsbot/*                every file except .gitkeep
#   data/querygen/*.json                  ONLY with --drop-carryover (see below)
#
# Never touches data/annotation/ (exports and the frozen inputs behind published
# numbers), logs/, argilla_backup/, reports/, or any .gitkeep.
#
# Previews by default and deletes only with --apply, matching `make repro-reproduce`.
#
# The guard: deleting an unpinned run destroys an unreproducible artefact, so --apply
# refuses unless --pin names a bundle that verifies clean against the working tree -
# i.e. proof that the pin is a faithful record of what is about to be deleted. Pin the
# outgoing run first:
#
#   make repro-pin KIND=freeze NAME=dataset-run PATHS="data/querygen data/publikationsbot"
#
# A pin is not an archive. It records checksums inside the repository; it does not copy
# the run anywhere. Copy the run out and verify the copy (§11.3 steps 2-3) before
# running this with --apply.

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
cd_root

PIN=""
APPLY=0
DROP_CARRYOVER=0

while (($#)); do
  case "$1" in
    --pin) PIN="${2:-}"; shift 2 || fatal "--pin needs a bundle directory name" ;;
    --apply) APPLY=1; shift ;;
    --drop-carryover) DROP_CARRYOVER=1; shift ;;
    -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) fatal "unknown argument: $1 (see --help)" 2 ;;
  esac
done

QUERYGEN_RUNS="data/querygen/runs"
BOT_DIR="data/publikationsbot"

# --- what is there -----------------------------------------------------------------

mapfile -t bot_files < <(find "$BOT_DIR" -type f ! -name .gitkeep 2>/dev/null | sort)
mapfile -t carryover < <(find data/querygen -maxdepth 1 -type f -name '*.json' 2>/dev/null | sort)
querygen_files=0
[[ -d $QUERYGEN_RUNS ]] && querygen_files=$(find "$QUERYGEN_RUNS" -type f | wc -l)

to_delete=$((querygen_files + ${#bot_files[@]}))
((DROP_CARRYOVER)) && to_delete=$((to_delete + ${#carryover[@]}))

section "reset: run output in the working tree"
if [[ -d $QUERYGEN_RUNS ]]; then
  log "$QUERYGEN_RUNS/            $querygen_files files (whole tree would go)"
else
  log "$QUERYGEN_RUNS/            absent - already clear"
fi
log "$BOT_DIR/     ${#bot_files[@]} files (.gitkeep kept)"
if ((${#carryover[@]})); then
  if ((DROP_CARRYOVER)); then
    log "data/querygen/*.json          ${#carryover[@]} planning summaries would be DELETED"
  else
    log "data/querygen/*.json          ${#carryover[@]} planning summaries KEPT (--drop-carryover to delete)"
    log "  kept = the next run steers away from the questions the last one asked;"
    log "  dropped = it plans from the spec alone. Archive them either way (§11.3 step 5)."
  fi
fi

if ((to_delete == 0)); then
  log "nothing to delete - the tree is already clean"
  exit 0
fi

# --- the guard ---------------------------------------------------------------------

if ((APPLY)); then
  [[ -n $PIN ]] || fatal "refusing to delete $to_delete files without --pin.
  Pin the outgoing run first, so the archive is verifiable:
    make repro-pin KIND=freeze NAME=dataset-run PATHS=\"data/querygen data/publikationsbot\"
  Copy it out and verify the copy (§11.3 steps 2-3), then:
    make reset PIN=<bundle> APPLY=1" 2

  [[ -d "reproducibility/$PIN" ]] || fatal "no such bundle: reproducibility/$PIN" 2
  pins_file="reproducibility/$PIN/pins.sha256"
  [[ -f $pins_file ]] || fatal "$pins_file missing - $PIN is not a usable pin" 2

  # Coverage: a bundle can verify clean and still say nothing about half the tree - pin
  # only data/querygen and every Publikationsbot file is unrecorded, yet deletable. So
  # require the pin to name every file about to go, not merely to be internally consistent.
  section "checking $PIN covers what would be deleted"
  mapfile -t pinned < <(sed -E 's/^[a-f0-9]{64}[[:space:]]+//' "$pins_file" | sort)
  targets=()
  [[ -d $QUERYGEN_RUNS ]] && mapfile -t -O "${#targets[@]}" targets < <(find "$QUERYGEN_RUNS" -type f | sort)
  targets+=("${bot_files[@]}")
  ((DROP_CARRYOVER)) && targets+=("${carryover[@]}")

  uncovered=()
  for t in "${targets[@]}"; do
    printf '%s\n' "${pinned[@]}" | grep -qxF "$t" || uncovered+=("$t")
  done
  if ((${#uncovered[@]})); then
    printf '  UNPINNED: %s\n' "${uncovered[@]}" >&2
    fatal "${#uncovered[@]} of ${#targets[@]} files are not in $PIN - refusing to delete.
  The pin does not record them, so deleting them would lose them for good. Re-pin with
  every path the run wrote:
    make repro-pin KIND=freeze NAME=dataset-run PATHS=\"data/querygen data/publikationsbot\"" 2
  fi
  log "coverage ok: all ${#targets[@]} files are pinned"

  section "verifying $PIN against the working tree"
  if ! .venv/bin/python scripts/repro/bundle.py verify "$PIN"; then
    fatal "pin $PIN does not match the working tree - refusing to delete.
  Every line must read OK: a MISMATCH or ABSENT means the bundle is not a faithful
  record of what is here, so deleting now would lose the difference. Re-pin, or
  investigate what changed." 2
  fi
  log "pin verified: $PIN is a faithful record of the tree"
fi

# --- act, or show what would happen ------------------------------------------------

if ((APPLY == 0)); then
  section "preview only - nothing deleted"
  log "$to_delete files would be deleted. Re-run with APPLY=1 (and PIN=<bundle>) to act."
  exit 0
fi

section "deleting"
if [[ -d $QUERYGEN_RUNS ]]; then
  rm -rf "$QUERYGEN_RUNS" || fatal "failed to remove $QUERYGEN_RUNS"
  log "removed $QUERYGEN_RUNS/"
fi
if ((${#bot_files[@]})); then
  find "$BOT_DIR" -type f ! -name .gitkeep -delete || fatal "failed to clear $BOT_DIR"
  log "cleared $BOT_DIR/ (${#bot_files[@]} files)"
fi
if ((DROP_CARRYOVER)) && ((${#carryover[@]})); then
  rm -f data/querygen/*.json || fatal "failed to remove the planning carry-over"
  log "removed ${#carryover[@]} planning summaries"
fi

section "done"
log "confirm with the §4 check: ls $QUERYGEN_RUNS should fail, $BOT_DIR/ should hold only .gitkeep,"
log "and make plan should list the full stage sequence for every domain in scope."
