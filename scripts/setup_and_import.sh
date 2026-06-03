#!/bin/bash
# scripts/setup_and_import.sh <domain>
#
# Thin convenience wiring of pragmata's native annotation CLI for one domain:
#
#   pragmata annotation setup  --users config/users.json --config <cfg>
#   pragmata annotation import <cleaned combined.jsonl>   --config <cfg>
#
# The ONLY workspace-specific step is clean_for_import.sh (strips run_bot.py
# provenance extras so the JSONL matches pragmata's QueryResponsePair schema).
# For anything non-standard, call `pragmata annotation {setup,import}` directly
# with the flags you need — this just covers the common path.

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
cd_root

[[ $# -eq 1 ]] || fatal "usage: $0 <domain>"
d="$1"
cfg="annotation_configs/${d}.yaml"
combined="publikationsbot_output/${d}_combined.jsonl"
[[ -f "$cfg" ]] || fatal "no config: $cfg"

"$PRAGMATA" annotation setup --users config/users.json --config "$cfg"

clean="/tmp/${d}_combined.clean.jsonl"
scripts/clean_for_import.sh "$combined" > "$clean"   # fatals if combined missing/empty
"$PRAGMATA" annotation import "$clean" --config "$cfg"
