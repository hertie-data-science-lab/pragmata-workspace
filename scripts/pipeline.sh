#!/bin/bash
#>>> usage
# scripts/pipeline.sh [--from STAGE] [--to STAGE] [--only STAGE]
#                     [--filter DOMAINS] [--jobs N] [--no-preflight] [--dry-run]
#
# Runs a contiguous slice of the dataset build pipeline, in this order:
#
#     querygen-run       generate synthetic queries
#     bot-run            query publikationsbot for answers + chunks
#     combine-run        assemble the import-ready dataset
#     annotation-setup   provision Argilla workspaces + users
#     annotation-import  load the datasets into Argilla
#
# It ends at the Argilla import: annotating, logging and reporting are separate
# operations, not stages of this pipeline.
#
# The slice runs over an optional domain filter, owning the cross-cutting concerns
# the atomic stage scripts don't: stage-aware pre-flight, a lock, bot parallelism,
# tee logging, per-stage timing, and continue-on-error with a final summary.
#
# Stage scripts remain runnable on their own; this just orchestrates them. The stage
# tokens below are exactly the make target names for the same stages.
#
#   pipeline.sh                                 # full pipeline, all domains
#   pipeline.sh --to bot-run                    # querygen-run + bot-run
#   pipeline.sh --from combine-run              # combine-run + the two annotation stages
#   pipeline.sh --only annotation-setup         # provision Argilla workspaces/users
#   pipeline.sh --only annotation-import        # import every domain
#   pipeline.sh --only bot-run --filter gesundheit --jobs 8
#   pipeline.sh --from querygen-run --to combine-run --filter gesundheit,europas-zukunft
#   pipeline.sh --dry-run                       # print the plan and exit
#
# --filter takes DOMAINS (e.g. gesundheit,europas-zukunft); querygen-run/bot-run
# expand each to its specs (<domain> + <domain>_edgecase), the rest use domains.
#
# Cron/tmux friendly: lock + exit codes + logs/annotation/pipeline.log. Example:
#   tmux new -s pipeline 'bash scripts/pipeline.sh'
#<<< usage

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
cd_root

# Stage tokens, in order. These are the make target names for the same stages, so
# `--only bot-run` and `make bot-run` name one thing.
STAGES=(querygen-run bot-run combine-run annotation-setup annotation-import)

# --- args ---
FROM="querygen-run"; TO="annotation-import"; FILTER=""; JOBS="${N_PARALLEL_BOTS:-4}"
DO_PREFLIGHT=1; DRY_RUN=0

# Help text is the header comment between the >>> usage / <<< usage markers, so it
# cannot drift out of range the way a hardcoded line span does.
usage() {
  sed -n '/^#>>> usage/,/^#<<< usage/{ /^#[<>]\{3\} usage/d; s/^# \{0,1\}//; p; }' \
    "${BASH_SOURCE[0]}"
  exit "${1:-0}"
}

while (( $# )); do
  case "$1" in
    --from)         FROM="$2"; shift 2 ;;
    --to)           TO="$2"; shift 2 ;;
    --only)         FROM="$2"; TO="$2"; shift 2 ;;
    --filter)       FILTER="$2"; shift 2 ;;
    --jobs)         JOBS="$2"; shift 2 ;;
    --no-preflight) DO_PREFLIGHT=0; shift ;;
    --dry-run|-n)   DRY_RUN=1; shift ;;
    -h|--help)      usage 0 ;;
    *)              fatal "unknown arg: $1 (try --help)" 2 ;;
  esac
done

stage_index() {
  local i
  for i in "${!STAGES[@]}"; do [[ "${STAGES[$i]}" == "$1" ]] && { echo "$i"; return; }; done
  echo -1
}
FROM_IDX="$(stage_index "$FROM")"; TO_IDX="$(stage_index "$TO")"
(( FROM_IDX >= 0 )) || fatal "unknown --from stage: $FROM (one of: ${STAGES[*]})" 2
(( TO_IDX  >= 0 )) || fatal "unknown --to stage: $TO (one of: ${STAGES[*]})" 2
(( FROM_IDX <= TO_IDX )) || fatal "--from ($FROM) comes after --to ($TO)" 2
in_slice() { local i; i="$(stage_index "$1")"; (( i >= FROM_IDX && i <= TO_IDX )); }

# --- filter resolution (unfiltered lists come from config_stems in lib/common.sh) ---
# domains: filter list, or all configs/annotation/domains/.
filter_domains() {
  if [[ -n "$FILTER" ]]; then split_csv "$FILTER"
  else config_stems configs/annotation/domains; fi
}
# specs: each domain -> <domain> + <domain>_edgecase (only those with a spec yaml);
# or all specs when unfiltered.
filter_specs() {
  if [[ -n "$FILTER" ]]; then
    local d s
    while IFS= read -r d; do
      for s in "$d" "${d}_edgecase"; do
        [[ -f "configs/annotation/querygen_specs/${s}.yaml" ]] && printf '%s\n' "$s"
      done
    done < <(split_csv "$FILTER")
  else
    config_stems configs/annotation/querygen_specs
  fi
}

