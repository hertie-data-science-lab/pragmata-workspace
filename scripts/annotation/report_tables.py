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
    return ", ".join(
        f"{k}×{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
    )


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


_RED_BG = "#ffd9d9"  # α < 0: agreement worse than chance
_YELLOW_BG = "#fff3cd"  # 0 ≤ α < ALPHA_RELIABLE: below the reliability cutoff
_RIGHT = ' style="text-align:right"'
_SEP = ' style="border-left:2px solid #d0d7de"'  # divider between column groups
ALPHA_RELIABLE = 0.667  # Krippendorff's conventional reliability cutoff


def _shaded(color: str, right: bool = False) -> str:
    """Inline style for a background-shaded cell (optionally right-aligned)."""
    return f' style="background-color:{color}{";text-align:right" if right else ""}"'


def _html_table(headers: list, rows: list[list]) -> str:
    """HTML table; each header/body cell is a plain string, or an (html, attr) tuple when
    it needs inline styling - background shading / alignment / group-divider borders - that
    a markdown pipe-table can't express. Plain tables use _table(); reserve this for those."""

    def cell(tag: str, c) -> str:
        text, attr = c if isinstance(c, tuple) else (c, "")
        return f"<{tag}{attr}>{text}</{tag}>"

    head = "".join(cell("th", h) for h in headers)
    body = "\n".join(
        "<tr>" + "".join(cell("td", c) for c in row) + "</tr>" for row in rows
    )
    return (
        f"<table>\n<thead><tr>{head}</tr></thead>\n<tbody>\n{body}\n</tbody>\n</table>"
    )


def _alpha_shade(a) -> str:
    """Cell style for an α value: red if < 0 (worse than chance), yellow if below the
    reliability cutoff, else plain right-aligned."""
    if a is None or a >= ALPHA_RELIABLE:
        return _RIGHT
    return _shaded(_RED_BG if a < 0 else _YELLOW_BG, right=True)


def _alpha_cell(a) -> tuple[str, str]:
    """α as a right-aligned cell, shaded by reliability tier."""
    return (_alpha(a), _alpha_shade(a))


def _uid(u: str) -> str:
    """Short, single-line annotator id for tables (full UUID is in the snapshot JSONL)."""
    return u[:8] if u else "-"


def _table(headers: list[str], aligns: list[str], rows: list[list[str]]) -> str:
    # All columns left-aligned (aligns retained for per-column intent but unused for now).
    # Markdown applies one alignment per column to header + cells together, so the header
    # can't be centred independently of left-aligned values.
    out = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(":---" for _ in headers) + "|",
    ]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def _collapsible(summary: str, body: str) -> str:
    """Wrap a (long) table in a click-to-expand <details>, itself inside a blockquote so
    it reads as a distinct, clickable box. Every line gets the '> ' prefix (blank lines
    too) so the inner markdown table still renders inside the quoted HTML."""
    inner = (
        f"<details>\n<summary>📂 <b>{summary}</b> - click to expand</summary>\n\n"
        f"{body}\n\n</details>"
    )
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
        rows.append(
            [
                label,
                _int(s["total_records"]),
                _int(s["submitted_responses"]),
                _int(s["completed_records"]),
                _int(s["pending_records"]),
                _pctc(s["completed_records"], s["total_records"]),
                "-",
            ]
        )
    rows.append(
        [
            "**Overall**",
            f"**{_int(c['total_records'])}**",
            f"**{_int(c['submitted_responses'])}**",
            f"**{_int(c['completed_records'])}**",
            f"**{_int(c['pending_records'])}**",
            f"**{_pctc(c['completed_records'], c['total_records'])}**",
            pan_compl,
        ]
    )
    table = _table(
        ["Split", "Total", "Subm.", "Compl.", "Pending", "% Compl.", "Panels Compl."],
        ["l", "r", "r", "r", "r", "r", "r"],
        rows,
    )
    note = _note(
        "**Panels** are retrieval-only (one per query, total across k-chunk splits) and count "
        "as complete only when every chunk (all k) is annotated."
    )
    return f"{table}\n\n{note}"


