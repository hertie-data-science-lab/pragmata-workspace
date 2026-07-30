#!/usr/bin/env python3
"""The two human-annotation tables: label agreement, and annotation operations.

`annotation_label_summary` — one row per programme x task x label. Agreement comes
from each programme's `iaa/report.json`; `n_items`/`n_true` are counted over annotated
UNITS after pragmata-style majority consolidation, so they are the same numbers eval
scoring ingests (a record annotated by three people counts once, at its majority value).
`n_items` is the client's column name for that count; the vocabulary is in
`docs/eval-data-dictionary.md`.

Two agreement footnotes travel as columns rather than prose. `n_items_calibration`
records the population alpha is actually computed on — pragmata's IAA keeps only the
calibration overlap ("IAA is only meaningful on overlapped records"), so alpha's n is
typically 30, not n_items. And `degenerate_calibration` flags labels with no variance
in that overlap: alpha = 1 - Do/De is undefined at De = 0 and pragmata returns 1.0 by
convention, so those 1.0s are not evidence of reliability.

`annotation_operations` — programme x task, flat. Counts, discards and cadence from the
pinned `logs/annotation/log.jsonl` snapshot; panel totals from the frozen export's own
sidecar. Production and calibration are POOLED throughout: the operational question is
how much annotation happened, and both kinds cost the same effort.

**Why the gap columns come from the log and not the export.** The export CSVs cannot
supply them: `created_at` is the *record's* `updated_at`, not the response timestamp,
so every annotator on one record shares an identical value. `log.py` reads per-response
timestamps from the Argilla REST API instead, session-guarded — the same machinery as
the daily report.

Usage:
  scripts/eval/annotation_tables.py
  scripts/eval/annotation_tables.py --snapshot-run-at 2026-07-30T02:01:00Z
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import eval_common as ec
import workspace as ws

# The client's requested columns, in their order, plus the two that make alpha readable.
LABEL_COLUMNS = [
    "programme",
    "task",
    "label",
    # Unit grain: annotated units after pragmata-style majority consolidation - the
    # numbers eval scoring ingests.
    "n_items",
    "n_annotators",
    "n_true",
    "pct_agree",
    "alpha",
    "alpha_ci_low",
    "alpha_ci_high",
    # alpha's real population, and the zero-variance convention. A blank alpha means the
    # calibration overlap was insufficient to compute one.
    "n_items_calibration",
    "degenerate_calibration",
]

OPS_COLUMNS = [
    "programme",
    "task",
    # Record grain, from the live Argilla dataset: one record per chunk for retrieval,
    # one per query otherwise.
    "n_records_live",
    "n_records_completed",
    "n_records_pending",
    # Response grain: individual annotator submissions.
    "n_responses_submitted",
    # Unit grain: records with >=1 submitted response, consolidated.
    "n_units_annotated",
    "n_annotators",
    "n_responses_discarded",
    "discard_rate",
    "discard_reason_unspecified",
    "discard_reason_invalid_or_unrealistic",
    "discard_reason_unclear",
    "discard_reason_outside_reviewer_expertise",
    # Cadence: per-annotator inter-submission gaps, pooled, session-guarded — the same
    # machinery and threshold as the daily report. The threshold is in the sidecar.
    "median_gap_s",
    "mean_gap_s",
    "gap_p25_s",
    "gap_p75_s",
    "n_gaps_used",
    # Retrieval panel coverage, blank for the other tasks: imported / STRICT-complete.
    "n_panels",
    "n_panels_complete",
]

DISCARD_REASONS = {
    "unspecified": "discard_reason_unspecified",
    "invalid_or_unrealistic": "discard_reason_invalid_or_unrealistic",
    "unclear": "discard_reason_unclear",
    "outside_reviewer_expertise": "discard_reason_outside_reviewer_expertise",
}


def label_rows(exports: Path, programme: str) -> list[dict]:
    """One row per task x label for a programme."""
    agreement = ec.load_iaa(exports, programme)
    rows = []
    for task in ec.TASKS:
        frame = ec.submitted(ec.read_task(exports, programme, task))
        calibration = ec.keep_calibration_rows(frame)
        for label in ec.LABELS[task]:
            row = {"programme": programme, "task": task, "label": label}
            n_units, n_true = ec.consolidated_prevalence(frame, task, label)
            row["n_items"] = n_units
            row["n_true"] = n_true
            row["n_annotators"] = (
                int(frame["annotator_id"].nunique()) if not frame.empty else 0
            )

            stats = agreement.get((task, label))
            if stats:
                for source, target in (
                    ("alpha", "alpha"),
                    ("ci_lower", "alpha_ci_low"),
                    ("ci_upper", "alpha_ci_high"),
                    ("pct_agreement", "pct_agree"),
                ):
                    if stats.get(source) is not None:
                        row[target] = f"{stats[source]:.6f}"
                row["n_items_calibration"] = stats.get("n_items", "")

            # Degenerate iff the label has no variance in the PAIRABLE overlap (items
            # with >=2 annotators) - the population alpha is computed on. Single-annotated
            # calibration rows can carry variance the overlap does not have, so they are
            # excluded from the test.
            if not calibration.empty and label in calibration.columns:
                keys = list(ec.UNIT_KEYS[task])
                multi = (
                    calibration.groupby(keys)["annotator_id"].transform("nunique") >= 2
                )
                overlap = calibration.loc[multi, label].dropna()
                row["degenerate_calibration"] = (
                    len(overlap) > 0 and overlap.nunique() == 1
                )
            rows.append(row)
    return rows


def units_annotated(exports: Path, programme: str, task: str) -> int:
    """Records with at least one submitted response, i.e. annotated units."""
    frame = ec.submitted(ec.read_task(exports, programme, task))
    if frame.empty:
        return 0
    keys = [k for k in ec.UNIT_KEYS[task] if k in frame.columns]
    return int(frame.groupby(keys).ngroups)


def ops_rows(snapshot: dict, programme: str, exports: Path) -> list[dict]:
    """One flat row per task for a programme."""
    domain = (snapshot.get("domains") or {}).get(programme)
    if domain is None:
        return [{"programme": programme, "task": task} for task in ec.TASKS]

    n_panels, n_panels_complete = ec.panel_totals(exports, programme)

    rows = []
    for task in ec.TASKS:
        block = (domain.get("tasks") or {}).get(task) or {}
        counts = block.get("count") or {}
        timing = ((block.get("timing") or {}).get("per_annotator")) or {}
        discards = ((block.get("labels") or {}).get("discards")) or {}

        row = {
            "programme": programme,
            "task": task,
            "n_records_live": counts.get("total_records", ""),
            "n_records_completed": counts.get("completed_records", ""),
            "n_records_pending": counts.get("pending_records", ""),
            "n_responses_submitted": counts.get("submitted_responses", ""),
            "n_units_annotated": units_annotated(exports, programme, task),
            "n_annotators": counts.get("n_annotators", ""),
            "n_responses_discarded": discards.get("n_discarded", ""),
            "discard_rate": discards.get("discard_rate", ""),
            "median_gap_s": timing.get("pooled_median_active_gap_s", ""),
            "mean_gap_s": timing.get("pooled_mean_gap_s", ""),
            "gap_p25_s": timing.get("pooled_gap_p25_s", ""),
            "gap_p75_s": timing.get("pooled_gap_p75_s", ""),
            "n_gaps_used": timing.get("n_gaps_used", ""),
        }
        for reason, column in DISCARD_REASONS.items():
            row[column] = (discards.get("by_reason") or {}).get(reason, 0)
        if ec.has_subrows(task):
            row["n_panels"] = n_panels
            row["n_panels_complete"] = n_panels_complete
        rows.append(row)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ec.add_common_args(ap)
    ec.add_snapshot_arg(ap)
    args = ap.parse_args()

    snapshot, identity = ws.find_snapshot(args.snapshot_run_at)
    programmes = ec.programmes(args.exports)
    target_dir = ec.out_dir(args.out_dir)

    labels = [
        row for programme in programmes for row in label_rows(args.exports, programme)
    ]
    ws.write_csv(
        target_dir / "annotation_label_summary.csv",
        labels,
        columns=LABEL_COLUMNS,
        prov=ws.provenance(
            script="scripts/eval/annotation_tables.py",
            inputs=ec.export_inputs(args.exports),
            exports_tree=str(args.exports),
            grain="programme x task x label",
            excluded_programmes=sorted(ec.EXCLUDED_PROGRAMMES),
        ),
    )
    print(f"wrote annotation_label_summary.csv ({len(labels)} rows)", file=sys.stderr)

    ops = [
        row
        for programme in programmes
        for row in ops_rows(snapshot, programme, args.exports)
    ]
    ws.write_csv(
        target_dir / "annotation_operations.csv",
        ops,
        columns=OPS_COLUMNS,
        prov=ws.provenance(
            script="scripts/eval/annotation_tables.py",
            # The log itself is not an input hash: it is append-only, so its whole-file
            # digest changes nightly and pins nothing. `snapshot` pins the one line read.
            inputs=ec.export_inputs(args.exports, include_iaa=False),
            snapshot=identity,
            exports_tree=str(args.exports),
            grain="programme x task, production and calibration pooled",
            excluded_programmes=sorted(ec.EXCLUDED_PROGRAMMES),
            session_gap_threshold_s=snapshot.get("session_gap_threshold_s"),
        ),
    )
    print(f"wrote annotation_operations.csv ({len(ops)} rows)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