# --- stages (each returns its rc) ---
# One function per stage token, hyphens as underscores (see the dispatch below).
stage_querygen_run() {
  local csv=""; [[ -n "$FILTER" ]] && csv="$(filter_specs | paste -sd,)"
  bash scripts/annotation/run_querygen.sh "$csv"
}

stage_bot_run() {
  mapfile -t specs < <(filter_specs | while IFS= read -r s; do
    [[ -f "data/querygen/runs/${s}/synthetic_queries.csv" ]] && echo "$s"
  done)
  log "bot-run: ${#specs[@]} spec(s), ${JOBS}-way parallel"
  (( ${#specs[@]} > 0 )) || return 0
  mkdir -p logs/annotation
  printf '%s\n' "${specs[@]}" | PY="$PY" xargs -P "$JOBS" -I {} bash -c '
    stem="$1"; log="logs/annotation/run_bot.${stem}.log"
    echo "[$(date -Iseconds)] start" > "$log"
    "$PY" scripts/annotation/run_bot.py --spec "$stem" >> "$log" 2>&1
    rc=$?; echo "[bot-run:$stem] finished (rc=$rc)"; exit $rc
  ' _ {}
}

stage_combine_run() {
  mapfile -t doms < <(filter_domains)
  "$PY" scripts/annotation/build_combined.py "${doms[@]}"
}

stage_annotation_setup() {
  local d rc=0
  while IFS= read -r d; do
    bash scripts/annotation/setup.sh "$d" || { warn "annotation-setup failed: $d"; rc=1; }
  done < <(filter_domains)
  return "$rc"
}

stage_annotation_import() {
  local d rc=0
  while IFS= read -r d; do
    bash scripts/annotation/import.sh "$d" || { warn "annotation-import failed: $d"; rc=1; }
  done < <(filter_domains)
  return "$rc"
}

# --- pre-flight (stage-aware) ---
preflight() {
  (( DO_PREFLIGHT )) || { log "pre-flight skipped (--no-preflight)"; return; }
  section "pre-flight"
  check_disk
  if in_slice querygen-run; then
    require_env OPENAI_API_KEY OPENAI_BASE_URL
    local stem; stem="$(config_stems configs/annotation/querygen_specs | head -1)"
    [[ -n "$stem" ]] || fatal "no specs under configs/annotation/querygen_specs/" 4
    local sample="configs/annotation/querygen_specs/${stem}.yaml"
    "$PY" scripts/annotation/merge_yaml.py configs/annotation/querygen_specs/_runtime.yaml "$sample" \
      | "$PY" -c "import sys,yaml; from pragmata.core.settings.querygen_settings import QueryGenRunSettings; QueryGenRunSettings.resolve(config=yaml.safe_load(sys.stdin))" \
        >/dev/null 2>&1 \
      || fatal "_runtime.yaml + $(basename "$sample") failed QueryGenRunSettings validation" 4
    log "  config: querygen schema validates"
  fi
  if in_slice bot-run; then
    az account show >/dev/null 2>&1 || fatal "az not authenticated; run 'az login --use-device-code'" 4
    log "  az: $(az account show --query user.name -o tsv 2>/dev/null)"
  fi
  if in_slice annotation-setup || in_slice annotation-import; then
    require_env ARGILLA_API_URL ARGILLA_API_KEY
  fi
  if in_slice annotation-setup; then
    [[ -f configs/annotation/users.json ]] || fatal "configs/annotation/users.json (roster) missing" 4
    log "  argilla: credentials + roster present"
  fi
  log "pre-flight OK"
}

# --- plan / dry-run ---
planned=()
for s in "${STAGES[@]}"; do in_slice "$s" && planned+=("$s"); done

if (( DRY_RUN )); then
  section "pipeline plan (dry-run)"
  log "stages : ${planned[*]}"
  log "filter : ${FILTER:-<all>}"
  log "jobs   : $JOBS (bot parallelism)"
  { in_slice querygen-run || in_slice bot-run; } && log "specs  : $(filter_specs | paste -sd' ')"
  { in_slice combine-run || in_slice annotation-setup || in_slice annotation-import; } && log "domains: $(filter_domains | paste -sd' ')"
  exit 0
fi

# --- lock: one heavy run at a time ---
# flock on a held fd: the kernel owns the lock, so it is released on exit however the
# process dies. No pid file, no staleness heuristic, nothing to clean up.
exec 9>".pipeline.lock"
flock -n 9 || fatal "another pipeline run is in flight" 3

mkdir -p logs/annotation
exec > >(tee -a logs/annotation/pipeline.log) 2>&1

section "pipeline started: $(ts)  [stages: ${planned[*]}  filter: ${FILTER:-all}]"
preflight

declare -A RC DUR
overall=0
for s in "${planned[@]}"; do
  section "stage: $s"
  start=$SECONDS
  # Stage tokens carry hyphens; function names use underscores.
  "stage_${s//-/_}"; rc=$?
  RC[$s]=$rc; DUR[$s]=$(( SECONDS - start ))
  (( rc == 0 )) || overall=1
  log "stage $s finished (rc=$rc, ${DUR[$s]}s)"
done

section "pipeline summary: $(ts)"
for s in "${planned[@]}"; do log "  $s: rc=${RC[$s]}, ${DUR[$s]}s"; done
exit "$overall"
