#!/bin/bash
# scripts/import.sh <domain>
#
# Imports one domain's combined JSONL into Argilla via pragmata's native
# `annotation import`. The only workspace-specific step is clean_for_import.sh
# (stripping run_bot.py provenance extras). Assumes the domain's workspaces
# already exist (run scripts/setup.sh <domain> first).
#
# For non-standard imports, call `pragmata annotation import` directly.

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
cd_root

[[ $# -eq 1 ]] || fatal "usage: $0 <domain>"
d="$1"
cfg="annotation_configs/${d}.yaml"
combined="publikationsbot_output/${d}_combined.jsonl"
[[ -f "$cfg" ]] || fatal "no config: $cfg"

clean="/tmp/${d}_combined.clean.jsonl"
scripts/clean_for_import.sh "$combined" > "$clean"   # fatals if combined missing/empty
"$PRAGMATA" annotation import "$clean" --config "$cfg"