def progress_by_domain(domains: dict) -> str:
    """Per-domain progress in one table: record-level counts (items submitted, records
    completed) with the retrieval panel-completeness columns appended. A panel is one
    query's k-chunk set, complete only when every chunk is annotated (stricter than a
    record); panel cells are '-' for domains the snapshot carries no completeness for."""
    items = sorted(
        domains.items(), key=lambda kv: (-kv[1]["count"]["submitted_responses"], kv[0])
    )
    # A vertical divider (_SEP) opens each column group - items | records | panels | ann -
    # and the per-group completion column is bolded as the figure that matters most.
    headers = [
        "Domain",
        ("Total (items)", _SEP),
        "Subm. (items)",
        ("Compl. (records)", _SEP),
        "% Compl. (records)",
        ("Compl. (panels)", _SEP),
        "% Compl. (panels)",
        ("Ann.", _SEP),
    ]
    rows = []
    for name, v in items:
        c = v["count"]
        comp = v.get("completeness") or {}
        rows.append(
            [
                name,
                (_int(c["total_records"]), _SEP),
                _int(c["submitted_responses"]),
                (f"<b>{_int(c['completed_records'])}</b>", _SEP),
                _pctc(c["completed_records"], c["total_records"]),
                (f"<b>{_int(comp.get('n_complete'))}</b>", _SEP),
                _pct(comp.get("fraction_complete")),
                (str(c["n_annotators"]), _SEP),
            ]
        )
    return _html_table(headers, rows)


def per_task_counts(domains: dict) -> str:
    """One row per (domain, task) with submitted > 0."""
    rows = []
    for name, v in sorted(domains.items()):
        for task in TASK_ORDER:
            tv = v["tasks"].get(task)
            if not tv or tv["count"]["submitted_responses"] == 0:
                continue
            c = tv["count"]
            rows.append(
                [
                    name,
                    task,
                    _int(c["total_records"]),
                    _int(c["submitted_responses"]),
                    _int(c["completed_records"]),
                    _pctc(c["completed_records"], c["total_records"]),
                    str(c["n_annotators"]),
                ]
            )
    return _table(
        ["Domain", "Task", "Total", "Subm.", "Compl.", "% Compl.", "Ann."],
        ["l", "l", "r", "r", "r", "r", "r"],
        rows,
    )


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
        rows.append(
            [name, _alpha_cell(a.get("mean_alpha")), (str(a["n_labels"]), _RIGHT)]
        )
    ta = total.get("agreement") or {}
    if ta.get("n_labels"):
        tma = ta.get("mean_alpha")
        rows.append(
            [
                ("<b>TOTAL</b>", ""),
                (f"<b>{_alpha(tma)}</b>", _alpha_shade(tma)),
                (f"<b>{ta['n_labels']}</b>", _RIGHT),
            ]
        )
    if not rows:
        return ""
    return _html_table(["Domain", "mean α", "Labels‡"], rows)


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
        rows.append([task, _alpha_cell(_wmean(pairs)), (str(len(pairs)), _RIGHT)])
    if not rows:
        return ""
    return _html_table(["Task", "mean α", "Labels‡"], rows)


