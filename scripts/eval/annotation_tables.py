#!/usr/bin/env python3
"""The two human-annotation tables: label agreement, and annotation operations.

Columns and caveats are defined in `docs/deliverables-data-dictionary.md`
(`annotation_label_summary.csv`, `annotation_operations.csv`).

Inputs. Agreement is recomputed here from the frozen export CSVs with pragmata's own IAA
functions, at parameters this script sets and records; item counts from the same CSVs
after pragmata-style majority consolidation, so they are the numbers eval scoring
ingests; operational counts, discards and cadence from the pinned
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

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import eval_common as ec
import workspace as ws

# Imported after eval_common, which calls ws.load_env(): the annotation pragmata is a
# pinned install rather than a path shadow, but the env has to be loaded before it is
# resolved, exactly as scripts/annotation/log.py does it.
#
# The underscore names are reached into deliberately. run_iaa() is the public entry point,
# but it WRITES its report into the export directory - here the read-only frozen tree - and
# what it does before writing is exactly the pieces imported below. Calling them directly
# reproduces its numbers without reproducing any of its logic. If a pragmata bump moves
# them, this import fails loudly, which is the right failure.
from pragmata.core.annotation.export_runner import TASK_EXPORT_ROW
from pragmata.core.annotation.iaa import (
    bootstrap_alpha,
    krippendorff_alpha_nominal,
    percentage_agreement,
)
from pragmata.core.annotation.iaa_runner import (
    TASK_LABELS,
    _filter_rows,
    _or_none,
    _pivot_task,
)
from pragmata.core.csv_io import read_csv
from pragmata.core.schemas.annotation_task import Task

# Bootstrap parameters for the alpha intervals. Set here rather than inherited, because
# they are the whole difference between an interval that can be re-derived and one that
# cannot: the export's own iaa/report.json records a resample count but no seed, so its
# bounds are unreproducible. These three go into the .provenance.json beside the CSV.
IAA_RESAMPLES = 1000
IAA_SEED = 0
IAA_CI_LEVEL = 0.95

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


def agreement_stats(exports: Path, programme: str) -> dict[tuple[str, str], dict]:
    """(task, label) -> agreement, recomputed from the export CSVs at report-build time.

    NOT read out of the export's ``iaa/report.json``. That file is written once, when the
    export is cut, at whatever parameters that run used and with no seed recorded — so
    its interval cannot be re-derived, and it goes stale in place, because a re-export
    overwrites the CSVs beside it without re-running agreement (in the frozen 2026-07-30
    tree the reports are 10.5h older than the rows they describe). Recomputing here fixes
    both: the numbers are of the CSVs this report actually reads, and the parameters that
    produced them are this script's own, recorded in the .provenance.json.

    The statistic is pragmata's, not a reimplementation: the same calibration+submitted
    row filter, the same coder x item pivot, the same alpha. Verified against the
    published 2026-07-31 table - all 91 (programme, task, label) cells reproduce alpha,
    pct_agreement and n_items exactly. Only the bounds move, because the published ones
    were the export's unseeded 200-resample run.

    Every entry describes CALIBRATION items only: ``_filter_rows`` keeps
    ``calibration == True`` submitted rows and drops production, because agreement is
    only meaningful on overlapped records. So an alpha attached to a production metric
    is evidence about the labelling scheme, not about those specific rows.
    """
    stats: dict[tuple[str, str], dict] = {}
    for task in ec.TASKS:
        path = exports / programme / f"{task}.csv"
        if not path.exists() or path.stat().st_size == 0:
            continue
        pragmata_task = Task(task)
        rows = _filter_rows(read_csv(path, TASK_EXPORT_ROW[pragmata_task]))
        if not rows:
            # No calibration overlap at all: leave the (task, label) keys absent so the
            # alpha columns come out blank rather than as a computed-looking zero.
            continue
        labels = TASK_LABELS[pragmata_task]
        matrices, annotators, _ = _pivot_task(rows, pragmata_task, labels)
        for label in labels:
            data = matrices[label]
            ci_lower, ci_upper = bootstrap_alpha(
                data, n_resamples=IAA_RESAMPLES, ci=IAA_CI_LEVEL, seed=IAA_SEED
            )
            stats[(task, label)] = {
                "alpha": _or_none(krippendorff_alpha_nominal(data)),
                "ci_lower": _or_none(ci_lower),
                "ci_upper": _or_none(ci_upper),
                # Items alpha is actually computed on: those at least two coders saw.
                "n_items": int(np.sum(np.sum(~np.isnan(data), axis=0) >= 2)),
                "n_annotators": len(annotators),
                "pct_agreement": _or_none(percentage_agreement(data)),
            }
    return stats


def label_rows(
    exports: Path, programme: str, agreement: dict[tuple[str, str], dict]
) -> list[dict]:
    """One row per task x label for a programme.

    Takes the agreement map rather than computing it: the bootstrap is the one expensive
    step in this script, so it stays visible at the call site rather than hiding inside a
    row builder.
    """
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
            # excluded from the test. Left BLANK when there is no pairable overlap at
            # all: False would read as "measured, and not degenerate", when in fact
            # nothing was measured - which is what the blank alpha beside it already says.
            if not calibration.empty:
                keys = list(ec.ITEM_KEYS[task])
                multi = (
                    calibration.groupby(keys)["annotator_id"].transform("nunique") >= 2
                )
                overlap = calibration.loc[multi, label].dropna()
                if len(overlap) > 0:
                    row["degenerate_calibration"] = overlap.nunique() == 1
            rows.append(row)
    return rows


def items_annotated(exports: Path, programme: str, task: str) -> int:
    """Records with at least one submitted response, i.e. annotated items."""
    frame = ec.submitted(ec.read_task(exports, programme, task))
    if frame.empty:
        return 0
    return int(frame.groupby(list(ec.ITEM_KEYS[task])).ngroups)


def ops_rows(snapshot: dict, programme: str, exports: Path) -> list[dict]:
    """One flat row per task for a programme.

    The export-derived columns are filled whether or not the snapshot carries a block for
    this programme. They are counted off the frozen CSVs and the export's own meta file,
    so a gap in the log must not blank numbers that never came from the log; only the
    snapshot's own columns go blank.
    """
    domain = (snapshot.get("domains") or {}).get(programme) or {}
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
            # Blank rather than 0 when the snapshot has nothing for this programme x
            # task: "nobody discarded anything" and "nothing was recorded" are different
            # claims, and n_responses_discarded beside them is already blank.
            row[column] = (
                (discards.get("by_reason") or {}).get(reason, 0) if block else ""
            )
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
    target_dir = ws.stage_report_dir("eval", args.out_dir)

    labels = [
        row
        for programme in programmes
        for row in label_rows(
            args.exports, programme, agreement_stats(args.exports, programme)
        )
    ]
    ws.write_csv(
        target_dir / "annotation_label_summary.csv",
        labels,
        columns=LABEL_COLUMNS,
        prov=ws.provenance(
            script="scripts/eval/annotation_tables.py",
            # CSVs only: the export's iaa/report.json is no longer read, so hashing it
            # would pin a file this table does not derive from.
            inputs=ec.export_inputs(args.exports, include_iaa=False),
            exports_tree=str(args.exports),
            grain="programme x task x label",
            excluded_programmes=sorted(ec.EXCLUDED_PROGRAMMES),
            # The parameters behind alpha_ci_low/high, and this script's own: agreement is
            # recomputed from the CSVs above, not copied from the export's iaa/report.json
            # (which records no seed) and not taken from the log snapshot (whose pooled
            # IAA is a different population). Seeded, so the bounds re-derive exactly.
            iaa={
                "source": "recomputed from the export CSVs by this script",
                "implementation": "pragmata.core.annotation.iaa (the pinned install)",
                "n_bootstrap_resamples": IAA_RESAMPLES,
                "seed": IAA_SEED,
                "ci_level": IAA_CI_LEVEL,
                "population": "submitted calibration responses, per programme x task",
            },
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
