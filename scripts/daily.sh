#!/bin/bash
# scripts/daily.sh
#
# Nightly annotation analysis. Chains three independently-runnable steps:
#
#   1. export.sh              annotations (incl. discarded) -> data/annotation/exports/<domain>/  (overwrite per domain)
#   2. monitor.py --use-export   counts + IAA + cadence + label/discard stats -> append runs/annotation/monitor.jsonl
#   3. report_tables.py       latest snapshot -> reports/annotation/<date>.md             (data tables)
#   4. plot_summary.py        latest snapshot -> reports/annotation/<date>/*.png          (plots; best-effort)
#
# A failed export does NOT abort the run: monitor reuses whatever CSVs exist and
# IAA degrades gracefully, so counts/cadence + the tables still get produced.
# Backup is a SEPARATE, on-demand workflow and is deliberately not run here.

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
cd_root
require_env ARGILLA_API_URL ARGILLA_API_KEY
mkdir -p reports/annotation runs/annotation/runs

section "export"
bash scripts/annotation/export.sh || warn "export had failures; monitor will reuse whatever CSVs exist"

section "monitor"
"$PY" scripts/annotation/monitor.py --use-export || fatal "monitor failed — no snapshot, skipping tables"

section "report-tables"
"$PY" scripts/annotation/report_tables.py || fatal "report-tables failed"

section "plots"
"$PY" scripts/annotation/plot_summary.py || warn "plots skipped (matplotlib missing or no data); tables still produced"