def iaa_by_label(domains: dict) -> str:
    """Pooled α per label across all domains (n_items-weighted mean of each label's
    per-domain α, within its task). One level finer than the by-task view, one coarser
    than the detailed by-domain×task×label breakdown."""
    agg: dict[tuple[str, str], list[tuple[float, int]]] = {}
    for v in domains.values():
        for task, tv in v.get("tasks", {}).items():
            for lbl, lv in (tv["agreement"].get("per_label") or {}).items():
                if lv.get("alpha") is not None and lv.get("n_items", 0) > 0:
                    agg.setdefault((task, lbl), []).append((lv["alpha"], lv["n_items"]))
    rows = []
    for task in TASK_ORDER:
        entries = [
            (lbl, _wmean(pairs), len(pairs))
            for (t, lbl), pairs in agg.items()
            if t == task
        ]
        entries.sort(key=lambda e: -e[1])  # highest mean α first
        for lbl, mean_a, n in entries:
            rows.append([task, lbl, _alpha_cell(mean_a), (str(n), _RIGHT)])
    if not rows:
        return ""
    return _html_table(["Task", "Label", "mean α", "Labels‡"], rows)


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
                rows.append(
                    [
                        name,
                        task,
                        lbl,
                        _alpha_cell(lv["alpha"]),
                        (_pct(lv["pct_agreement"]), _RIGHT),
                        (_prev(lab.get(lbl)), _RIGHT),
                        (_int(lv["n_items"]), _RIGHT),
                        (str(lv["n_annotators"]), _RIGHT),
                    ]
                )
    if not rows:
        return ""
    return _html_table(
        ["Domain", "Task", "Label", "α", "% agree", "Prev.*", "Items†", "Ann."], rows
    )


def _label_rows(per_label: dict) -> list[list[str]]:
    # Both numerator (# true) and denominator (n); prevalence = # true / n.
    return [
        [lbl, _int(lv["n_true"]), _int(lv["n"]), _prev(lv)]
        for lbl, lv in per_label.items()
        if lv["n"] > 0
    ]


def label_distribution_totals(total: dict) -> str:
    """Pooled class balance per task across all domains (fraction true over n)."""
    rows = []
    for task in TASK_ORDER:
        pl = (total.get("labels") or {}).get(task)
        if pl:
            rows += [[task, *r] for r in _label_rows(pl)]
    if not rows:
        return ""
    return _table(
        ["Task", "Label", "# true", "n", "Prev.*"], ["l", "l", "r", "r", "r"], rows
    )


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
    return _table(
        ["Domain", "Task", "Label", "# true", "n", "Prev.*"],
        ["l", "l", "l", "r", "r", "r"],
        rows,
    )


def discards(domains: dict, total: dict) -> str:
    rows = []
    items = sorted(
        domains.items(),
        key=lambda kv: -((kv[1].get("discards") or {}).get("n_discarded") or 0),
    )
    for name, v in items:
        d = v.get("discards")
        if not d:
            continue
        rows.append(
            [
                name,
                _int(d["n_submitted"]),
                _int(d["n_discarded"]),
                _pct(d["discard_rate"]),
                _breakdown(d["by_reason"]),
            ]
        )
    d = total.get("discards")
    if d:
        rows.append(
            [
                "**TOTAL**",
                f"**{_int(d['n_submitted'])}**",
                f"**{_int(d['n_discarded'])}**",
                f"**{_pct(d['discard_rate'])}**",
                _breakdown(d["by_reason"]),
            ]
        )
    if not rows:
        return ""
    return _table(
        ["Domain", "Subm.", "Discarded", "Rate", "Reasons"],
        ["l", "r", "r", "r", "l"],
        rows,
    )


def constraint_violations(domains: dict, total: dict) -> str:
    rows = []
    for name, v in sorted(domains.items()):
        c = v.get("constraints")
        if not c:
            continue
        rows.append([name, _int(c["total"]), _breakdown(c.get("by_constraint") or {})])
    c = total.get("constraints")
    if c:
        rows.append(
            [
                "**TOTAL**",
                f"**{_int(c['total'])}**",
                _breakdown(c.get("by_constraint") or {}),
            ]
        )
    if not rows:
        return ""
    return _table(["Domain", "Violations", "By constraint"], ["l", "r", "l"], rows)


BIAS_FLAG_PP = (
    20  # |Δ| at/above this (percentage points) counts as a substantial deviation
)
BIAS_MIN_N = (
    5  # a deviation needs at least this many items to count (kills small-n noise)
)


