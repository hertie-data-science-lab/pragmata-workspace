#!/bin/bash
# scripts/export.sh [domain]
#
# Exports current submitted annotations from Argilla to flat per-task CSVs via
# pragmata's native `annotation export`. With no argument it exports every
# domain; pass a domain stem to export just one.
#
# Artifacts land under annotation/ (gitignored), keyed by export-id = domain, so
# each run overwrites that domain's "latest" snapshot. This is the durable
# counterpart to scripts/monitor.py, which runs its own throwaway export
# (export-id=monitor) purely to feed IAA + label-stats — the two don't interfere.
#
# Exported WITH --include-discarded so discard rows (response_status=discarded,
# discard_reason) are available to monitor's discard stats. CONTRACT: any
# submitted-only consumer must filter response_status == "submitted" (IAA already
# does); label/constraint columns are null on discarded rows.
#
# Like the other stage wrappers this uses the installed `pragmata` ($PRAGMATA);
# it must resolve to the same branch the data was imported with (see README
# "Under the hood"). For non-standard exports, call `pragmata annotation export`
# directly.

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
cd_root
require_env ARGILLA_API_URL ARGILLA_API_KEY

# Domains: the one given, else every annotation_configs/*.yaml stem (skip _helpers).
if [[ $# -ge 1 ]]; then
  domains=("$1")
else
  mapfile -t domains < <(cd annotation_configs && for f in *.yaml; do [[ "$f" == _* ]] || echo "${f%.yaml}"; done)
fi
[[ ${#domains[@]} -gt 0 ]] || fatal "no domains found under annotation_configs/"

rc=0
for d in "${domains[@]}"; do
  cfg="annotation_configs/${d}.yaml"
  [[ -f "$cfg" ]] || { warn "no config: $cfg (skipping)"; rc=1; continue; }
  section "export: $d"
  "$PRAGMATA" annotation export --config "$cfg" --export-id "$d" --base-dir "$WORKSPACE_ROOT" \
    --include-discarded \
    || { warn "export failed: $d"; rc=1; }
done
exit "$rc"
