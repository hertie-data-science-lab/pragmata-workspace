#!/bin/bash
# scripts/daily.sh
#
# Nightly annotation analysis. Chains three independently-runnable steps:
#
#   1. export.sh              submitted annotations -> annotation/exports/<domain>/  (overwrite per domain)
#   2. monitor.py --use-export   live counts + IAA + cadence -> append logs/monitor.jsonl
#   3. report_tables.py       latest snapshot -> logs/analysis/<date>.md             (pure data tables)
#
# A failed export does NOT abort the run: monitor reuses whatever CSVs exist and
# IAA degrades gracefully, so counts/cadence + the tables still get produced.
# Backup is a SEPARATE, on-demand workflow and is deliberately not run here.

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
cd_root
require_env ARGILLA_API_URL ARGILLA_API_KEY
mkdir -p logs/analysis logs/runs

section "export"
bash scripts/export.sh || warn "export had failures; monitor will reuse whatever CSVs exist"

section "monitor"
"$PY" scripts/monitor.py --use-export || fatal "monitor failed — no snapshot, skipping tables"

section "report-tables"
"$PY" scripts/report_tables.py || fatal "report-tables failed"
