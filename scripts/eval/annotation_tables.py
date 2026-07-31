#!/usr/bin/env python3
"""The two human-annotation tables: label agreement, and annotation operations.

Columns and caveats are defined in `docs/eval-data-dictionary.md`
(`annotation_label_summary.csv`, `annotation_operations.csv`).

Inputs. Agreement comes from each programme's `iaa/report.json`; item counts from the
frozen export CSVs after pragmata-style majority consolidation, so they are the numbers
eval scoring ingests; operational counts, discards and cadence from the pinned
`logs/annotation/log.jsonl` snapshot; panel totals from the export's own
`annotation_export.meta.json`. Production and calibration are pooled throughout.

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
    # Item grain: annotated items after pragmata-style majority consolidation - the
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
    # Item grain: records with >=1 submitted response, consolidated.
    "n_items_annotated",
    "n_annotators",
    "n_responses_discarded",
    "discard_rate",
    "discard_reason_unspecified",
    "discard_reason_invalid_or_unrealistic",
    "discard_reason_unclear",
    "discard_reason_outside_reviewer_expertise",
    # Cadence: per-annotator inter-submission gaps, pooled, session-guarded — the same
    # machinery and threshold as the daily report. The threshold is in the .provenance.json.
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
            n_items, n_true = ec.consolidated_prevalence(frame, task, label)
            row["n_items"] = n_items
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
                keys = list(ec.ITEM_KEYS[task])
                multi = (
                    calibration.groupby(keys)["annotator_id"].transform("nunique") >= 2
                )
                overlap = calibration.loc[multi, label].dropna()
                row["degenerate_calibration"] = (
                    len(overlap) > 0 and overlap.nunique() == 1
                )
            rows.append(row)
    return rows


def items_annotated(exports: Path, programme: str, task: str) -> int:
    """Records with at least one submitted response, i.e. annotated items."""
    frame = ec.submitted(ec.read_task(exports, programme, task))
    if frame.empty:
        return 0
    keys = [k for k in ec.ITEM_KEYS[task] if k in frame.columns]
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
            "n_items_annotated": items_annotated(exports, programme, task),
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
            # alpha and its interval come from the export's own iaa/report.json, which
            # records the resample count but not the seed. Both are carried here so the
            # intervals in this table can be reproduced; the snapshot is not an input to
            # it, only these parameters are.
            iaa=snapshot.get("iaa"),
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
