#!/usr/bin/env python3
"""Render the annotation-monitor markdown tables deterministically from a snapshot.

Reads one JSON snapshot line from ``logs/monitor.jsonl`` (the latest by default)
and prints the analysis tables as markdown to stdout. The numbers are pulled
verbatim from the snapshot - this script only reshapes and formats them, so the
output is reproducible and the hand-written prose/commentary can be layered on top.

Usage:
  scripts/report_tables.py                       # latest line of logs/monitor.jsonl
  scripts/report_tables.py --jsonl PATH          # a different history file
  scripts/report_tables.py --line N              # 0-based line index (negative = from end)
  scripts/report_tables.py > tables.md           # capture for pasting

Sort rules (all deterministic):
  - domains by submitted desc, then name
  - per-task rows: only tasks with submitted > 0, task order retrieval/grounding/generation
  - per-annotator timing by pace (rec/active-h) desc
  - pace tables by gap ascending (untimed groups last)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TASK_ORDER = ["retrieval", "grounding", "generation"]


# ---- formatting helpers -----------------------------------------------------

def _int(n) -> str:
    return f"{int(n):,}" if n is not None else "-"


def _alpha(a) -> str:
    return f"{a:.3f}" if a is not None else "-"


def _pct(p) -> str:
    return f"{p * 100:.1f}%" if p is not None else "-"


def _f(x, nd=2) -> str:
    return f"{x:.{nd}f}" if x is not None else "-"


def _table(headers: list[str], aligns: list[str], rows: list[list[str]]) -> str:
    sep = {"l": "---", "r": "---:", "c": ":-:"}
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(sep[a] for a in aligns) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


# ---- table builders ---------------------------------------------------------

def overall_counts(total: dict) -> str:
    c = total["count"]
    rows = []
    for label, key in [("Production", "production"), ("Calibration", "calibration")]:
        s = c[key]
        rows.append([label, _int(s["total_records"]), _int(s["submitted_responses"]),
                     _int(s["completed_records"]), _int(s["pending_records"])])
    rows.append(["**Overall**", f"**{_int(c['total_records'])}**", f"**{_int(c['submitted_responses'])}**",
                 f"**{_int(c['completed_records'])}**", f"**{_int(c['pending_records'])}**"])
    return _table(["Split", "Total", "Submitted", "Completed", "Pending"],
                  ["l", "r", "r", "r", "r"], rows)


def _domain_alpha_cell(c: dict, agr: dict) -> str:
    if c["submitted_responses"] == 0:
        return "**not started**"
    return _alpha(agr.get("mean_alpha")) if agr.get("mean_alpha") is not None else "-"


def progress_by_domain(domains: dict) -> str:
    items = sorted(domains.items(), key=lambda kv: (-kv[1]["count"]["submitted_responses"], kv[0]))
    rows = []
    for name, v in items:
        c = v["count"]
        rows.append([name, _int(c["total_records"]), _int(c["submitted_responses"]),
                     _int(c["completed_records"]), str(c["n_annotators"]),
                     _domain_alpha_cell(c, v["agreement"])])
    return _table(["Domain", "Total", "Submitted", "Completed", "Annotators", "mean α"],
                  ["l", "r", "r", "r", "r", "r"], rows)


def per_task_counts(domains: dict) -> str:
    """One row per (domain, task) with submitted > 0."""
    rows = []
    for name, v in sorted(domains.items()):
        for task in TASK_ORDER:
            tv = v["tasks"].get(task)
            if not tv or tv["count"]["submitted_responses"] == 0:
                continue
            c = tv["count"]
            rows.append([name, task, _int(c["total_records"]), _int(c["submitted_responses"]),
                         _int(c["completed_records"]), str(c["n_annotators"]),
                         _alpha(tv["agreement"].get("mean_alpha"))])
    return _table(["Domain", "Task", "Total", "Submitted", "Completed", "Ann.", "mean α"],
                  ["l", "l", "r", "r", "r", "r", "r"], rows)


def iaa_per_label(domains: dict) -> str:
    """Emit a labelled sub-table for every (domain, task) that has per-label agreement."""
    blocks = []
    for name, v in sorted(domains.items()):
        for task in TASK_ORDER:
            tv = v["tasks"].get(task)
            if not tv:
                continue
            per_label = tv["agreement"].get("per_label") or {}
            if not per_label:
                continue
            first = next(iter(per_label.values()))
            n_items, n_ann = first["n_items"], first["n_annotators"]
            if n_items == 0:  # no overlapping items → no agreement to report
                continue
            rows = [[lbl, _alpha(lv["alpha"]), _pct(lv["pct_agreement"])]
                    for lbl, lv in per_label.items()]
            tbl = _table(["Label", "α", "% agreement"], ["l", "r", "r"], rows)
            blocks.append(f"#### {name} / {task} (n = {n_items} items, {n_ann} annotators)\n\n{tbl}")
    return "\n\n".join(blocks)


def per_annotator_timing(total: dict) -> str:
    by = total["timing"]["per_annotator"]["by_annotator"]
    rows = []
    for ann, v in by.items():
        active_h = v["active_span_s"] / 3600 if v["active_span_s"] else 0.0
        pace = v["n_events"] / active_h if active_h else None
        rows.append((pace if pace is not None else -1, [
            ann, _f(v["median_active_gap_s"], 1), str(v["n_events"]), str(v["n_sessions"]),
            _f(active_h, 2), _f(pace, 1), str(v["n_pause_breaks"]),
        ]))
    rows.sort(key=lambda r: -r[0])
    return _table(["Annotator", "Median gap (s)", "Events", "Sessions", "Active time (h)",
                   "Pace (rec/active-h)", "Breaks"],
                  ["l", "r", "r", "r", "r", "r", "r"], [r[1] for r in rows])


def domain_pace(domains: dict) -> str:
    timed, untimed = [], []
    for name, v in domains.items():
        pa = v["timing"]["per_annotator"]
        gap = pa["pooled_median_active_gap_s"]
        (timed if gap is not None else untimed).append((name, gap, pa["n_annotators"]))
    timed.sort(key=lambda r: r[1])
    rows = [[name, _f(gap, 1), str(n)] for name, gap, n in timed]
    if untimed:
        rows.append([" / ".join(sorted(n for n, _, _ in untimed)), "-", "0"])
    return _table(["Domain", "Pooled median gap (s)", "Annotators"], ["l", "r", "r"], rows)


def task_pace_collapsed(domains: dict) -> str:
    """Cross-domain task pace: weighted mean of per-annotator medians (weighted by n_gaps).

    The snapshot stores no raw gaps, so a true pooled median is unavailable across
    domains; this weighted mean is the faithful approximation from stored medians.
    """
    agg: dict[str, list[tuple[float, int]]] = {}
    for v in domains.values():
        for task, tv in v["tasks"].items():
            for av in tv["timing"]["per_annotator"].get("by_annotator", {}).values():
                med, n = av.get("median_active_gap_s"), av.get("n_gaps_used", 0)
                if med is not None and n > 0:
                    agg.setdefault(task, []).append((med, n))
    rows = []
    for task in TASK_ORDER:
        pairs = agg.get(task)
        if not pairs:
            continue
        total_n = sum(n for _, n in pairs)
        wmean = sum(m * n for m, n in pairs) / total_n
        rows.append((wmean, [task, _f(wmean, 1), str(len(pairs)), str(total_n)]))
    rows.sort(key=lambda r: r[0])
    return _table(["Task", "Weighted mean gap (s)", "Annotator-sessions", "Total gaps"],
                  ["l", "r", "r", "r"], [r[1] for r in rows])


def task_x_domain_pace(domains: dict) -> str:
    timed = []
    for name, v in domains.items():
        for task, tv in v["tasks"].items():
            pa = tv["timing"]["per_annotator"]
            gap = pa["pooled_median_active_gap_s"]
            if gap is not None:
                timed.append((name, task, gap, pa["n_annotators"]))
    timed.sort(key=lambda r: r[2])
    rows = [[name, task, _f(gap, 1), str(n)] for name, task, gap, n in timed]
    return _table(["Domain", "Task", "Pooled median gap (s)", "Annotators"],
                  ["l", "l", "r", "r"], rows)


# ---- driver -----------------------------------------------------------------

def render(snap: dict) -> str:
    total, domains = snap["total"], snap["domains"]
    parts = [
        f"**Snapshot:** run at **{snap['run_at']}** · "
        f"session gap threshold {snap['session_gap_threshold_s'] // 60} min",
        "## Overall counts\n\n" + overall_counts(total),
        "## Progress by domain\n\n" + progress_by_domain(domains),
        "### Per-task within active domains\n\n" + per_task_counts(domains),
        "## Inter-annotator agreement (Krippendorff's α)\n\n" + iaa_per_label(domains),
        "## Per-annotator activity & timing\n\n"
        f"Pooled median active gap across everyone = **{total['timing']['per_annotator']['pooled_median_active_gap_s']} s/record**.\n\n"
        + per_annotator_timing(total),
        "### Domain-level pace\n\n" + domain_pace(domains),
        "### Task-level pace (collapsed across domains)\n\n" + task_pace_collapsed(domains),
        "### Task × domain pace\n\n" + task_x_domain_pace(domains),
    ]
    return "\n\n".join(parts) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jsonl", default="logs/monitor.jsonl", type=Path,
                    help="history file to read (default: logs/monitor.jsonl)")
    ap.add_argument("--line", default=-1, type=int,
                    help="0-based snapshot index; negative counts from end (default: -1 = latest)")
    args = ap.parse_args()

    lines = [ln for ln in args.jsonl.read_text().splitlines() if ln.strip()]
    if not lines:
        sys.exit(f"no snapshots in {args.jsonl}")
    try:
        snap = json.loads(lines[args.line])
    except IndexError:
        sys.exit(f"line {args.line} out of range ({len(lines)} snapshots)")
    sys.stdout.write(render(snap))


if __name__ == "__main__":
    main()