def _bias_by_annotator(domains: dict) -> dict[str, list[dict]]:
    """uuid -> [{delta, n, pool, domain, task, label, prevalence}, …] over tasks with ≥2
    annotators. ``pool`` = how many annotators labelled that item (the deviation's base)."""
    by_uuid: dict[str, list[dict]] = {}
    for name, v in sorted(domains.items()):
        for task in TASK_ORDER:
            lab = (v.get("tasks", {}).get(task) or {}).get("labels")
            by_ann = (lab or {}).get("by_annotator") or {}
            if len(by_ann) < 2:
                continue
            pool: dict[
                str, int
            ] = {}  # label -> # annotators who gave it a non-null value
            for labs in by_ann.values():
                for lbl, lv in labs.items():
                    if lv.get("n", 0) > 0:
                        pool[lbl] = pool.get(lbl, 0) + 1
            for uuid, labs in by_ann.items():
                for lbl, lv in labs.items():
                    d = lv.get("delta_vs_pool")
                    if d is None:
                        continue
                    by_uuid.setdefault(uuid, []).append(
                        {
                            "delta": d,
                            "n": lv["n"],
                            "pool": pool.get(lbl, 0),
                            "domain": name,
                            "task": task,
                            "label": lbl,
                            "prevalence": lv["prevalence"],
                        }
                    )
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

    note = _note(
        f"**Δ** = the annotator's prevalence for a label minus the pooled prevalence "
        f"(percentage points); **n** = how many records the annotator labelled for it; "
        f"**Pool** = how many annotators labelled it in total (the base the deviation is measured "
        f"against - disagreeing with a 2-person pool is weaker evidence than with a 5-person one). "
        f"A **red Δ cell** marks a substantial deviation (≥{BIAS_FLAG_PP}pp) backed by "
        f"**n ≥ {BIAS_MIN_N}** records - smaller samples are left unmarked, since one or two "
        "records can't establish a bias."
    )
    order = sorted(by_uuid, key=lambda u: -wmean_abs(by_uuid[u]))
    rows = []
    for uuid in order:
        recs = sorted(by_uuid[uuid], key=lambda r: -abs(r["delta"]))
        for i, r in enumerate(recs):
            delta = f"{r['delta'] * 100:+.0f}"
            delta_cell = (
                delta,
                _shaded(_RED_BG, right=True) if is_substantial(r) else _RIGHT,
            )
            rows.append(
                [
                    _uid(uuid) if i == 0 else "",
                    r["domain"],
                    r["task"],
                    r["label"],
                    (_pct(r["prevalence"]), _RIGHT),
                    delta_cell,
                    (_int(r["n"]), _RIGHT),
                    (str(r["pool"]), _RIGHT),
                ]
            )
    table = _html_table(
        ["Annotator", "Domain", "Task", "Label", "Prev.*", "Δ pp", "n", "Pool"], rows
    )
    return f"{note}\n\n{table}"


def per_annotator_timing(total: dict) -> str:
    by = total["timing"]["per_annotator"]["by_annotator"]
    rows = []
    for ann, v in by.items():
        active_h = v["active_span_s"] / 3600 if v["active_span_s"] else 0.0
        pace = v["n_events"] / active_h if active_h else None
        rows.append(
            (
                pace if pace is not None else -1,
                [
                    _uid(ann),
                    str(v["n_events"]),
                    _f(active_h, 2),
                    _f(v["median_active_gap_s"], 1),
                    str(v["n_gaps_used"]),
                    _f(pace, 1),
                ],
            )
        )
    rows.sort(key=lambda r: -r[0])
    return _table(
        [
            "Annotator",
            "Events",
            "Active time (h)",
            "Median gap (s)",
            "Gaps",
            "Pace (rec/h)",
        ],
        ["l", "r", "r", "r", "r", "r"],
        [r[1] for r in rows],
    )


