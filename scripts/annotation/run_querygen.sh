#!/bin/bash
# scripts/annotation/run_querygen.sh [spec1,spec2,...]
#
# Runs pragmata querygen across configs/annotation/querygen_specs/ — all specs by default, or a
# comma-separated subset (first positional arg).
#   baseline  <domain>.yaml           -> N=$N_BASELINE
#   edge-case <domain>_edgecase.yaml  -> N=$N_EDGECASE
#
# Each spec is deep-merged with configs/annotation/querygen_specs/_runtime.yaml (shared model /
# batching / timeout knobs) via scripts/annotation/merge_yaml.py, then passed to pragmata
# via --config-path. Azure is reached natively through pragmata's `openai`
# provider pointed at the Azure v1 endpoint (set OPENAI_API_KEY + OPENAI_BASE_URL
# in .env; model_provider: openai in _runtime.yaml) — no wrapper needed.

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
cd_root

MERGE="scripts/annotation/merge_yaml.py"
RUNTIME="configs/annotation/querygen_specs/_runtime.yaml"

# --- spec selection: comma-list, or all non-underscore specs ---
if [[ -n "${1:-}" ]]; then
  specs=()
  while IFS= read -r stem; do
    if [[ -f "configs/annotation/querygen_specs/${stem}.yaml" ]]; then
      specs+=("configs/annotation/querygen_specs/${stem}.yaml")
    else
      warn "no spec at configs/annotation/querygen_specs/${stem}.yaml, skipping"
    fi
  done < <(split_csv "$1")
  [[ ${#specs[@]} -gt 0 ]] || fatal "no valid specs after filter" 6
else
  specs=(configs/annotation/querygen_specs/[!_]*.yaml)
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
  if ! "$PRAGMATA" -v querygen gen-queries \
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
