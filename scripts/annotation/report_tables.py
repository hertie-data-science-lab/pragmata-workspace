#!/usr/bin/env python3
"""Render the annotation-report markdown tables deterministically from a snapshot.

The *reporting* half (manual): turns one snapshot logged by log.py into markdown.
Writes reports/annotation/<date>/report.md and updates the reports/annotation/_latest
symlink; plot_summary.py drops its PNGs into the same dir.

Reads one JSON snapshot line from ``logs/annotation/log.jsonl`` (the latest by default)
and prints the analysis tables as markdown to stdout. The numbers are pulled
verbatim from the snapshot - this script only reshapes and formats them, so the
output is reproducible and the hand-written prose/commentary can be layered on top.

Usage:
  scripts/annotation/report_tables.py                       # latest snapshot -> reports/annotation/<date>/report.md
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


def _wmean(pairs) -> float:
    """Weighted mean of (value, weight) pairs; 0.0 when the total weight is 0."""
    tot = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / tot if tot else 0.0


def _note(text: str) -> str:
    """A small italic footnote line, rendered muted beneath the table it annotates."""
    return f"<small>_{text}_</small>"


def _uid(u: str) -> str:
    """Short, single-line annotator id for tables (full UUID is in the snapshot JSONL)."""
    return u[:8] if u else "-"


def _table(headers: list[str], aligns: list[str], rows: list[list[str]]) -> str:
    # All columns left-aligned (aligns retained for per-column intent but unused for now).
    # Markdown applies one alignment per column to header + cells together, so the header
    # can't be centred independently of left-aligned values.
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(":---" for _ in headers) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def _collapsible(summary: str, body: str) -> str:
    """Wrap a (long) table in a click-to-expand <details>, itself inside a blockquote so
    it reads as a distinct, clickable box. Every line gets the '> ' prefix (blank lines
    too) so the inner markdown table still renders inside the quoted HTML."""
    inner = (f"<details>\n<summary>📂 <b>{summary}</b> — click to expand</summary>\n\n"
             f"{body}\n\n</details>")
    return "\n".join(f"> {line}" if line else ">" for line in inner.splitlines())


# ---- table builders ---------------------------------------------------------

def overall_counts(total: dict) -> str:
    """Headline counts in one table: per-split record counts, plus retrieval panel
    completion in two extra columns. Panel data is total-only (not split by
    production/calibration), so the panel cells sit on the Overall row; a panel is
    one query's chunk set, complete only when every chunk is annotated (stricter
    than record-level completion)."""
    c = total["count"]
    comp = total.get("completeness") or {}
    pan_compl = f"**{_int(comp['n_complete'])}**" if comp else "-"
    rows = []
    for label, key in [("Production", "production"), ("Calibration", "calibration")]:
        s = c[key]
        rows.append([label, _int(s["total_records"]), _int(s["submitted_responses"]),
                     _int(s["completed_records"]), _int(s["pending_records"]),
                     _pctc(s["completed_records"], s["total_records"]), "-"])
    rows.append(["**Overall**", f"**{_int(c['total_records'])}**", f"**{_int(c['submitted_responses'])}**",
                 f"**{_int(c['completed_records'])}**", f"**{_int(c['pending_records'])}**",
                 f"**{_pctc(c['completed_records'], c['total_records'])}**", pan_compl])
    table = _table(["Split", "Total", "Subm.", "Compl.", "Pending", "% Compl.",
                    "Panels Compl."],
                   ["l", "r", "r", "r", "r", "r", "r"], rows)
    note = _note("**Panels** are retrieval-only (one per query, total across k-chunk splits) and count "
                 "as complete only when every chunk (all k) is annotated.")
    return f"{table}\n\n{note}"


def progress_by_domain(domains: dict) -> str:
    items = sorted(domains.items(), key=lambda kv: (-kv[1]["count"]["submitted_responses"], kv[0]))
    rows = []
    for name, v in items:
        c = v["count"]
        rows.append([name, _int(c["total_records"]), _int(c["submitted_responses"]),
                     _int(c["completed_records"]), _pctc(c["completed_records"], c["total_records"]),
                     str(c["n_annotators"])])
    return _table(["Domain", "Total", "Subm.", "Compl.", "% Compl.", "Ann."],
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
                         _int(c["completed_records"]), _pctc(c["completed_records"], c["total_records"]),
                         str(c["n_annotators"])])
    return _table(["Domain", "Task", "Total", "Subm.", "Compl.", "% Compl.", "Ann."],
                  ["l", "l", "r", "r", "r", "r", "r"], rows)


def iaa_by_domain(domains: dict, total: dict) -> str:
    """Pooled α per domain (n_items-weighted mean across that domain's labels, as logged)."""
    rows = []

    def alpha_key(kv):  # highest mean α first; domains without α (None) sort last
        a = kv[1]["agreement"].get("mean_alpha")
        return (-(a if a is not None else -2), kv[0])

    for name, v in sorted(domains.items(), key=alpha_key):
        a = v["agreement"]
        if not a.get("n_labels"):
            continue
        rows.append([name, _alpha(a.get("mean_alpha")), str(a["n_labels"])])
    ta = total.get("agreement") or {}
    if ta.get("n_labels"):
        rows.append(["**TOTAL**", f"**{_alpha(ta.get('mean_alpha'))}**", f"**{ta['n_labels']}**"])
    if not rows:
        return ""
    return _table(["Domain", "mean α", "Labels‡"], ["l", "r", "r"], rows)


def iaa_by_task(domains: dict) -> str:
    """Pooled α per task across all domains (n_items-weighted mean of each task's per-label α)."""
    agg: dict[str, list[tuple[float, int]]] = {}
    for v in domains.values():
        for task, tv in v.get("tasks", {}).items():
            for lv in (tv["agreement"].get("per_label") or {}).values():
                if lv.get("alpha") is not None and lv.get("n_items", 0) > 0:
                    agg.setdefault(task, []).append((lv["alpha"], lv["n_items"]))
    rows = []
    for task in TASK_ORDER:
        pairs = agg.get(task)
        if not pairs:
            continue
        rows.append([task, _alpha(_wmean(pairs)), str(len(pairs))])
    if not rows:
        return ""
    return _table(["Task", "mean α", "Labels‡"], ["l", "r", "r"], rows)


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
                rows.append([name, task, lbl, _alpha(lv["alpha"]),
                             _pct(lv["pct_agreement"]), _prev(lab.get(lbl)),
                             _int(lv["n_items"]), str(lv["n_annotators"])])
    if not rows:
        return ""
    return _table(["Domain", "Task", "Label", "α", "% agree", "Prev.*", "Items†", "Ann."],
                  ["l", "l", "l", "r", "r", "r", "r", "r"], rows)


def _label_rows(per_label: dict) -> list[list[str]]:
    return [[lbl, _int(lv["n"]), _prev(lv)] for lbl, lv in per_label.items() if lv["n"] > 0]


def label_distribution_totals(total: dict) -> str:
    """Pooled class balance per task across all domains (fraction true over n)."""
    rows = []
    for task in TASK_ORDER:
        pl = (total.get("labels") or {}).get(task)
        if pl:
            rows += [[task, *r] for r in _label_rows(pl)]
    if not rows:
        return ""
    return _table(["Task", "Label", "n", "Prev.*"], ["l", "l", "r", "r"], rows)


def label_distribution_by_domain(domains: dict) -> str:
    """Class balance per (domain, task, label): prevalence (fraction true) over n."""
    rows = []
    for name, v in sorted(domains.items()):
        for task in TASK_ORDER:
            lab = (v.get("tasks", {}).get(task) or {}).get("labels")
            if lab and lab.get("per_label"):
                rows += [[name, task, *r] for r in _label_rows(lab["per_label"])]
    if not rows:
        return ""
    return _table(["Domain", "Task", "Label", "n", "Prev.*"], ["l", "l", "l", "r", "r"], rows)


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
    return _table(["Domain", "Subm.", "Discarded", "Rate", "Reasons"],
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
    # Per-domain breakdown; the panel-vs-record completion distinction is explained in Overall counts.
    return _table(["Domain", "Panels", "Compl.", "% Compl."], ["l", "r", "r", "r"], rows)


BIAS_FLAG_PP = 20  # |Δ| at/above this (percentage points) counts as a substantial deviation
BIAS_MIN_N = 5  # a deviation needs at least this many items to count (kills small-n noise)


def _bias_by_annotator(domains: dict) -> dict[str, list[dict]]:
    """uuid -> [{delta, n, domain, task, label, prevalence}, …] over tasks with ≥2 annotators."""
    by_uuid: dict[str, list[dict]] = {}
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
                    by_uuid.setdefault(uuid, []).append(
                        {"delta": d, "n": lv["n"], "domain": name, "task": task,
                         "label": lbl, "prevalence": lv["prevalence"]})
    return by_uuid


def annotator_bias(domains: dict) -> str:
    """One table of every per-annotator label deviation from the pooled prevalence,
    grouped by annotator (worst-deviating annotator first, biggest deviation first
    within each). A substantial deviation (≥ threshold pp over enough items) gets a
    red-shaded Δ cell; nothing else is highlighted."""
    by_uuid = _bias_by_annotator(domains)
    if not by_uuid:
        return ""

    def wmean_abs(recs: list[dict]) -> float:
        return _wmean([(abs(r["delta"]), r["n"]) for r in recs])

    def is_substantial(r: dict) -> bool:
        # Large enough to matter, and backed by enough items to trust (not small-n noise).
        return abs(r["delta"]) >= BIAS_FLAG_PP / 100 and r["n"] >= BIAS_MIN_N

    # Raw HTML (not a markdown table) so the problematic Δ cells can be red-shaded.
    RIGHT, DELTA_BG = ' style="text-align:right"', ' style="background-color:#ffd9d9;text-align:right"'

    order = sorted(by_uuid, key=lambda u: -wmean_abs(by_uuid[u]))
    headers = ["Annotator", "Domain", "Task", "Label", "Prev.*", "Δ pp", "n"]
    note = _note(f"**Δ** = the annotator's prevalence for a label minus the pooled prevalence "
                 f"(percentage points); **n** = how many records the annotator labelled for it (the sample "
                 f"behind their prevalence). A **red Δ cell** marks a substantial deviation (≥{BIAS_FLAG_PP}pp) "
                 f"backed by **n ≥ {BIAS_MIN_N}** records — smaller samples are left unmarked, since one or two "
                 "records can't establish a bias.")
    out = [note, "", "<table>",
           "<thead><tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr></thead>", "<tbody>"]
    for uuid in order:
        recs = sorted(by_uuid[uuid], key=lambda r: -abs(r["delta"]))
        for i, r in enumerate(recs):
            delta_td = DELTA_BG if is_substantial(r) else RIGHT
            cells = [f"<td>{_uid(uuid) if i == 0 else ''}</td>",
                     f"<td>{r['domain']}</td>", f"<td>{r['task']}</td>", f"<td>{r['label']}</td>",
                     f"<td{RIGHT}>{_pct(r['prevalence'])}</td>",
                     f"<td{delta_td}>{r['delta'] * 100:+.0f}</td>", f"<td{RIGHT}>{_int(r['n'])}</td>"]
            out.append("<tr>" + "".join(cells) + "</tr>")
    out += ["</tbody>", "</table>"]
    return "\n".join(out)


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
        wmean = _wmean(pairs) if pairs else None
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
        f"**Snapshot:** run at **{ws.local_dt(snap['run_at']):%Y-%m-%d %H:%M %Z}**",
        "## Overall counts\n\n" + overall_counts(total),
    ]
    # Panel-level progress (retrieval k-chunk completeness) precedes record-level
    # progress; omitted when the snapshot carries no completeness data.
    comp = completeness(domains, total)
    if comp:
        parts.append("## Progress (per panel)\n\n### Retrieval k-chunk completeness\n\n" + comp)
    parts += [
        "## Progress (per record)",
        "### By domain\n\n" + progress_by_domain(domains),
        _collapsible("By task", per_task_counts(domains)),
    ]
    # Inter-annotator agreement, now its own section at three granularities
    # (progress tables carry counts only). Omit empty sub-tables.
    iaa_parts = [f"### {sub}\n\n{body}" for sub, body in [
        ("By domain", iaa_by_domain(domains, total)),
        ("By task", iaa_by_task(domains)),
    ] if body]
    by_label = iaa_per_label(domains)  # long — collapsed by default
    if by_label:
        iaa_parts.append(_collapsible("By label", by_label))
    if iaa_parts:  # footnotes for the columns above live at the end of this section
        iaa_parts += [
            _note("**†Items**: the number of calibration-overlap items Krippendorff's α is "
                  "computed on (records annotated by ≥2 people in the calibration split), with **Ann.** "
                  "the annotators in that overlap. α is **not** computed over the submitted/completed "
                  "production counts in the progress tables — those measure coverage, not agreement."),
            _note("**‡Labels**: the number of per-label α scores pooled into the mean (one per "
                  "label × task × domain in scope); the pooled mean is weighted by each score's **Items**."),
        ]
        parts.append("## Inter-annotator agreement (Krippendorff's α)\n\n" + "\n\n".join(iaa_parts))
    # Label-value statistics (omit a section when the snapshot carries no data for it).
    label_parts = []
    if (label_tot := label_distribution_totals(total)):
        label_parts.append(f"### Totals\n\n{label_tot}")
    if (label_dom := label_distribution_by_domain(domains)):  # long — collapsed by default
        label_parts.append(_collapsible("By domain", label_dom))
    if label_parts:  # the Prev. footnote lives here, where prevalence is the subject
        label_parts.append(_note(
            "**\\*Prev. = prevalence**: the share of submitted annotations where the "
            "label is true. We track it to catch degenerate or near-degenerate labels (one class "
            "almost never chosen), which flag an ambiguous guideline or a label too one-sided to "
            "yield meaningful agreement."))
    label_block = "\n\n".join(label_parts)
    disc, viol = discards(domains, total), constraint_violations(domains, total)
    for title, body in [
        ("## Label distribution", label_block),
        ("## Discards", _collapsible("By domain", disc) if disc else ""),
        ("## Logical-constraint violations", _collapsible("By domain", viol) if viol else ""),
        ("## Per-annotator label bias", annotator_bias(domains)),  # Δ/n note is in the table caption
    ]:
        if body:
            parts.append(f"{title}\n\n{body}")
    parts += [
        "## Rate of annotation\n\n" + _collapsible("By annotator", per_annotator_timing(total)),
        "### Domain-level pace\n\n" + domain_pace(domains),
        "### Task-level pace\n\n" + task_pace(domains, total),
        _collapsible("Task × domain pace", task_x_domain_pace(domains)),
        _note(f"**Session gap threshold**: {snap['session_gap_threshold_s'] // 60} min "
              "(longer gaps are treated as session breaks and excluded from cadence medians)."),
    ]
    return "\n\n".join(parts) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jsonl", default=ws.LOGS_DIR / "log.jsonl", type=Path,
                    help="history file to read (default: logs/annotation/log.jsonl)")
    ap.add_argument("--line", default=-1, type=int,
                    help="0-based snapshot index; negative counts from end (default: -1 = latest)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output .md path (default: reports/annotation/<snapshot-date>/report.md)")
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
    # default: reports/annotation/<local-snapshot-date>/report.md (+ _latest symlink)
    if args.out:
        out = args.out
        out.parent.mkdir(parents=True, exist_ok=True)
    else:
        out = ws.report_dir(snap["run_at"]) / "report.md"
        ws.link_latest(out.parent)
    out.write_text(md)
    print(f"wrote {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