def domain_pace(domains: dict) -> str:
    timed, untimed = [], []
    for name, v in domains.items():
        pa = v["timing"]["per_annotator"]
        gap = pa["pooled_median_active_gap_s"]
        (timed if gap is not None else untimed).append(
            (name, gap, pa["n_gaps_used"], pa["n_annotators"])
        )
    timed.sort(key=lambda r: r[1])
    rows = [[name, _f(gap, 1), str(ngaps), str(n)] for name, gap, ngaps, n in timed]
    if untimed:
        rows.append([" / ".join(sorted(n for n, *_ in untimed)), "-", "-", "0"])
    return _table(
        ["Domain", "Median gap (s)", "Gaps", "Annotators"], ["l", "r", "r", "r"], rows
    )


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
        rows.append(
            (
                pooled,
                [
                    task,
                    _f(pooled, 1),
                    str(pa["n_gaps_used"]),
                    _f(wmean, 1),
                    str(pa["n_annotators"]),
                ],
            )
        )
    rows.sort(key=lambda r: r[0])
    return _table(
        ["Task", "Median gap (s)", "Gaps", "Weighted mean (s)", "Annotators"],
        ["l", "r", "r", "r", "r"],
        [r[1] for r in rows],
    )


def task_x_domain_pace(domains: dict) -> str:
    timed = []
    for name, v in domains.items():
        for task, tv in v["tasks"].items():
            pa = tv["timing"]["per_annotator"]
            gap = pa["pooled_median_active_gap_s"]
            if gap is not None:
                timed.append((name, task, gap, pa["n_gaps_used"], pa["n_annotators"]))
    timed.sort(key=lambda r: r[2])
    rows = [
        [name, task, _f(gap, 1), str(ngaps), str(n)]
        for name, task, gap, ngaps, n in timed
    ]
    return _table(
        ["Domain", "Task", "Median gap (s)", "Gaps", "Annotators"],
        ["l", "l", "r", "r", "r"],
        rows,
    )


# ---- driver -----------------------------------------------------------------


