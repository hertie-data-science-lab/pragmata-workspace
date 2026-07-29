#!/usr/bin/env python3
"""The two human-annotation tables: label agreement, and annotation operations.

`annotation_label_summary` — programme x task x label. Agreement comes from each
programme's `iaa/report.json`, prevalence from the export CSVs.

**Why prevalence is split by population.** pragmata computes IAA over CALIBRATION
rows only (`iaa_runner.py:_filter_rows` keeps `calibration == True` and submitted;
"IAA is only meaningful on overlapped records"). Calibration items are
deliberately-selected overlap items, so their label prevalence is not the corpus's.
Reporting one `n_true` beside alpha would silently put two populations in one row, and
a figure using it as a base rate would be wrong. Both are therefore emitted, named.

`annotation_operations` — programme x task x dataset. Counts, discards and cadence
from the latest `logs/annotation/log.jsonl` snapshot; retrieval panel completeness
from it too.

**Why the gap columns come from the log and not the export.** The export CSVs cannot
supply them: `created_at` is the *record's* `updated_at`, not the response timestamp,
so every annotator on one record shares an identical value. `log.py` reads per-response
timestamps from the Argilla REST API instead, session-guarded. Those live at
programme x task grain, not per dataset, so they appear on the `all` row only rather
than being duplicated at a grain they do not have.

Usage:
  scripts/eval/annotation_tables.py
  scripts/eval/annotation_tables.py --snapshot N   # use the Nth-from-last snapshot
"""

from __future__ import annotations

import argparse
import json
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
    # Agreement — calibration items only, by construction.
    "alpha",
    "alpha_ci_low",
    "alpha_ci_high",
    "pct_agree",
    "n_annotators",
    "n_items_calibration",
    # Prevalence, per population. n_items here are annotator-response rows.
    "n_true_calibration",
    "n_rows_calibration",
    "prevalence_calibration",
    "n_true_production",
    "n_rows_production",
    "prevalence_production",
    # alpha = 1 - Do/De, so at zero variance De is 0 and alpha is undefined; pragmata
    # returns 1.0 by convention. This flags that case so a perfect-looking alpha is not
    # read as evidence of reliability.
    "degenerate_calibration",
    "status",
]

OPS_COLUMNS = [
    "programme",
    "task",
    "dataset",
    "n_curated",
    "n_live",
    "n_completed",
    "n_pending",
    "n_submitted_responses",
    "n_annotators",
    "n_discarded",
    "discard_rate",
    "discard_reason_unspecified",
    "discard_reason_invalid_or_unrealistic",
    "discard_reason_unclear",
    "discard_reason_outside_reviewer_expertise",
    # Cadence: programme x task grain, so populated on the `all` row only.
    "median_gap_s",
    "mean_gap_s",
    "gap_p25_s",
    "gap_p75_s",
    "n_gaps_used",
    "session_gap_threshold_s",
    # Retrieval panel completeness: also programme-level, `all` row only.
    "n_panels",
    "n_panels_with_responses",
    "n_panels_complete",
    "gap_grain",
]

DISCARD_REASONS = {
    "unspecified": "discard_reason_unspecified",
    "invalid_or_unrealistic": "discard_reason_invalid_or_unrealistic",
    "unclear": "discard_reason_unclear",
    "outside_reviewer_expertise": "discard_reason_outside_reviewer_expertise",
}


def latest_snapshot(nth: int) -> dict:
    """The Nth-from-last snapshot in logs/annotation/log.jsonl (1 = last).

    Asserts the snapshot actually carries the pooled gap statistics. They were added to
    log.py additively, without a SNAPSHOT_SCHEMA_VERSION bump (the version guard is for
    incompatible changes, and bumping would strand every existing entry for
    report_tables.py). The cost of that choice is that an older snapshot would sail
    through and emit the whole cadence block blank, which reads as "no cadence data"
    rather than "wrong snapshot" - so it is checked explicitly here instead.
    """
    path = ws.LOGS_DIR / "log.jsonl"
    if not path.exists():
        raise SystemExit(f"no snapshot log at {path} - run `make log` first.")
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    if nth < 1:
        # lines[-0] is lines[0], i.e. the oldest snapshot - the opposite of "latest".
        raise SystemExit(f"--snapshot counts back from the last and must be >= 1, got {nth}.")
    if nth > len(lines):
        raise SystemExit(f"only {len(lines)} snapshots available, asked for {nth} from last.")
    snapshot = json.loads(lines[-nth])
    timing = (((snapshot.get("total") or {}).get("timing") or {}).get("per_annotator")) or {}
    if "pooled_mean_gap_s" not in timing:
        raise SystemExit(
            f"snapshot {snapshot.get('run_at')} predates the pooled gap statistics, so the "
            f"cadence columns would come out blank. Re-run `make log` and try again."
        )
    return snapshot


