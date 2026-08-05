#!/bin/bash
# scripts/annotation/export.sh [domain]
#
# Exports current submitted annotations from Argilla to flat per-task CSVs via
# pragmata's native `annotation export`. With no argument it exports every
# domain; pass a domain stem to export just one.
#
# Artifacts land under data/annotation/exports/ (gitignored), keyed by export-id = domain,
# so each run overwrites that domain's latest snapshot. This is the durable counterpart to
# log.py, which runs its own throwaway export into a temp tree for IAA + label stats.
#
# Only domain stems may live under data/annotation/exports/ (plus the .gitkeep marker):
# transfer-push publishes that tree and consumers glob exports/*/ as the domain list, so a
# stray dir arrives as an extra domain. The guard below warns on one.
#
# Exported WITH --include-discarded so discard rows (response_status=discarded,
# discard_reason) reach log.py's discard stats. CONTRACT: any submitted-only consumer must
# filter response_status == "submitted" (IAA already does); label/constraint columns are
# null on discarded rows.
#
# Every export is then pseudonymised in place (pseudonymize_export.py): pragmata writes the
# Argilla username into annotator_id, and the usernames here are real names. The rewrite is
# part of exporting, not an optional extra — the published tree must never hold names.
#
# Uses $PRAGMATA, which must resolve to the same tree the data was imported with. For a
# non-standard export, call `pragmata annotation export` directly with a --base-dir
# outside data/ or a domain --export-id — a non-domain export-id under $DATA_DIR lands in
# the published tree.

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
cd_root
require_env ARGILLA_API_URL ARGILLA_API_KEY

# One export or freeze at a time. freeze.sh copies this tree wholesale while export
# rewrites it in place and pseudonymises it afterwards, so an overlap could freeze a
# half-written or still-named tree. Same mechanism as pipeline.sh: flock on a held fd, so
# the kernel releases it however the process dies — no pid file, nothing to clean up.
exec 9>".export.lock"
flock -n 9 || fatal "an export or freeze is already running" 3

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

# Pseudonymise the identities the export just wrote. fatal, not warn: a tree that still
# holds real names must not be reachable by transfer-push, so a failure here has to stop
# the run rather than degrade like a per-domain export failure.
section "pseudonymize"
"$PY" scripts/annotation/pseudonymize_export.py "${domains[@]}" \
  || fatal "pseudonymisation failed — the export tree may still hold real names"

# Published-tree guard. Any directory here that isn't a domain stem gets shipped by
# transfer-push and read as an extra domain downstream — silent and wrong. warn + rc=1 rather
# than fatal, matching the per-domain failure path: daily.sh tolerates export failures so
# that logging still runs.
declare -A is_domain=()
for s in "${all_stems[@]}"; do is_domain["$s"]=1; done
for dir in "$DATA_DIR"/annotation/exports/*/; do
  [[ -d "$dir" ]] || continue
  name="$(basename "$dir")"
  [[ -n "${is_domain[$name]:-}" ]] || {
    warn "stray dir in exports tree: $name — not a domain config; transfer-push would publish it as a domain"
    rc=1
  }
done

exit "$rc"
