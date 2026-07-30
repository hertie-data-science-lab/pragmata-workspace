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
SPECS_DIR="configs/annotation/querygen_specs"
RUNTIME="$SPECS_DIR/_runtime.yaml"

# --- spec selection: comma-list, or every spec. Stems throughout; the path is only
#     built where a file is actually read. ---
stems=()
if [[ -n "${1:-}" ]]; then
  while IFS= read -r stem; do
    if [[ -f "$SPECS_DIR/${stem}.yaml" ]]; then
      stems+=("$stem")
    else
      warn "no spec at $SPECS_DIR/${stem}.yaml, skipping"
    fi
  done < <(split_csv "$1")
  [[ ${#stems[@]} -gt 0 ]] || fatal "no valid specs after filter" 6
else
  mapfile -t stems < <(config_stems "$SPECS_DIR")
  [[ ${#stems[@]} -gt 0 ]] || fatal "no specs under $SPECS_DIR/" 6
fi

merged="$(mktemp --suffix=.yaml)"
trap 'rm -f "$merged"' EXIT
log "Running ${#stems[@]} spec(s) through querygen..."

failures=()
for stem in "${stems[@]}"; do
  if [[ "$stem" == *_edgecase ]]; then n="$N_EDGECASE"; else n="$N_BASELINE"; fi
  section "querygen: $stem (N=$n)"

  spec="$SPECS_DIR/${stem}.yaml"
  if ! "$PY" "$MERGE" "$RUNTIME" "$spec" > "$merged"; then
    warn "  failed to merge $RUNTIME + $spec"; failures+=("$stem (merge)"); continue
  fi
  if ! "$PRAGMATA" -v querygen gen-queries \
      --config-path "$merged" --n-queries "$n" --run-id "$stem" \
      --base-dir "$DATA_DIR"; then
    warn "  failed: $stem"; failures+=("$stem (gen-queries)")
  fi
done

section "Summary"
if (( ${#failures[@]} > 0 )); then
  log "FAILED (${#failures[@]}/${#stems[@]}):"
  printf '  - %s\n' "${failures[@]}" >&2
  exit 1
fi
log "All ${#stems[@]} spec(s) completed."