def render(snap: dict) -> str:
    total, domains = snap["total"], snap["domains"]
    parts = [
        f"**Snapshot:** run at **{ws.local_dt(snap['run_at']):%Y-%m-%d %H:%M %Z}**",
        (
            "<small>**Counting units**:\n"
            "- an **item** is one submitted annotation,\n"
            "- a **panel** is a query's k-chunk retrieval set (complete only once every "
            "chunk is annotated),\n"
            "- a **record** is a fully completed end-to-end annotation for one query-answer "
            "pair (incl all retrieval panels, all min_submitted).</small>"
        ),
        "## Overall counts\n\n" + overall_counts(total),
        # Per-domain progress: record-level counts with retrieval panel completeness
        # columns appended (panel data is dropped per-domain when the snapshot lacks it).
        "## Progress",
        "### By domain\n\n" + progress_by_domain(domains),
        _collapsible("By domain x task", per_task_counts(domains)),
    ]
    # Inter-annotator agreement - own section, three granularities. α-shading legend up
    # top; each per-column footnote sits below the first table that uses its marker.
    bd, bt = iaa_by_domain(domains, total), iaa_by_task(domains)
    bl = iaa_by_label(domains)
    by_label = iaa_per_label(domains)  # long - collapsed by default
    iaa_parts = []
    if bd or bt or bl or by_label:
        iaa_parts.append(
            _note(
                f"Cell shading flags low agreement - **yellow**: α below {ALPHA_RELIABLE} "
                "(Krippendorff's reliability cutoff); **red**: α below 0 (agreement worse than chance)."
            )
        )
    labels_note = _note(
        "**‡Labels**: the number of per-label α scores pooled into the mean (one per "
        "label × task × domain in scope); the pooled mean is weighted by each score's **Items**."
    )
    labels_done = False
    if bd:
        iaa_parts += [
            f"### By domain\n\n{bd}",
            labels_note,
        ]  # ‡Labels first appears here
        labels_done = True
    if bt:
        iaa_parts.append(f"### By task\n\n{bt}")
        if not labels_done:
            iaa_parts.append(labels_note)
            labels_done = True
    if bl:
        iaa_parts.append(f"### By label\n\n{bl}")
        if not labels_done:
            iaa_parts.append(labels_note)
            labels_done = True
    if by_label:  # Prev.* and Items† first appear in this table; their notes live with it
        label_notes = "\n\n".join(
            [
                _note(
                    "**\\*Prev. = prevalence**: the share of submitted annotations where the "
                    "label is true. We track it to catch degenerate or near-degenerate labels (one class "
                    "almost never chosen), which flag an ambiguous guideline or a label too one-sided to "
                    "yield meaningful agreement."
                ),
                _note(
                    "**†Items**: the number of calibration-overlap items Krippendorff's α is "
                    "computed on (records annotated by ≥2 people in the calibration split), with **Ann.** "
                    "the annotators in that overlap. α is **not** computed over the submitted/completed "
                    "production counts in the progress tables - those measure coverage, not agreement."
                ),
            ]
        )
        iaa_parts.append(
            _collapsible(
                "Detailed breakdown by label", f"{by_label}\n\n{label_notes}"
            )
        )
    if iaa_parts:
        parts.append(
            "## Inter-annotator agreement (Krippendorff's α)\n\n"
            + "\n\n".join(iaa_parts)
        )
    # Label-value statistics (omit a section when the snapshot carries no data for it).
    label_parts = []
    if label_tot := label_distribution_totals(total):
        label_parts.append(_collapsible("Totals", label_tot))
    if label_dom := label_distribution_by_domain(
        domains
    ):  # long - collapsed by default
        label_parts.append(_collapsible("By domain", label_dom))
    label_block = "\n\n".join(label_parts)
    disc, viol = discards(domains, total), constraint_violations(domains, total)
    bias = annotator_bias(domains)  # note + table; collapsed as one block
    for title, body in [
        ("## Label distribution", label_block),
        ("## Discards", _collapsible("By domain", disc) if disc else ""),
        (
            "## Logical-constraint violations",
            _collapsible("By domain", viol) if viol else "",
        ),
        (
            "## Per-annotator label bias",
            _collapsible("By annotator", bias) if bias else "",
        ),
    ]:
        if body:
            parts.append(f"{title}\n\n{body}")
    parts += [
        "## Rate of annotation\n\n"
        + _note(
            "NB: **Median gap** is a *constructed* proxy for pace, not a direct measure of effort. "
            "It is built from each annotator's submission timestamps: sort their submissions, take "
            "the gaps between consecutive ones, drop any longer than the **session-gap threshold** "
            f"({snap['session_gap_threshold_s'] // 60} min) as breaks, and take the median of what "
            "remains. So it assumes continuous work between submissions - real time-on-task "
            "(multitasking, thinking time, idle spells under the threshold) is invisible to it. "
            "**Gaps** is how many within-session gaps the median is built from (the more, the "
            "more trustworthy). Lower median = faster; **Pace** is its inverse (records/hour)."
        )
        + "\n\n"
        + _collapsible("By annotator", per_annotator_timing(total)),
        _collapsible("Domain-level pace", domain_pace(domains)),
        _collapsible("Task-level pace", task_pace(domains, total)),
        _collapsible("Task × domain pace", task_x_domain_pace(domains)),
    ]
    return "\n\n".join(parts) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--jsonl",
        default=ws.LOGS_DIR / "log.jsonl",
        type=Path,
        help="history file to read (default: logs/annotation/log.jsonl)",
    )
    ap.add_argument(
        "--line",
        default=-1,
        type=int,
        help="0-based snapshot index; negative counts from end (default: -1 = latest)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output .md path (default: reports/annotation/<snapshot-date>/report.md)",
    )
    ap.add_argument(
        "--stdout", action="store_true", help="write to stdout instead of a file"
    )
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
