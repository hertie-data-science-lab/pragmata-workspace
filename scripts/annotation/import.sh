#!/bin/bash
# scripts/annotation/import.sh <domain>
#
# Imports one domain's combined JSONL into Argilla via pragmata's native
# `annotation import`, after stripping run_bot.py's provenance extras down to
# pragmata's QueryResponsePair schema (the one workspace-specific step).
# Assumes the domain's workspaces already exist (run scripts/annotation/setup.sh <domain>).
#
# For non-standard imports, call `pragmata annotation import` directly.

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
cd_root

[[ $# -eq 1 ]] || fatal "usage: $0 <domain>"
d="$1"
cfg="configs/annotation/domains/${d}.yaml"
combined="data/publikationsbot/${d}_combined.jsonl"
[[ -f "$cfg" ]] || fatal "no config: $cfg"
[[ -s "$combined" ]] || fatal "no combined JSONL: $combined (run build_combined.py first)"

# Strip run_bot.py extras -> {query, answer, chunks, context_set, language}, and each
# chunk down to its four schema fields. Both levels matter: QueryResponsePair AND Chunk
# are extra="forbid", so a chunk carrying run_bot's own `title`/`score` fails validation -
# and the import skips invalid records after writing the valid ones, so a partial import
# is what a missed key costs. Extras stay in the workspace JSONL, where the eval manifest
# reads them; only what Argilla stores is projected here.
clean="$(mktemp --suffix=.jsonl)"
trap 'rm -f "$clean"' EXIT
jq -c '{
  query,
  answer,
  chunks: (.chunks | map({chunk_id, doc_id, chunk_rank, text})),
  context_set,
  language
}' "$combined" > "$clean" \
  || fatal "jq projection failed on $combined"
"$PRAGMATA" annotation import "$clean" --config "$cfg" --base-dir "$DATA_DIR"
