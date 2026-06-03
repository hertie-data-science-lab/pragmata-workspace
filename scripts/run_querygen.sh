#!/bin/bash
# scripts/run_querygen.sh [spec1,spec2,...]
#
# Runs pragmata querygen across querygen_specs/ — all specs by default, or a
# comma-separated subset passed as the first positional arg.
#   baseline  <domain>.yaml           -> N=$N_BASELINE
#   edge-case <domain>_edgecase.yaml  -> N=$N_EDGECASE
# (N_BASELINE / N_EDGECASE live in config/workspace.env.)
#
# Runtime config (model, reasoning_effort, batch_size, near_duplicate_tolerance,
# planning-memory) lives in querygen_specs/_runtime.yaml. Each invocation
# deep-merges _runtime.yaml + <spec>.yaml via scripts/merge_yaml.py and passes
# the result to pragmata via --config-path. Spec values win on conflict.
#
# To change the model, reasoning effort, batching, etc.: edit
# querygen_specs/_runtime.yaml. This script only carries per-run things
# (n_queries, run_id) and pipeline tunables (config/workspace.env).
#
# Requires the patched pragmata CLI (PR #222 / feat/cli-querygen-expose-runtime-knobs
# / demo-2026-05-26): _runtime.yaml uses keys validated against the full
# QueryGenRunSettings schema; older pragmata rejects the unknown top-level keys.
#
# Routes Azure OpenAI via scripts/pragmata_azure.py (registers the azure_openai
# provider in pragmata's API-key registry).
#
# Resilient to per-spec failures: continues past errors and reports at the end.
# Failures are also appended (timestamped, TSV) to querygen/runs/_failures.log.
# Idempotent: re-running a spec re-uses its run_id (overwrites CSV/meta);
# skip-on-complete avoids redoing specs whose CSV already has >=N rows.

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
cd_root

WRAPPER="scripts/pragmata_azure.py"
MERGE="scripts/merge_yaml.py"
RUNTIME="querygen_specs/_runtime.yaml"
RUNS_DIR="querygen/runs"
FAILURE_LOG="$RUNS_DIR/_failures.log"

mkdir -p "$RUNS_DIR"

failures=()

log_failure() {
  # Args: <stem> <stage>
  printf '%s\t%s\t%s\n' "$(ts)" "$1" "$2" >> "$FAILURE_LOG"
  failures+=("$1 ($2)")
}

# --- spec selection: positional comma-list, or all non-underscore specs ---
if [[ -n "${1:-}" ]]; then
  specs=()
  while IFS= read -r stem; do
    if [[ -f "querygen_specs/${stem}.yaml" ]]; then
      specs+=("querygen_specs/${stem}.yaml")
    else
      warn "no spec at querygen_specs/${stem}.yaml, skipping"
    fi
  done < <(split_csv "$1")
  [[ ${#specs[@]} -gt 0 ]] || fatal "no valid specs after filter" 6
else
  # Glob excludes _runtime.yaml (underscore-prefixed) and any future _helpers.
  specs=(querygen_specs/[!_]*.yaml)
fi

merged="$(mktemp --suffix=.yaml)"
trap 'rm -f "$merged"' EXIT
log "Running ${#specs[@]} spec(s) through querygen..."

for spec in "${specs[@]}"; do
  stem="$(basename "$spec" .yaml)"
  if [[ "$stem" == *_edgecase ]]; then n="$N_EDGECASE"; else n="$N_BASELINE"; fi

  section "querygen: $stem (N=$n)"

  # Skip-on-complete: a prior CSV with >=N rows (minus header) means done.
  # Strict >=N: if N was bumped, an old smaller CSV is regenerated.
  csv="$RUNS_DIR/$stem/synthetic_queries.csv"
  if [[ -f "$csv" ]]; then
    have=$(( $(wc -l < "$csv") - 1 ))
    if (( have >= n )); then
      log "  ✓ already complete ($have rows, skipping)"
      continue
    fi
    log "  partial/stale CSV ($have/$n rows), redoing from scratch"
  fi

  # Per-spec disk guard: refuse to start a spec if disk would imminently fill.
  free_mb="$(df -m . | awk 'NR==2 {print $4}')"
  if (( free_mb < DISK_MIN_FREE_MB )); then
    log_failure "$stem" "disk-full (${free_mb}MB)"
    fatal "only ${free_mb}MB free, refusing to continue" 2
  fi

  # Defensive mkdir (pragmata's ensure_dirs should do this too; costs nothing).
  mkdir -p "$RUNS_DIR/$stem"

  if ! "$PY" "$MERGE" "$RUNTIME" "$spec" > "$merged"; then
    warn "  FAILED to merge $RUNTIME + $spec"
    log_failure "$stem" "merge"
    continue
  fi

  # Auto-retry once on transient failure (LLM 5xx, ensure_dirs flake, etc.).
  # Per-spec wall-clock cap (QUERYGEN_SPEC_TIMEOUT) is belt-and-braces against
  # any hang the per-call httpx timeout misses; `timeout` exits 124, which
  # falls through to the retry-once logic below.
  attempt=1
  while :; do
    if timeout "$QUERYGEN_SPEC_TIMEOUT" "$PY" "$WRAPPER" -v querygen gen-queries \
        --config-path "$merged" \
        --n-queries "$n" \
        --run-id "$stem"; then
      break
    fi
    if (( attempt >= 2 )); then
      warn "  FAILED after 2 attempts: $stem"
      log_failure "$stem" "gen-queries"
      break
    fi
    log "  attempt $attempt failed, retrying in ${QUERYGEN_RETRY_BACKOFF_S}s..."
    sleep "$QUERYGEN_RETRY_BACKOFF_S"
    attempt=$(( attempt + 1 ))
  done
done

section "Summary"
log "Specs attempted: ${#specs[@]}"
log "Run dirs found:  $(ls "$RUNS_DIR" 2>/dev/null | grep -vc '^_')"
if (( ${#failures[@]} > 0 )); then
  log "FAILED runs:"
  printf '  - %s\n' "${failures[@]}" >&2
  log "Failure log: $FAILURE_LOG"
  exit 1
fi
log "All ${#specs[@]} runs completed successfully."