def label_rows(exports: Path, programme: str) -> list[dict]:
    """One row per task x label for a programme."""
    agreement = ec.load_iaa(exports, programme)
    rows = []
    for task in ec.TASKS:
        frame = ec.submitted(ec.read_task(exports, programme, task))
        calibration = ec.keep_calibration_rows(frame)
        production = ec.drop_calibration_rows(frame)
        for label in ec.LABELS[task]:
            row = {"programme": programme, "task": task, "label": label}
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
                row["n_annotators"] = stats.get("n_annotators", "")
                row["n_items_calibration"] = stats.get("n_items", "")
                # A null alpha is not a low alpha: it means the overlap was insufficient
                # to compute one at all.
                row["status"] = "ok" if stats.get("alpha") is not None else "no_alpha_insufficient_overlap"
            else:
                # No overlap means no alpha — not an alpha of zero.
                row["status"] = "no_agreement_no_overlap" if not frame.empty else "no_data"

            for name, subset in (("calibration", calibration), ("production", production)):
                if subset.empty or label not in subset.columns:
                    row[f"n_true_{name}"] = 0
                    row[f"n_rows_{name}"] = 0
                    continue
                n_rows = int(subset[label].notna().sum())
                n_true = int(subset[label].astype(float).fillna(0).sum())
                row[f"n_true_{name}"] = n_true
                row[f"n_rows_{name}"] = n_rows
                if n_rows:
                    row[f"prevalence_{name}"] = f"{n_true / n_rows:.4f}"
                if name == "calibration":
                    row["degenerate_calibration"] = n_rows > 0 and n_true in (0, n_rows)
            rows.append(row)
    return rows


def curated_counts() -> dict[str, int]:
    """Queries per programme in the curated corpus that was imported to Argilla."""
    counts = {}
    for path in sorted(ws.OUT_DIR.glob(f"*{ec.CURATED_SUFFIX}")):
        programme = path.name.removesuffix(ec.CURATED_SUFFIX)
        counts[programme] = sum(1 for line in path.open() if line.strip())
    return counts


def panel_counts(exports: Path, programme: str) -> tuple[int, int, int]:
    """(n_panels, n_panels_with_responses, n_panels_complete) for a programme.

    The totals come from the export's own ``completeness_summary``, not from counting
    record_uuids in the rows: rows only cover panels that received a submitted response,
    so a row-derived total undercounts the denominator and reports 0 panels for
    zentrum-fuer-datenmanagement, which has 70 imported and none annotated. The
    row-derived figure is still useful, so it ships beside the total under its own name.

    Deliberately not the log snapshot's completeness block either, even though it carries
    the same two numbers: that describes whatever live Argilla state `make log` last
    captured, which is not guaranteed to be the moment the export was frozen. Reading the
    export keeps these equal to policy_grid.csv's columns of the same name.
    """
    n_panels, n_complete = ec.panel_totals(exports, programme)
    frame = ec.submitted(ec.read_task(exports, programme, "retrieval"))
    return n_panels, (0 if frame.empty else ec.n_queries(frame)), n_complete


