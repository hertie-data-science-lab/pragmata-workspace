#!/bin/bash
# scripts/pipeline.sh [--from STAGE] [--to STAGE] [--only STAGE]
#                     [--filter DOMAINS] [--jobs N] [--no-preflight] [--dry-run]
#
# Runs a contiguous slice of the annotation pipeline:
#
#     querygen -> bot -> combine -> setup -> import
#
# over an optional domain filter, owning the cross-cutting concerns the atomic
# stage scripts don't: stage-aware pre-flight, a lockfile, bot parallelism,
# tee logging, per-stage timing, and continue-on-error with a final summary.
#
# Stage scripts remain runnable on their own; this just orchestrates them.
#
#   pipeline.sh                          # full pipeline, all domains
#   pipeline.sh --to bot                 # querygen + bot
#   pipeline.sh --from combine           # combine + setup + import
#   pipeline.sh --only setup             # provision Argilla workspaces/users
#   pipeline.sh --only import            # import every domain
#   pipeline.sh --only bot --filter gesundheit --jobs 8
#   pipeline.sh --from querygen --to combine --filter gesundheit,europas-zukunft
#   pipeline.sh --dry-run                # print the plan and exit
#
# --filter takes DOMAINS (e.g. gesundheit,europas-zukunft); querygen/bot expand
# each to its specs (<domain> + <domain>_edgecase), combine/import use domains.
#
# Cron/tmux friendly: lockfile + exit codes + runs/annotation/runs/pipeline.log. Example:
#   tmux new -s pipeline 'bash scripts/pipeline.sh'

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
cd_root

STAGES=(querygen bot combine setup import)

# --- args ---
FROM="querygen"; TO="import"; FILTER=""; JOBS="${N_PARALLEL_BOTS:-4}"
DO_PREFLIGHT=1; DRY_RUN=0

usage() { sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

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

# --- filter resolution ---
# domains: filter list, or all configs/annotation/.
filter_domains() {
  if [[ -n "$FILTER" ]]; then split_csv "$FILTER"
  else ls configs/annotation/*.yaml 2>/dev/null | xargs -n1 basename | sed 's/\.yaml$//'; fi
}
# specs: each domain -> <domain> + <domain>_edgecase (only those with a spec yaml);
# or all non-underscore specs when unfiltered.
filter_specs() {
  if [[ -n "$FILTER" ]]; then
    local d s
    while IFS= read -r d; do
      for s in "$d" "${d}_edgecase"; do
        [[ -f "configs/annotation/querygen_specs/${s}.yaml" ]] && printf '%s\n' "$s"
      done
    done < <(split_csv "$FILTER")
  else
    ls configs/annotation/querygen_specs/[!_]*.yaml 2>/dev/null | xargs -n1 basename | sed 's/\.yaml$//'
  fi
}

# --- stages (each returns its rc) ---
stage_querygen() {
  local csv=""; [[ -n "$FILTER" ]] && csv="$(filter_specs | paste -sd,)"
  bash scripts/annotation/run_querygen.sh "$csv"
}

stage_bot() {
  mapfile -t specs < <(filter_specs | while IFS= read -r s; do
    [[ -f "data/annotation/querygen/runs/${s}/synthetic_queries.csv" ]] && echo "$s"
  done)
  log "bot: ${#specs[@]} spec(s), ${JOBS}-way parallel"
  (( ${#specs[@]} > 0 )) || return 0
  mkdir -p runs/annotation/runs
  printf '%s\n' "${specs[@]}" | PY="$PY" xargs -P "$JOBS" -I {} bash -c '
    stem="$1"; log="runs/annotation/runs/run_bot.${stem}.log"
    echo "[$(date -Iseconds)] start" > "$log"
    "$PY" scripts/annotation/run_bot.py --spec "$stem" >> "$log" 2>&1
    rc=$?; echo "[bot:$stem] finished (rc=$rc)"; exit $rc
  ' _ {}
}

stage_combine() {
  mapfile -t doms < <(filter_domains)
  "$PY" scripts/annotation/build_combined.py "${doms[@]}"
}

stage_setup() {
  local d rc=0
  while IFS= read -r d; do
    bash scripts/annotation/setup.sh "$d" || { warn "setup failed: $d"; rc=1; }
  done < <(filter_domains)
  return "$rc"
}

stage_import() {
  local d rc=0
  while IFS= read -r d; do
    bash scripts/annotation/import.sh "$d" || { warn "import failed: $d"; rc=1; }
  done < <(filter_domains)
  return "$rc"
}

# --- pre-flight (stage-aware) ---
preflight() {
  (( DO_PREFLIGHT )) || { log "pre-flight skipped (--no-preflight)"; return; }
  section "pre-flight"
  check_disk
  if in_slice querygen; then
    require_env OPENAI_API_KEY OPENAI_BASE_URL
    local sample; sample="$(ls configs/annotation/querygen_specs/[!_]*.yaml | head -1)"
    "$PY" scripts/annotation/merge_yaml.py configs/annotation/querygen_specs/_runtime.yaml "$sample" \
      | "$PY" -c "import sys,yaml; from pragmata.core.settings.querygen_settings import QueryGenRunSettings; QueryGenRunSettings.resolve(config=yaml.safe_load(sys.stdin))" \
        >/dev/null 2>&1 \
      || fatal "_runtime.yaml + $(basename "$sample") failed QueryGenRunSettings validation" 4
    log "  config: querygen schema validates"
  fi
  if in_slice bot; then
    az account show >/dev/null 2>&1 || fatal "az not authenticated; run 'az login --use-device-code'" 4
    log "  az: $(az account show --query user.name -o tsv 2>/dev/null)"
  fi
  if in_slice setup || in_slice import; then
    require_env ARGILLA_API_URL ARGILLA_API_KEY
  fi
  if in_slice setup; then
    [[ -f config/users.json ]] || fatal "config/users.json (roster) missing" 4
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
  { in_slice querygen || in_slice bot; } && log "specs  : $(filter_specs | paste -sd' ')"
  { in_slice combine || in_slice setup || in_slice import; } && log "domains: $(filter_domains | paste -sd' ')"
  exit 0
fi

# --- lockfile: one heavy run at a time ---
LOCK=".pipeline.lock"
if [[ -f "$LOCK" ]]; then
  existing="$(cat "$LOCK" 2>/dev/null)"
  if [[ -n "$existing" ]] && kill -0 "$existing" 2>/dev/null; then
    fatal "another pipeline run is in flight (PID $existing)" 3
  fi
  log "removing stale lockfile (PID $existing not alive)"
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

mkdir -p runs/annotation/runs
exec > >(tee -a runs/annotation/runs/pipeline.log) 2>&1

section "pipeline started: $(ts)  [stages: ${planned[*]}  filter: ${FILTER:-all}]"
preflight

declare -A RC DUR
overall=0
for s in "${planned[@]}"; do
  section "stage: $s"
  start=$SECONDS
  "stage_$s"; rc=$?
  RC[$s]=$rc; DUR[$s]=$(( SECONDS - start ))
  (( rc == 0 )) || overall=1
  log "stage $s finished (rc=$rc, ${DUR[$s]}s)"
done

section "pipeline summary: $(ts)"
for s in "${planned[@]}"; do log "  $s: rc=${RC[$s]}, ${DUR[$s]}s"; done
exit "$overall"
