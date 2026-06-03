#!/bin/bash
# scripts/import.sh <domain>
#
# Imports one domain's combined JSONL into Argilla via pragmata's native
# `annotation import`, after stripping run_bot.py's provenance extras down to
# pragmata's 5-field QueryResponsePair schema (the one workspace-specific step).
# Assumes the domain's workspaces already exist (run scripts/setup.sh <domain>).
#
# For non-standard imports, call `pragmata annotation import` directly.

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
cd_root

[[ $# -eq 1 ]] || fatal "usage: $0 <domain>"
d="$1"
cfg="annotation_configs/${d}.yaml"
combined="publikationsbot_output/${d}_combined.jsonl"
[[ -f "$cfg" ]] || fatal "no config: $cfg"
[[ -s "$combined" ]] || fatal "no combined JSONL: $combined (run build_combined.py first)"

# Strip run_bot.py extras -> {query, answer, chunks, context_set, language}.
clean="/tmp/${d}_combined.clean.jsonl"
jq -c '{query, answer, chunks, context_set, language}' "$combined" > "$clean" \
  || fatal "jq projection failed on $combined"
"$PRAGMATA" annotation import "$clean" --config "$cfg"