def ops_rows(snapshot: dict, programme: str, curated: dict[str, int], exports: Path) -> list[dict]:
    """Rows for one programme: production, calibration, and the `all` roll-up."""
    domain = (snapshot.get("domains") or {}).get(programme)
    if domain is None:
        return [
            {"programme": programme, "task": task, "dataset": dataset, "gap_grain": "programme_task"}
            for task in ec.TASKS
            for dataset in ("production", "calibration", "all")
        ]

    n_panels, n_panels_with_responses, n_panels_complete = panel_counts(exports, programme)
    rows = []
    for task in ec.TASKS:
        block = (domain.get("tasks") or {}).get(task) or {}
        counts = block.get("count") or {}
        timing = ((block.get("timing") or {}).get("per_annotator")) or {}
        discards = ((block.get("labels") or {}).get("discards")) or {}

        for dataset in ("production", "calibration", "all"):
            source = counts if dataset == "all" else (counts.get(dataset) or {})
            row = {
                "programme": programme,
                "task": task,
                "dataset": dataset,
                "n_live": source.get("total_records", ""),
                "n_completed": source.get("completed_records", ""),
                "n_pending": source.get("pending_records", ""),
                "n_submitted_responses": source.get("submitted_responses", ""),
                "gap_grain": "programme_task",
            }
            if dataset == "all":
                # Everything below exists only at programme x task (or programme) grain;
                # duplicating it onto the dataset rows would imply a split that the
                # underlying data does not have.
                row["n_curated"] = curated.get(programme, "")
                row["n_annotators"] = counts.get("n_annotators", "")
                row["n_discarded"] = discards.get("n_discarded", "")
                row["discard_rate"] = discards.get("discard_rate", "")
                for reason, column in DISCARD_REASONS.items():
                    row[column] = (discards.get("by_reason") or {}).get(reason, 0)
                row["median_gap_s"] = timing.get("pooled_median_active_gap_s", "")
                row["mean_gap_s"] = timing.get("pooled_mean_gap_s", "")
                row["gap_p25_s"] = timing.get("pooled_gap_p25_s", "")
                row["gap_p75_s"] = timing.get("pooled_gap_p75_s", "")
                row["n_gaps_used"] = timing.get("n_gaps_used", "")
                row["session_gap_threshold_s"] = snapshot.get("session_gap_threshold_s", "")
                if ec.has_subrows(task):
                    row["n_panels"] = n_panels
                    row["n_panels_with_responses"] = n_panels_with_responses
                    row["n_panels_complete"] = n_panels_complete
            rows.append(row)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ec.add_common_args(ap)
    ap.add_argument("--snapshot", type=int, default=1,
                    help="Which snapshot to read, counting back from the last (default 1).")
    args = ap.parse_args()

    snapshot = latest_snapshot(args.snapshot)
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
            caveats=[
                "alpha/pct_agree/n_items_calibration describe CALIBRATION rows only — "
                "pragmata computes IAA over overlapped rows and drops production.",
                "Prevalence is reported separately for each population; there is no "
                "single n_true, because alpha's population is not the corpus's.",
                "n_rows_* are annotator-response rows, so a record annotated by three "
                "people contributes three.",
                "degenerate_calibration=True marks a label with no variance in the "
                "calibration rows. alpha = 1 - Do/De is undefined there and pragmata "
                "returns 1.0 by convention, so those 1.0s are not evidence of "
                "reliability. status=no_alpha_insufficient_overlap means no alpha could "
                "be computed at all, which is not the same as a low one.",
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
            snapshot_schema_version=snapshot.get("schema_version"),
            grain="programme x task x dataset (production | calibration | all)",
            caveats=[
                "Cadence, discards, annotator counts, n_curated and panel completeness "
                "exist only at programme x task (or programme) grain, so they are on the "
                "`all` row; the dataset rows carry the count columns only.",
                "n_panels/n_panels_complete come from the frozen export, not the log "
                "snapshot's completeness block, so they match policy_grid.csv's columns "
                "of the same name rather than a possibly-later live Argilla state.",
                "Gaps are inter-submission spacing per annotator from the Argilla REST "
                "API, pooled, with gaps above session_gap_threshold_s excluded as breaks. "
                "The export CSVs cannot supply this: created_at is the record's "
                "updated_at, identical across a record's annotators.",
                "n_curated counts curated queries per programme; each query fans out into "
                "all three tasks, so it repeats across a programme's task rows.",
            ],
        ),
    )
    print(f"wrote annotation_operations.csv ({len(ops)} rows)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
