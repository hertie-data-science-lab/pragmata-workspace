#!/usr/bin/env python3
"""Eval-pipeline run 1: corpus metrics from the human-labelled annotations.

Pools the frozen canonical export across programmes, runs `pragmata eval score` once
per task, and collects every per-metric estimate into one tidy CSV — task x metric,
16 rows. Drew's ask has no per-programme grain for the metric taxonomy, and pooling is
also what makes retrieval usable: complete panels are 181 queries pooled, against
n<=5 in four of seven programmes taken singly.

**Why it filters first and passes --path.** `eval score` accepts `--export-id`, which
would read the export directly — but it applies no completeness policy of its own
(core/eval/grouping.py groups by record_uuid and averages whatever chunks are
present), so incomplete retrieval panels would silently produce @K metrics over
partial chunk sets. The filter is therefore applied here, explicitly and in version
control, and the pooled filtered CSV is handed over by path. Two filters:

  1. submitted responses only — a discarded response is an abstention with no labels
  2. retrieval: incomplete panels are skipped by `eval score --skip-incomplete-panels`
     (pragmata #305), which records the drop count on the report as n_panels_skipped;
     --all-panels maps to --allow-incomplete-panels instead

Calibration items stay in: pragmata's majority consolidation
coalesces their multi-annotator rows into one value per unit, exactly as it does when
training. --exclude-calibration drops wholly-calibration queries for comparison runs
(query grain, because a retrieval record is one chunk and panels are routinely mixed —
see ec.drop_calibration_queries).

**Why alpha travels in the same CSV.** The confidence intervals cover sampling
uncertainty over queries only — not annotator disagreement, by explicit design (see
pragmata's docs/design/eval-scoring-metrics.md). Some labels have Krippendorff alpha
at or below chance, and a tight Wilson interval on such labels reads as precision
that is not there. Each row therefore carries the POOLED alpha of the label(s) it
rests on — the matching population for pooled metrics — from the latest log snapshot.

Usage:
  scripts/eval/score_human.py                        # calibration in, complete panels
  scripts/eval/score_human.py --exclude-calibration  # production-only comparison run
  scripts/eval/score_human.py --all-panels           # drop the completeness filter
  scripts/eval/score_human.py --allow-dirty          # permit a dirty pragmata pin
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import eval_common as ec  # noqa: E402
import workspace as ws  # noqa: E402

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
NON_METRIC_KEYS = {"task", "source", "notes", "created_at", "n_examples", "ci_level", "top_k", "n_panels_skipped"}

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
    "alpha_min_ci_low",
    "alpha_min_ci_high",
    "alpha_n_items",
    "alpha_min_degenerate",
    "status",
]


# The reportable policy: calibration kept in, complete retrieval panels only.
DEFAULT_POLICY = "calib-complete"


def policy_name(args) -> str:
    """Short slug naming the filter combination, used in paths and in every row."""
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
    for key, target in (("ci_lower", "alpha_min_ci_low"), ("ci_upper", "alpha_min_ci_high")):
        if stats.get(key) is not None:
            row[target] = f"{stats[key]:.4f}"
    row["alpha_n_items"] = stats.get("n_items", "")
    # alpha = 1 - Do/De is undefined at De = 0 (the label never varies in the pooled
    # calibration items) and pragmata returns 1.0 by convention. Without this flag,
    # grounding_presence_rate's alpha_min of 1.0 reads as perfect measured reliability.
    row["alpha_min_degenerate"] = stats.get("expected_disagreement") == 0


def run_score(pin, csv_path: Path, task: str, score_id: str, args) -> Path:
    """Invoke `pragmata eval score` on a filtered CSV; return the report JSON path.

    Runs from the eval pin's own venv with its src on PYTHONPATH: the shared
    PRAGMATA_SRC pin is a frozen demo checkout with no eval module, and the eval
    module needs pandera, which the workspace venv lacks.
    """
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(pin.src)
    command = [
        str(pin.bin), "eval", "score",
        "--path", str(csv_path),
        "--task", task,
        # base_dir defaults to the process cwd. It is pragmata's tool-root PARENT —
        # tools write <base_dir>/{annotation,querygen,eval} as siblings — so this must
        # be data/, matching the existing data/annotation/ tree. Passing the repo root
        # instead scatters an eval/ tree beside the source.
        "--base-dir", str(ws.DATA_DIR),
        "--score-id", score_id,
        "--ci", str(args.ci),
        "--n-resamples", str(args.n_resamples),
        "--seed", str(args.seed),
        # Panel completeness is pragmata's job (#305): skip records n_panels_skipped
        # on the report; --all-panels accepts the bias instead.
        "--allow-incomplete-panels" if args.all_panels else "--skip-incomplete-panels",
    ]
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-6:]
        raise SystemExit(f"eval score failed for {score_id}/{task}:\n  " + "\n  ".join(tail))
    return ws.DATA_DIR / "eval" / "scores" / score_id / f"{task}_scores.json"


def rows_from_report(report: dict, task: str, policy: str, alphas: dict) -> list[dict]:
    """One row per metric in a score report."""
    rows = []
    for metric, value in report.items():
        if metric in NON_METRIC_KEYS:
            continue
        if value is None:
            # conditional_fabrication_rate is None when no query cited a source, i.e.
            # the conditional is undefined rather than zero.
            rows.append(
                {
                    "task": task, "metric": metric,
                    "policy": policy, "n_examples": report.get("n_examples", ""),
                    "ci_level": report.get("ci_level", ""), "status": "undefined_no_denominator",
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
        {"task": task, "metric": metric, "policy": policy,
         "n": 0, "n_examples": 0, "status": status,
         "source_labels": ";".join(labels)}
        for metric, labels in METRIC_LABELS.items()
        if any(lab in ec.LABELS[task] for lab in labels)
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ec.add_common_args(ap)
    ap.add_argument("--exclude-calibration", action="store_true",
                    help="Comparison run: drop wholly-calibration queries (default: keep them).")
    ap.add_argument("--all-panels", action="store_true",
                    help="Score every retrieval panel, not just complete ones.")
    ap.add_argument("--ci", type=float, default=0.95, help="Confidence level (default 0.95).")
    ap.add_argument("--n-resamples", type=int, default=1000,
                    help="Bootstrap iterations for the continuous retrieval metrics.")
    ap.add_argument("--seed", type=int, default=0,
                    help="Bootstrap RNG seed; fixed so intervals are reproducible.")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="Score even if the pragmata pin has uncommitted changes.")
    args = ap.parse_args()

    pin = ws.eval_pragmata()
    pragmata_git = ws.git_describe(pin.repo)
    if pragmata_git.get("dirty") and not args.allow_dirty:
        raise SystemExit(
            f"pragmata pin at {pin.repo} has uncommitted changes — the numbers would not be\n"
            f"reproducible from its SHA. Commit/stash there, or pass --allow-dirty."
        )

    policy = policy_name(args)
    filtered_root = ws.DATA_DIR / "eval" / "filtered" / policy
    rows: list[dict] = []

    # Pooled alpha, because the metrics are pooled: every domain's calibration items go
    # into one reliability matrix per (task, label) in the log snapshot.
    snapshot = ec.latest_snapshot()
    alphas = ec.pooled_agreement(snapshot)

    for task in ec.TASKS:
        frames = []
        for programme in ec.programmes(args.exports):
            raw, frame = filtered_frame(args.exports, programme, task, args)
            if frame.empty:
                status = "no_data" if raw.empty else "no_rows_after_filter"
                print(f"  {programme}/{task}: contributes nothing ({status})", file=sys.stderr)
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
    target = ec.out_dir(args.out_dir) / f"eval_metric_estimates{suffix}.csv"
    ws.write_csv(
        target,
        rows,
        columns=COLUMNS,
        prov=ws.provenance(
            script="scripts/eval/score_human.py",
            inputs=ec.export_inputs(args.exports),
            pragmata_src=pin.src,
            exports_tree=str(args.exports),
            policy=policy,
            grain="task x metric, pooled across programmes",
            excluded_programmes=sorted(ec.EXCLUDED_PROGRAMMES),
            filters={
                "response_status": "submitted",
                "calibration": "excluded (query grain)" if args.exclude_calibration else "included",
                "retrieval_panels": (
                    "all (--allow-incomplete-panels)" if args.all_panels
                    else "incomplete skipped by pragmata (--skip-incomplete-panels)"
                ),
            },
            ci_level=args.ci,
            n_resamples=args.n_resamples,
            seed=args.seed,
            alpha_source={"snapshot_run_at": snapshot.get("run_at"), "population": "pooled calibration items"},
            caveats=[
                "CIs cover sampling uncertainty over queries only — not annotator "
                "disagreement or label error.",
                "alpha_* columns are the POOLED alpha over every domain's calibration "
                "items (pragmata computes IAA over overlapped rows only).",
                "alpha_min_degenerate=True means the label never varies in that pooled "
                "population: alpha is undefined there and pragmata returns 1.0 by "
                "convention, so such an alpha is NOT evidence of reliability.",
                "The pooled retrieval n=181 is dominated by three programmes "
                "(demokratie 70, europas-zukunft 68, kommunen 33 = 171 of 181); "
                "nachhaltige contributes 0 complete panels. policy_grid.csv carries "
                "the full composition.",
                "top_k is max(chunk_rank) and K varies per query; do not label these '@5'.",
            ],
        ),
    )
    print(f"wrote {target} ({len(rows)} rows, policy={policy})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
