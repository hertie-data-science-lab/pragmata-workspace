#!/usr/bin/env python3
"""Render the annotation-monitor markdown tables deterministically from a snapshot.

Reads one JSON snapshot line from ``logs/annotation/monitor.jsonl`` (the latest by default)
and prints the analysis tables as markdown to stdout. The numbers are pulled
verbatim from the snapshot - this script only reshapes and formats them, so the
output is reproducible and the hand-written prose/commentary can be layered on top.

Usage:
  scripts/annotation/report_tables.py                       # latest snapshot -> reports/annotation/<date>.md
  scripts/annotation/report_tables.py --line N              # 0-based line index (negative = from end)
  scripts/annotation/report_tables.py --jsonl PATH          # a different history file
  scripts/annotation/report_tables.py --out PATH            # write to a specific path
  scripts/annotation/report_tables.py --stdout              # print to stdout instead

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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import workspace as ws  # noqa: E402

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


def _prev(lv) -> str:
    """Prevalence (fraction true) as a percentage, from a label_summary dict."""
    if not lv or lv.get("prevalence") is None:
        return "-"
    return f"{lv['prevalence'] * 100:.0f}%"


def _breakdown(counts: dict) -> str:
    if not counts:
        return "-"
    return ", ".join(f"{k}×{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))


def _pctc(done, total) -> str:
    """Completion percentage (completed / total records)."""
    return f"{100 * done / total:.0f}%" if total else "-"


def _uid(u: str) -> str:
    """Short, single-line annotator id for tables (full UUID is in the snapshot JSONL)."""
    return u[:8] if u else "-"


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
                     _int(s["completed_records"]), _int(s["pending_records"]),
                     _pctc(s["completed_records"], s["total_records"])])
    rows.append(["**Overall**", f"**{_int(c['total_records'])}**", f"**{_int(c['submitted_responses'])}**",
                 f"**{_int(c['completed_records'])}**", f"**{_int(c['pending_records'])}**",
                 f"**{_pctc(c['completed_records'], c['total_records'])}**"])
    return _table(["Split", "Total", "Submitted", "Completed", "Pending", "% Done"],
                  ["l", "r", "r", "r", "r", "r"], rows)


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
                     _int(c["completed_records"]), _pctc(c["completed_records"], c["total_records"]),
                     str(c["n_annotators"]), _domain_alpha_cell(c, v["agreement"])])
    return _table(["Domain", "Total", "Subm.", "Compl.", "% Done", "Ann.", "mean α"],
                  ["l", "r", "r", "r", "r", "r", "r"], rows)


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
                         _int(c["completed_records"]), _pctc(c["completed_records"], c["total_records"]),
                         str(c["n_annotators"]), _alpha(tv["agreement"].get("mean_alpha"))])
    return _table(["Domain", "Task", "Total", "Subm.", "Compl.", "% Done", "Ann.", "mean α"],
                  ["l", "l", "r", "r", "r", "r", "r", "r"], rows)


def iaa_per_label(domains: dict) -> str:
    """One combined table of per-label agreement across all (domain, task) with calibration overlap.

    Items = calibration-overlap items, Ann = annotators in that overlap (both per domain/task).
    """
    rows = []
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
            lab = (tv.get("labels") or {}).get("per_label") or {}
            for lbl, lv in per_label.items():
                rows.append([name, task, lbl, _alpha(lv["alpha"]), _pct(lv["pct_agreement"]),
                             _prev(lab.get(lbl)), _int(n_items), str(n_ann)])
    if not rows:
        return ""
    return _table(["Domain", "Task", "Label", "α", "% agree", "Prev.", "Items", "Ann."],
                  ["l", "l", "l", "r", "r", "r", "r", "r"], rows)


def label_distribution(domains: dict, total: dict) -> str:
    """Class balance per (domain, task, label): prevalence (fraction true) over n."""
    rows = []

    def emit(scope, task, per_label):
        for lbl, lv in per_label.items():
            if lv["n"] == 0:  # label with no submitted data yet — skip the empty row
                continue
            rows.append([scope, task, lbl, _int(lv["n"]), _prev(lv)])

    for name, v in sorted(domains.items()):
        for task in TASK_ORDER:
            lab = (v.get("tasks", {}).get(task) or {}).get("labels")
            if lab and lab.get("per_label"):
                emit(name, task, lab["per_label"])
    for task in TASK_ORDER:
        pl = (total.get("labels") or {}).get(task)
        if pl:
            emit("**TOTAL**", task, pl)
    if not rows:
        return ""
    return _table(["Domain", "Task", "Label", "n", "Prev."],
                  ["l", "l", "l", "r", "r"], rows)


def discards(domains: dict, total: dict) -> str:
    rows = []
    items = sorted(domains.items(), key=lambda kv: -((kv[1].get("discards") or {}).get("n_discarded") or 0))
    for name, v in items:
        d = v.get("discards")
        if not d:
            continue
        rows.append([name, _int(d["n_submitted"]), _int(d["n_discarded"]),
                     _pct(d["discard_rate"]), _breakdown(d["by_reason"])])
    d = total.get("discards")
    if d:
        rows.append(["**TOTAL**", f"**{_int(d['n_submitted'])}**", f"**{_int(d['n_discarded'])}**",
                     f"**{_pct(d['discard_rate'])}**", _breakdown(d["by_reason"])])
    if not rows:
        return ""
    return _table(["Domain", "Submitted", "Discarded", "Rate", "Reasons"],
                  ["l", "r", "r", "r", "l"], rows)


def constraint_violations(domains: dict, total: dict) -> str:
    rows = []
    for name, v in sorted(domains.items()):
        c = v.get("constraints")
        if not c:
            continue
        rows.append([name, _int(c["total"]), _breakdown(c.get("by_constraint") or {})])
    c = total.get("constraints")
    if c:
        rows.append(["**TOTAL**", f"**{_int(c['total'])}**", _breakdown(c.get("by_constraint") or {})])
    if not rows:
        return ""
    return _table(["Domain", "Violations", "By constraint"], ["l", "r", "l"], rows)


def completeness(domains: dict, total: dict) -> str:
    """Retrieval panel completeness: fraction of K-chunk panels fully annotated."""
    rows = []

    def emit(name, c):
        if not c:
            return
        rows.append([name, _int(c["n_panels"]), _int(c["n_complete"]), _pct(c["fraction_complete"])])

    for name, v in sorted(domains.items()):
        emit(name, v.get("completeness"))
    emit("**TOTAL**", total.get("completeness"))
    if not rows:
        return ""
    return _table(["Domain", "Panels", "Complete", "Frac"], ["l", "r", "r", "r"], rows)


def annotator_bias(domains: dict, top_n: int = 15) -> str:
    """Largest per-annotator deviations from the pool prevalence (≥2 annotators per task)."""
    devs = []
    for name, v in sorted(domains.items()):
        for task in TASK_ORDER:
            lab = (v.get("tasks", {}).get(task) or {}).get("labels")
            by_ann = (lab or {}).get("by_annotator") or {}
            if len(by_ann) < 2:
                continue
            for uuid, labs in by_ann.items():
                for lbl, lv in labs.items():
                    d = lv.get("delta_vs_pool")
                    if d is None:
                        continue
                    devs.append((abs(d), [_uid(uuid), name, task, lbl,
                                          _pct(lv["prevalence"]), f"{d * 100:+.0f}", _int(lv["n"])]))
    if not devs:
        return ""
    devs.sort(key=lambda r: -r[0])
    return _table(["Annotator", "Domain", "Task", "Label", "Ann. prev.", "Δ pool (pp)", "n"],
                  ["l", "l", "l", "l", "r", "r", "r"], [r[1] for r in devs[:top_n]])


def per_annotator_timing(total: dict) -> str:
    by = total["timing"]["per_annotator"]["by_annotator"]
    rows = []
    for ann, v in by.items():
        active_h = v["active_span_s"] / 3600 if v["active_span_s"] else 0.0
        pace = v["n_events"] / active_h if active_h else None
        rows.append((pace if pace is not None else -1, [
            _uid(ann), str(v["n_events"]), _f(active_h, 2),
            _f(v["median_active_gap_s"], 1), _f(pace, 1),
        ]))
    rows.sort(key=lambda r: -r[0])
    return _table(["Annotator", "Events", "Active time (h)", "Median gap (s)", "Pace (rec/h)"],
                  ["l", "r", "r", "r", "r"], [r[1] for r in rows])


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
    return _table(["Domain", "Median gap (s)", "Annotators"], ["l", "r", "r"], rows)


def task_pace(domains: dict, total: dict) -> str:
    """Cross-domain task pace: true pooled median (events pooled by task) alongside the
    weighted mean of per-domain medians (gap-count-weighted) for comparison."""
    by_task = total.get("timing_by_task") or {}
    # weighted mean of per-(domain,task) per-annotator medians (the pre-pooling approximation)
    agg: dict[str, list[tuple[float, int]]] = {}
    for v in domains.values():
        for task, tv in v["tasks"].items():
            for av in tv["timing"]["per_annotator"].get("by_annotator", {}).values():
                med, n = av.get("median_active_gap_s"), av.get("n_gaps_used", 0)
                if med is not None and n > 0:
                    agg.setdefault(task, []).append((med, n))
    rows = []
    for task in TASK_ORDER:
        pa = (by_task.get(task) or {}).get("per_annotator")
        if not pa or pa.get("pooled_median_active_gap_s") is None:
            continue
        pooled = pa["pooled_median_active_gap_s"]
        pairs = agg.get(task)
        wmean = sum(m * n for m, n in pairs) / sum(n for _, n in pairs) if pairs else None
        rows.append((pooled, [task, _f(pooled, 1), _f(wmean, 1), str(pa["n_annotators"])]))
    rows.sort(key=lambda r: r[0])
    return _table(["Task", "Median gap (s)", "Weighted mean (s)", "Annotators"],
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
    return _table(["Domain", "Task", "Median gap (s)", "Annotators"],
                  ["l", "l", "r", "r"], rows)


# ---- driver -----------------------------------------------------------------

def render(snap: dict) -> str:
    total, domains = snap["total"], snap["domains"]
    parts = [
        f"**Snapshot:** run at **{ws.local_dt(snap['run_at']):%Y-%m-%d %H:%M %Z}** · "
        f"session gap threshold {snap['session_gap_threshold_s'] // 60} min",
        "## Overall counts\n\n" + overall_counts(total),
        "## Progress by domain\n\n" + progress_by_domain(domains),
        "### Per-task within active domains\n\n" + per_task_counts(domains),
        "## Inter-annotator agreement (Krippendorff's α)\n\n" + iaa_per_label(domains),
    ]
    # Label-value statistics (omit a section when the snapshot carries no data for it).
    for title, body in [
        ("## Label distribution", label_distribution(domains, total)),
        ("## Discards", discards(domains, total)),
        ("## Logical-constraint violations", constraint_violations(domains, total)),
        ("## Retrieval panel completeness", completeness(domains, total)),
        ("## Per-annotator label bias", annotator_bias(domains)),
    ]:
        if body:
            parts.append(f"{title}\n\n{body}")
    parts += [
        "## Per-annotator activity & timing\n\n" + per_annotator_timing(total),
        "### Domain-level pace\n\n" + domain_pace(domains),
        "### Task-level pace\n\n" + task_pace(domains, total),
        "### Task × domain pace\n\n" + task_x_domain_pace(domains),
    ]
    return "\n\n".join(parts) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jsonl", default=ws.LOGS_DIR / "monitor.jsonl", type=Path,
                    help="history file to read (default: logs/annotation/monitor.jsonl)")
    ap.add_argument("--line", default=-1, type=int,
                    help="0-based snapshot index; negative counts from end (default: -1 = latest)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output .md path (default: reports/annotation/<snapshot-date>.md)")
    ap.add_argument("--stdout", action="store_true", help="write to stdout instead of a file")
    args = ap.parse_args()
    ws.load_env()  # for REPORT_TZ (local-time display)

    lines = [ln for ln in args.jsonl.read_text().splitlines() if ln.strip()]
    if not lines:
        sys.exit(f"no snapshots in {args.jsonl}")
    try:
        snap = json.loads(lines[args.line])
    except IndexError:
        sys.exit(f"line {args.line} out of range ({len(lines)} snapshots)")

    md = render(snap)
    if args.stdout:
        sys.stdout.write(md)
        return
    # default: reports/annotation/<local-snapshot-date>.md
    out = args.out or (ws.REPORTS_DIR / f"{ws.local_dt(snap['run_at']):%Y-%m-%d}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    print(f"wrote {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
