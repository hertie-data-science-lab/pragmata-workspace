#!/bin/bash
# scripts/run_querygen.sh [spec1,spec2,...]
#
# Runs pragmata querygen across querygen_specs/ — all specs by default, or a
# comma-separated subset (first positional arg).
#   baseline  <domain>.yaml           -> N=$N_BASELINE
#   edge-case <domain>_edgecase.yaml  -> N=$N_EDGECASE
#
# Each spec is deep-merged with querygen_specs/_runtime.yaml (shared model /
# batching / timeout knobs) via scripts/merge_yaml.py, then passed to pragmata
# via --config-path. Azure is routed through scripts/pragmata_azure.py.
#
# Resume and the per-call timeout are pragmata's job now: it resumes by default
# on the same run_id, and the HTTP timeout lives in _runtime.yaml. So this
# script is just merge + loop + N. Continues past per-spec failures.

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
cd_root

WRAPPER="scripts/pragmata_azure.py"
MERGE="scripts/merge_yaml.py"
RUNTIME="querygen_specs/_runtime.yaml"

# --- spec selection: comma-list, or all non-underscore specs ---
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
  specs=(querygen_specs/[!_]*.yaml)
fi

merged="$(mktemp --suffix=.yaml)"
trap 'rm -f "$merged"' EXIT
log "Running ${#specs[@]} spec(s) through querygen..."

failures=()
for spec in "${specs[@]}"; do
  stem="$(basename "$spec" .yaml)"
  if [[ "$stem" == *_edgecase ]]; then n="$N_EDGECASE"; else n="$N_BASELINE"; fi
  section "querygen: $stem (N=$n)"

  if ! "$PY" "$MERGE" "$RUNTIME" "$spec" > "$merged"; then
    warn "  failed to merge $RUNTIME + $spec"; failures+=("$stem (merge)"); continue
  fi
  if ! "$PY" "$WRAPPER" -v querygen gen-queries \
      --config-path "$merged" --n-queries "$n" --run-id "$stem"; then
    warn "  failed: $stem"; failures+=("$stem (gen-queries)")
  fi
done

section "Summary"
if (( ${#failures[@]} > 0 )); then
  log "FAILED (${#failures[@]}/${#specs[@]}):"
  printf '  - %s\n' "${failures[@]}" >&2
  exit 1
fi
log "All ${#specs[@]} spec(s) completed."
