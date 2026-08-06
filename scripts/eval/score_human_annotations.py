#!/usr/bin/env python3
"""Corpus metrics from the human-labelled annotations.

Columns and caveats are defined in `docs/data-dictionary.md`
(`eval_metric_estimates.csv`). Its twin, `score_synthetic_predictions.py`, scores the same
metric taxonomy on an evaluator model's predictions and reuses this module's row builders, so
the two CSVs stay column-for-column comparable.

Pools the frozen canonical export across programmes, runs `pragmata eval score` once per
task, and collects every per-metric estimate into one tidy CSV. Pooling is also what makes
retrieval usable: panel completeness is uneven enough that several programmes contribute
too few complete panels to report on their own.

**Why it filters here and passes --path.** `eval score --export-id` would read the export
directly, but applies no completeness policy of its own, so incomplete retrieval panels
would silently produce @K metrics over partial chunk sets. The filter is therefore applied
here, explicitly and in version control: submitted responses only, and for retrieval
`--skip-incomplete-panels` (pragmata #305), which records the drop as `n_panels_skipped`.
Calibration items stay in — majority consolidation coalesces them exactly as when
training; `--exclude-calibration` drops wholly-calibration queries at query grain, which
keeps the calibration chunks of mixed retrieval panels (see `policy_name`).

Usage:
  scripts/eval/score_human_annotations.py                        # the reportable policy
  scripts/eval/score_human_annotations.py --exclude-calibration  # production-only run
  scripts/eval/score_human_annotations.py --all-panels           # no completeness filter
  scripts/eval/score_human_annotations.py --allow-dirty          # dirty/unpinned pragmata
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import eval_common as ec
import workspace as ws

# Which label(s) each metric is computed from, so the right alpha can be attached.
# Derived from pragmata's core/eval/metrics.py formulas and the *_METRIC_LABELS maps
# in core/eval/grouping.py. Metrics resting on two labels get the weaker alpha of the
# two, which is the conservative read.
METRIC_LABELS: dict[str, tuple[str, ...]] = {
    # retrieval
    "topical_precision_at_k": ("topically_relevant",),
    "sufficiency_hit_at_k": ("evidence_sufficient",),
    "sufficiency_rate_at_k": ("evidence_sufficient",),
    "misleading_context_rate_at_k": ("misleading",),
    "mean_reciprocal_rank_at_k": ("topically_relevant",),
    "ndcg_at_k": ("topically_relevant", "evidence_sufficient"),
    # grounding
    "grounding_presence_rate": ("support_present",),
    "unsupported_claim_rate": ("unsupported_claim_present",),
    "contradiction_rate": ("contradicted_claim_present",),
    "citation_presence_rate": ("source_cited",),
    "conditional_fabrication_rate": ("source_cited", "fabricated_source"),
    # generation
    "proper_action_rate": ("proper_action",),
    "on_topic_rate": ("response_on_topic",),
    "helpfulness_rate": ("helpful",),
    "incompleteness_rate": ("incomplete",),
    "unsafe_content_rate": ("unsafe_content",),
}

# Report keys that are not metrics.
NON_METRIC_KEYS = {
    "task",
    "source",
    "notes",
    "created_at",
    "n_examples",
    "ci_level",
    "top_k",
    "n_panels_skipped",
}

COLUMNS = [
    "task",
    "metric",
    "point",
    "ci_low",
    "ci_high",
    "method",
    "n",
    "n_examples",
    "ci_level",
    "top_k",
    # Incomplete retrieval panels dropped by pragmata before scoring; n counts the
    # completed-panel population, not the corpus.
    "n_panels_skipped",
    "policy",
    # Reliability of the labels the metric rests on — calibration-only, by definition
    # of how pragmata computes IAA.
    "source_labels",
    "alpha_min",
    "alpha_min_label",
    "alpha_n_items",
    "alpha_min_degenerate",
    "status",
]


# The reportable policy: calibration kept in, complete retrieval panels only.
DEFAULT_POLICY = "calib-complete"

# Where the pooled, filtered CSVs handed to `eval score --path` are staged. Deliberately
# NOT under data/eval/: that is pragmata's own eval tool tree (see data/README.md), and
# the ownership invariant in docs/report-deliverables.md is that a tool tree holds only what
# that tool produced. These are workspace-produced inputs TO the tool, so they get a
# workspace-owned sibling; pragmata still writes its reports to data/eval/scores/.
FILTERED_ROOT = ws.DATA_DIR / "eval-inputs"


def policy_name(args) -> str:
    """Short slug naming the filter combination, used in paths and in every row.

    ``prod-*`` is shorthand, not a guarantee of a calibration-free population. The filter
    it names works at QUERY grain (see ec.drop_calibration_queries), so it removes only
    wholly-calibration queries; the calibration CHUNKS of a mixed retrieval panel stay in,
    because dropping them would break their panel's @K denominators. In the frozen
    2026-07-30 tree that leaves 476 of 1836 submitted retrieval rows — roughly a quarter —
    calibration even under ``prod-*``. Grounding and generation, one record per query, are
    genuinely calibration-free there.

    The slugs are published schema (the `policy` column, and the filename suffix), so they
    are described rather than renamed.
    """
    return "-".join(
        [
            "prod" if args.exclude_calibration else "calib",
            "allpanels" if args.all_panels else "complete",
        ]
    )


def filtered_frame(exports: Path, programme: str, task: str, args):
    """Apply the scoring policy to one programme x task frame.

    Returns ``(raw, filtered)``. The raw frame comes back too so a caller seeing an
    empty result can tell "this programme was never annotated" from "the filters
    removed everything" without re-reading and re-parsing the CSV.
    """
    raw = ec.read_task(exports, programme, task)
    frame = ec.submitted(raw)
    if args.exclude_calibration:
        frame = ec.drop_calibration_queries(frame)
    return raw, frame


def attach_alpha(row: dict, metric: str, task: str, alphas: dict) -> None:
    """Attach the weakest alpha among the labels the metric is computed from."""
    labels = METRIC_LABELS.get(metric, ())
    row["source_labels"] = ";".join(labels)
    candidates = [
        (alphas[(task, lab)]["alpha"], lab, alphas[(task, lab)])
        for lab in labels
        if (task, lab) in alphas and alphas[(task, lab)].get("alpha") is not None
    ]
    if not candidates:
        return
    alpha, label, stats = min(candidates, key=lambda c: c[0])
    row["alpha_min"] = f"{alpha:.4f}"
    row["alpha_min_label"] = label
    # No interval on alpha_min: the pooled alpha's bootstrap CI is resampled separately
    # from the metric's, so putting the two side by side invited reading them as one
    # uncertainty budget. The point estimate plus the degeneracy flag is what the metric
    # rows need; the resample count and its determinism are in the data dictionary.
    row["alpha_n_items"] = stats.get("n_items", "")
    # alpha = 1 - Do/De is undefined at De = 0 (the label never varies in the pooled
    # calibration items) and pragmata returns 1.0 by convention. Without this flag,
    # grounding_presence_rate's alpha_min of 1.0 reads as perfect measured reliability.
    row["alpha_min_degenerate"] = stats.get("expected_disagreement") == 0


def run_score(pin, csv_path: Path, task: str, score_id: str, args) -> Path:
    """Invoke `pragmata eval score` on a filtered CSV; return the report JSON path.

    The subprocess mechanics - PYTHONPATH shadow, base_dir, the stale-report mtime
    guard - live in ec.run_score_cli, shared with the synthetic scorer. One venv
    serves both pragmata halves: pandera, the only thing the eval module needs beyond
    the annotation side, is a workspace dependency in pyproject.toml.
    """
    return ec.run_score_cli(
        pin,
        ["--path", str(csv_path)],
        task,
        score_id,
        args,
        context=f"{score_id}/{task}",
    )


def rows_from_report(report: dict, task: str, policy: str, alphas: dict) -> list[dict]:
    """One row per metric in a score report."""
    rows = []
    for metric, value in report.items():
        if metric in NON_METRIC_KEYS:
            continue
        if value is None:
            # conditional_fabrication_rate is None when no query cited a source, i.e.
            # the conditional is undefined rather than zero. source_labels is still
            # filled: which labels the metric WOULD rest on is a property of the metric,
            # not of this run, and empty_rows() fills it on its n=0 rows for the same
            # reason. Blanking it here made one undefined row look different in kind.
            rows.append(
                {
                    "task": task,
                    "metric": metric,
                    "policy": policy,
                    "n_examples": report.get("n_examples", ""),
                    "ci_level": report.get("ci_level", ""),
                    "status": "undefined_no_denominator",
                    "source_labels": ";".join(METRIC_LABELS.get(metric, ())),
                }
            )
            continue
        row = {
            "task": task,
            "metric": metric,
            "point": f"{value['point']:.6f}",
            "ci_low": f"{value['ci_lower']:.6f}",
            "ci_high": f"{value['ci_upper']:.6f}",
            "method": value["method"],
            "n": value["n"],
            "n_examples": report.get("n_examples", ""),
            "ci_level": report.get("ci_level", ""),
            "top_k": report.get("top_k", ""),
            "n_panels_skipped": report.get("n_panels_skipped", ""),
            "policy": policy,
            "status": "ok",
        }
        attach_alpha(row, metric, task, alphas)
        rows.append(row)
    return rows


def empty_rows(task: str, policy: str, status: str) -> list[dict]:
    """Explicit n=0 rows so a task cannot silently vanish from the report."""
    return [
        {
            "task": task,
            "metric": metric,
            "policy": policy,
            "n": 0,
            "n_examples": 0,
            "status": status,
            "source_labels": ";".join(labels),
        }
        for metric, labels in METRIC_LABELS.items()
        if any(lab in ec.LABELS[task] for lab in labels)
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ec.add_common_args(ap)
    ec.add_snapshot_arg(ap)
    ap.add_argument(
        "--exclude-calibration",
        action="store_true",
        help="Comparison run: drop wholly-calibration queries (default: keep them).",
    )
    ap.add_argument(
        "--all-panels",
        action="store_true",
        help="Score every retrieval panel, not just complete ones.",
    )
    ap.add_argument(
        "--ci", type=float, default=0.95, help="Confidence level (default 0.95)."
    )
    ap.add_argument(
        "--n-resamples",
        type=int,
        default=1000,
        help="Bootstrap iterations for the continuous retrieval metrics.",
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Bootstrap RNG seed; fixed so intervals are reproducible.",
    )
    ap.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Score even if the pragmata pin is dirty or names no commit at all.",
    )
    args = ap.parse_args()

    pin = ws.eval_pragmata()
    ec.require_clean_eval_pin(pin, allow_dirty=args.allow_dirty)

    policy = policy_name(args)
    filtered_root = FILTERED_ROOT / policy
    rows: list[dict] = []

    # Pooled alpha, because the metrics are pooled: every domain's calibration items go
    # into one reliability matrix per (task, label) in the log snapshot.
    snapshot, identity = ws.find_snapshot(args.snapshot_run_at)
    alphas = ec.pooled_agreement(snapshot)

    for task in ec.TASKS:
        frames = []
        for programme in ec.programmes(args.exports):
            raw, frame = filtered_frame(args.exports, programme, task, args)
            if frame.empty:
                status = "no_data" if raw.empty else "no_rows_after_filter"
                print(
                    f"  {programme}/{task}: contributes nothing ({status})",
                    file=sys.stderr,
                )
                continue
            frames.append(frame)
        if not frames:
            rows.extend(empty_rows(task, policy, "no_rows_after_filter"))
            continue

        pooled = pd.concat(frames, ignore_index=True)
        csv_path = filtered_root / f"{task}.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        pooled.to_csv(csv_path, index=False, encoding="utf-8")

        # One score dir per policy: it holds one JSON per task, exactly this loop.
        report_path = run_score(pin, csv_path, task, policy, args)
        report = json.loads(report_path.read_text())
        rows.extend(rows_from_report(report, task, policy, alphas))
        print(
            f"  {task}: scored n={report.get('n_examples')} pooled from {len(frames)} programme(s)",
            file=sys.stderr,
        )

    # The default policy keeps the plain filename, because that is the deliverable.
    # Any other policy gets it in the name: the output directory is date-only, so two
    # runs with different flags would otherwise overwrite each other at one path, and
    # the difference would only be visible inside the file.
    suffix = "" if policy == DEFAULT_POLICY else f".{policy}"
    target = (
        ws.stage_report_dir("eval", args.out_dir) / f"eval_metric_estimates{suffix}.csv"
    )
    ws.write_csv(
        target,
        rows,
        columns=COLUMNS,
        prov=ws.provenance(
            script="scripts/eval/score_human_annotations.py",
            inputs=ec.export_inputs(args.exports),
            pragmata_src=pin.src,
            snapshot=identity,
            exports_tree=str(args.exports),
            policy=policy,
            grain="task x metric, pooled across programmes",
            excluded_programmes=sorted(ec.EXCLUDED_PROGRAMMES),
            filters={
                "response_status": "submitted",
                "calibration": "wholly-calibration queries dropped (query grain); "
                "calibration chunks inside mixed retrieval panels kept"
                if args.exclude_calibration
                else "included",
                "retrieval_panels": (
                    "all (--allow-incomplete-panels)"
                    if args.all_panels
                    else "incomplete skipped by pragmata (--skip-incomplete-panels)"
                ),
            },
            ci_level=args.ci,
            n_resamples=args.n_resamples,
            seed=args.seed,
            alpha_population="pooled calibration items",
        ),
    )
    print(f"wrote {target} ({len(rows)} rows, policy={policy})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
