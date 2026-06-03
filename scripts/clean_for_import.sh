#!/bin/bash
# scripts/clean_for_import.sh <input-jsonl>
#
# Projects a run_bot.py-emitted JSONL down to the 5-field QueryResponsePair
# schema that pragmata's annotation import accepts (extra="forbid"):
#
#   query, answer, chunks, context_set, language
#
# Removed (run_bot.py provenance extras that would otherwise be rejected):
#   query_id, domain, role, topic, intent, task, difficulty, format,
#   spec_stem, retried
#
# Cleaned JSONL goes to stdout; stats to stderr so a downstream pipeline can
# consume the data unaffected. This is a stdin/stdout filter, so it does NOT
# cd to the workspace root — the input path is resolved against your cwd.
#
# Usage:
#   scripts/clean_for_import.sh publikationsbot_output/<domain>_combined.jsonl > /tmp/clean.jsonl

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"   # helpers only; no cd_root (filter)

[[ $# -eq 1 ]] || fatal "usage: $0 <input-jsonl>"
INPUT="$1"
[[ -f "$INPUT" ]] || fatal "$INPUT not found"

input_count="$(wc -l < "$INPUT")"
(( input_count > 0 )) || fatal "$INPUT is empty"

# Buffer to a temp file so we can stream to stdout AND report a count to stderr
# without running jq over the input twice.
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

jq -c '{query, answer, chunks, context_set, language}' "$INPUT" > "$TMP" \
  || fatal "jq projection failed on $INPUT"
output_count="$(wc -l < "$TMP")"

log "$(basename "$INPUT"): $input_count records -> $output_count cleaned (stripped extras)"
cat "$TMP"
