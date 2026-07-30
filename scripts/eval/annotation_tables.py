#!/usr/bin/env python3
"""The two human-annotation tables: label agreement, and annotation operations.

`annotation_label_summary` — one row per programme x task x label. Agreement comes
from each programme's `iaa/report.json`; `n_items`/`n_true` are counted over annotated
UNITS after pragmata-style majority consolidation, so they are the same numbers eval
scoring ingests (a record annotated by three people counts once, at its majority value).

Two agreement footnotes travel as columns rather than prose. `n_items_calibration`
records the population alpha is actually computed on — pragmata's IAA keeps only the
calibration overlap ("IAA is only meaningful on overlapped records"), so alpha's n is
typically 30, not n_items. And `degenerate_calibration` flags labels with no variance
in that overlap: alpha = 1 - Do/De is undefined at De = 0 and pragmata returns 1.0 by
convention, so those 1.0s are not evidence of reliability.

`annotation_operations` — programme x task, flat. Counts, discards and cadence from the
latest `logs/annotation/log.jsonl` snapshot; panel totals from the frozen export's own
sidecar so they match `policy_grid.csv` exactly.

**Why the gap columns come from the log and not the export.** The export CSVs cannot
supply them: `created_at` is the *record's* `updated_at`, not the response timestamp,
so every annotator on one record shares an identical value. `log.py` reads per-response
timestamps from the Argilla REST API instead, session-guarded — the same machinery as
the daily report.

Usage:
  scripts/eval/annotation_tables.py
  scripts/eval/annotation_tables.py --snapshot N   # use the Nth-from-last snapshot
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import eval_common as ec  # noqa: E402
import workspace as ws  # noqa: E402

LABEL_COLUMNS = [
    "programme",
    "task",
    "label",
    # Unit grain: annotated units after pragmata-style majority consolidation - the
    # numbers eval scoring ingests.
    "n_items",
    "n_true",
    # Response grain: individual annotator submissions, the daily report's counting. A
    # record annotated by three people contributes three here and one above.
    "n_responses",
    "n_true_responses",
    "n_annotators",
    "pct_agree",
    "alpha",
    "alpha_ci_low",
    "alpha_ci_high",
    # Footnotes as columns: alpha's real population, and the zero-variance convention.
    "n_items_calibration",
    "degenerate_calibration",
    "status",
]

OPS_COLUMNS = [
    "programme",
    "task",
    "n_curated",
    "n_live",
    "n_completed",
    "n_pending",
    "n_submitted_responses",
    # Distinct annotation units with >=1 submitted response (chunks for retrieval,
    # queries otherwise) - the consolidated-unit counterpart of n_submitted_responses.
    "n_units_annotated",
    "n_annotators",
    "n_discarded",
    "discard_rate",
    "discard_reason_unspecified",
    "discard_reason_invalid_or_unrealistic",
    "discard_reason_unclear",
    "discard_reason_outside_reviewer_expertise",
    # Cadence: per-annotator inter-submission gaps, pooled, session-guarded — the same
    # machinery and threshold as the daily report.
    "median_gap_s",
    "mean_gap_s",
    "gap_p25_s",
    "gap_p75_s",
    "n_gaps_used",
    "session_gap_threshold_s",
    # Retrieval panel coverage (blank for the other tasks): imported / with >=1
    # response / STRICT-complete.
    "n_panels",
    "n_panels_with_responses",
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
            row["n_annotators"] = int(frame["annotator_id"].nunique()) if not frame.empty else 0

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
                # A null alpha is not a low alpha: the overlap was insufficient to
                # compute one at all.
                row["status"] = "ok" if stats.get("alpha") is not None else "no_alpha_insufficient_overlap"
            else:
                row["status"] = "no_agreement_no_overlap" if not frame.empty else "no_data"

            if not frame.empty and label in frame.columns:
                row["n_responses"] = int(frame[label].notna().sum())
                row["n_true_responses"] = int(frame[label].astype(float).fillna(0).sum())

            # Degenerate iff the label has no variance in the PAIRABLE overlap (items
            # with >=2 annotators) - the population alpha is computed on. Single-annotated
            # calibration rows can carry variance the overlap does not have, so they are
            # excluded from the test.
            if not calibration.empty and label in calibration.columns:
                keys = list(ec.UNIT_KEYS[task])
                multi = calibration.groupby(keys)["annotator_id"].transform("nunique") >= 2
                overlap = calibration.loc[multi, label].dropna()
                row["degenerate_calibration"] = len(overlap) > 0 and overlap.nunique() == 1
            rows.append(row)
    return rows


def curated_counts() -> dict[str, int]:
    """Queries per programme in the curated corpus that was imported to Argilla."""
    counts = {}
    for path in sorted(ws.OUT_DIR.glob(f"*{ec.CURATED_SUFFIX}")):
        programme = path.name.removesuffix(ec.CURATED_SUFFIX)
        counts[programme] = sum(1 for line in path.open() if line.strip())
    return counts


def units_annotated(exports: Path, programme: str, task: str) -> int:
    """Distinct annotation units with at least one submitted response."""
    frame = ec.submitted(ec.read_task(exports, programme, task))
    if frame.empty:
        return 0
    keys = [k for k in ec.UNIT_KEYS[task] if k in frame.columns]
    return int(frame.groupby(keys).ngroups)


def ops_rows(snapshot: dict, programme: str, curated: dict[str, int], exports: Path) -> list[dict]:
    """One flat row per task for a programme."""
    domain = (snapshot.get("domains") or {}).get(programme)
    if domain is None:
        return [{"programme": programme, "task": task} for task in ec.TASKS]

    n_panels, n_panels_complete = ec.panel_totals(exports, programme)
    retrieval = ec.submitted(ec.read_task(exports, programme, "retrieval"))
    n_panels_responses = ec.n_queries(retrieval)

    rows = []
    for task in ec.TASKS:
        block = (domain.get("tasks") or {}).get(task) or {}
        counts = block.get("count") or {}
        timing = ((block.get("timing") or {}).get("per_annotator")) or {}
        discards = ((block.get("labels") or {}).get("discards")) or {}

        row = {
            "programme": programme,
            "task": task,
            "n_curated": curated.get(programme, ""),
            # n_live counts records in the live Argilla dataset: one per CHUNK for
            # retrieval, one per query otherwise — straight from dataset.progress().
            "n_live": counts.get("total_records", ""),
            "n_completed": counts.get("completed_records", ""),
            "n_pending": counts.get("pending_records", ""),
            "n_submitted_responses": counts.get("submitted_responses", ""),
            "n_units_annotated": units_annotated(exports, programme, task),
            "n_annotators": counts.get("n_annotators", ""),
            "n_discarded": discards.get("n_discarded", ""),
            "discard_rate": discards.get("discard_rate", ""),
            "median_gap_s": timing.get("pooled_median_active_gap_s", ""),
            "mean_gap_s": timing.get("pooled_mean_gap_s", ""),
            "gap_p25_s": timing.get("pooled_gap_p25_s", ""),
            "gap_p75_s": timing.get("pooled_gap_p75_s", ""),
            "n_gaps_used": timing.get("n_gaps_used", ""),
            "session_gap_threshold_s": snapshot.get("session_gap_threshold_s", ""),
        }
        for reason, column in DISCARD_REASONS.items():
            row[column] = (discards.get("by_reason") or {}).get(reason, 0)
        if ec.has_subrows(task):
            row["n_panels"] = n_panels
            row["n_panels_with_responses"] = n_panels_responses
            row["n_panels_complete"] = n_panels_complete
        rows.append(row)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ec.add_common_args(ap)
    ap.add_argument("--snapshot", type=int, default=1,
                    help="Which snapshot to read, counting back from the last (default 1).")
    args = ap.parse_args()

    snapshot = ec.latest_snapshot(args.snapshot)
    curated = curated_counts()
    programmes = ec.programmes(args.exports)
    target_dir = ec.out_dir(args.out_dir)

    labels = [row for programme in programmes for row in label_rows(args.exports, programme)]
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
            caveats=[
                "Two grains, named apart: n_items/n_true count annotated UNITS after "
                "pragmata-style majority consolidation (ties fall back to the first row "
                "in file order) - what eval scoring ingests; n_responses/"
                "n_true_responses count individual annotator submissions - the daily "
                "report's grain. Prevalence rates agree between the two; absolute "
                "numbers differ wherever items were multi-annotated.",
                "alpha/pct_agree are computed on the CALIBRATION overlap only "
                "(n_items_calibration), not on n_items - pragmata's IAA drops "
                "production rows.",
                "degenerate_calibration=True marks a label with no variance in the "
                "PAIRABLE overlap (calibration items with >=2 annotators - the "
                "population alpha is computed on); alpha is undefined there and "
                "pragmata returns 1.0 by convention, so those 1.0s are not evidence "
                "of reliability.",
            ],
        ),
    )
    print(f"wrote annotation_label_summary.csv ({len(labels)} rows)", file=sys.stderr)

    ops = [row for programme in programmes for row in ops_rows(snapshot, programme, curated, args.exports)]
    ws.write_csv(
        target_dir / "annotation_operations.csv",
        ops,
        columns=OPS_COLUMNS,
        prov=ws.provenance(
            script="scripts/eval/annotation_tables.py",
            inputs=[ws.LOGS_DIR / "log.jsonl"] + ec.export_inputs(args.exports, include_iaa=False),
            exports_tree=str(args.exports),
            snapshot_run_at=snapshot.get("run_at"),
            grain="programme x task",
            excluded_programmes=sorted(ec.EXCLUDED_PROGRAMMES),
            caveats=[
                "Gaps are inter-submission spacing per annotator from the Argilla REST "
                "API, pooled, with gaps above session_gap_threshold_s excluded as "
                "breaks - the same machinery as the daily report. The export CSVs "
                "cannot supply this: created_at is the record's updated_at, identical "
                "across a record's annotators.",
                "n_curated counts curated queries per programme; each query fans out "
                "into all three tasks, so it repeats across a programme's task rows. "
                "n_live counts live Argilla records: one per chunk for retrieval, one "
                "per query otherwise.",
                "n_panels (imported) comes from the export's completeness_summary; "
                "n_panels_with_responses is counted off the export rows; they differ "
                "where panels were never annotated.",
            ],
        ),
    )
    print(f"wrote annotation_operations.csv ({len(ops)} rows)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
