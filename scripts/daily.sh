#!/bin/bash
# scripts/daily.sh
#
# Nightly annotation *logging*. Chains two independently-runnable steps:
#
#   1. export.sh           annotations (incl. discarded) -> data/annotation/exports/<domain>/  (overwrite per domain)
#   2. log.py --use-export counts + IAA + cadence + label/discard stats -> append logs/annotation/log.jsonl
#
# A failed export does NOT abort the run: log reuses whatever CSVs exist and
# IAA degrades gracefully, so counts/cadence still get logged.
#
# Reporting (markdown + plots) is a SEPARATE, manual step — run `make report`
# to render reports/annotation/<date>/ (report.md + *.png) from the latest
# snapshot. Backup is likewise on-demand and deliberately not run here.

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
cd_root
require_env ARGILLA_API_URL ARGILLA_API_KEY
mkdir -p logs/annotation

section "export"
bash scripts/annotation/export.sh || warn "export had failures; log will reuse whatever CSVs exist"

section "log"
"$PY" scripts/annotation/log.py --use-export || fatal "log failed — no snapshot appended"
