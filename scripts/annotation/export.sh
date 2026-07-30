#!/bin/bash
# scripts/annotation/export.sh [domain]
#
# Exports current submitted annotations from Argilla to flat per-task CSVs via
# pragmata's native `annotation export`. With no argument it exports every
# domain; pass a domain stem to export just one.
#
# Artifacts land under data/annotation/exports/ (gitignored), keyed by export-id = domain, so
# each run overwrites that domain's "latest" snapshot. This is the durable
# counterpart to scripts/annotation/log.py, which runs its own throwaway export into a
# private temp tree purely to feed IAA + label-stats — the two don't interfere. No
# directory other than a domain stem may live under data/annotation/exports/ (the
# .gitkeep marker is the only other entry): eval-push publishes that tree and consumers
# glob exports/*/ as the domain list, so a stray dir arrives as an extra domain. This
# script warns if it finds one.
#
# Exported WITH --include-discarded so discard rows (response_status=discarded,
# discard_reason) are available to log.py's discard stats. CONTRACT: any
# submitted-only consumer must filter response_status == "submitted" (IAA already
# does); label/constraint columns are null on discarded rows.
#
# Like the other stage wrappers this uses the installed `pragmata` ($PRAGMATA);
# it must resolve to the same branch the data was imported with (see README
# "Under the hood"). For non-standard exports, call `pragmata annotation export`
# directly — but pass a --base-dir outside data/, or a domain --export-id: a
# non-domain export-id under $DATA_DIR lands in the published tree (see above).

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
cd_root
require_env ARGILLA_API_URL ARGILLA_API_KEY

# Every configs/annotation/domains/*.yaml stem — the domain list, and the set the
# stray-dir check below validates the exports tree against.
mapfile -t all_stems < <(config_stems configs/annotation/domains)
# Domains to export: the one given, else all of them.
if [[ $# -ge 1 ]]; then
  domains=("$1")
else
  domains=("${all_stems[@]}")
fi
[[ ${#domains[@]} -gt 0 ]] || fatal "no domains found under configs/annotation/domains/"

rc=0
for d in "${domains[@]}"; do
  cfg="configs/annotation/domains/${d}.yaml"
  [[ -f "$cfg" ]] || { warn "no config: $cfg (skipping)"; rc=1; continue; }
  section "export: $d"
  "$PRAGMATA" annotation export --config "$cfg" --export-id "$d" --base-dir "$DATA_DIR" \
    --include-discarded \
    || { warn "export failed: $d"; rc=1; }
done

# Published-tree guard. Any directory here that isn't a domain stem gets shipped by
# eval-push and read as an extra domain downstream, which is silent and wrong (it once
# double-counted a domain in another box's IAA aggregates). warn + rc=1 rather than fatal,
# matching the per-domain failure path: daily.sh tolerates export failures so that logging
# still runs. Note a push is additive, so this bounds the damage to one cycle — a stray
# already in the container has to be deleted deliberately.
declare -A is_domain=()
for s in "${all_stems[@]}"; do is_domain["$s"]=1; done
for dir in "$DATA_DIR"/annotation/exports/*/; do
  [[ -d "$dir" ]] || continue
  name="$(basename "$dir")"
  [[ -n "${is_domain[$name]:-}" ]] || {
    warn "stray dir in exports tree: $name — not a domain config; eval-push would publish it as a domain"
    rc=1
  }
done

exit "$rc"
