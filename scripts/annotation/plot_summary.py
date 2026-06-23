#!/usr/bin/env python3
"""Render summary-stat plots from logs/annotation/log.jsonl to PNGs.

The *reporting* half (manual): progress.png uses the full snapshot history (a time
series); the rest use one snapshot (latest by default). PNGs land in
reports/annotation/<snapshot-date>/ alongside report.md, and the
reports/annotation/_latest symlink is repointed at that dir.

Usage:
  scripts/annotation/plot_summary.py                 # latest snapshot -> reports/annotation/<date>/
  scripts/annotation/plot_summary.py --line N        # 0-based index; negative from end
  scripts/annotation/plot_summary.py --out-dir DIR   # write PNGs here instead
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.patches as mpatches  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import workspace as ws  # noqa: E402

TASK_ORDER = ["retrieval", "grounding", "generation"]

# Default matplotlib cycle with the leading blue (#1f77b4) dropped, so the
# per-domain lines never collide with the blue TOTAL line in the rows above.
DOMAIN_COLORS = ["#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
                 "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]


def _save(fig, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {path}", file=sys.stderr)


def plot_progress(snaps: list[dict], out: Path) -> bool:
    """2x2 time-series grid in progress.png. Columns are the two metrics
    (burn-up = submitted responses; burn-down = records remaining); rows split
    TOTAL (top, own scale) from the per-domain lines (bottom) so the much larger
    total doesn't compress the domain-level patterns.
    """
    up: dict[str, list[tuple[str, int]]] = {}
    down: dict[str, list[tuple[str, int]]] = {}
    for s in snaps:
        t = s["run_at"]
        tc = s["total"]["count"]
        up.setdefault("TOTAL", []).append((t, tc["submitted_responses"]))
        down.setdefault("TOTAL", []).append((t, tc["pending_records"]))
        for name, v in s.get("domains", {}).items():
            if "count" in v:
                up.setdefault(name, []).append((t, v["count"]["submitted_responses"]))
                down.setdefault(name, []).append((t, v["count"]["pending_records"]))
    if len(snaps) < 2:
        return False
    def line(ax, name, pts, lw, color=None):
        # Plot against real timestamps so points sit at their true time distance
        # apart (matplotlib date axis), not at fixed categorical intervals.
        xs = [ws.local_dt(p[0]) for p in pts]
        ax.plot(xs, [p[1] for p in pts], marker="o", lw=lw, label=name, color=color)

    fig, ((ax_tu, ax_td), (ax_du, ax_dd)) = plt.subplots(2, 2, figsize=(13, 9), sharex=True)

    # Row 1: TOTAL alone (own scale). Row 2: per-domain lines, sharing one
    # colour per domain across both panels so the single legend reads true.
    line(ax_tu, "TOTAL", up["TOTAL"], 2)
    line(ax_td, "TOTAL", down["TOTAL"], 2)
    domains = sorted(k for k in up if k != "TOTAL")
    domain_color = {name: DOMAIN_COLORS[i % len(DOMAIN_COLORS)]
                    for i, name in enumerate(domains)}
    for name in domains:
        line(ax_du, name, up[name], 1, domain_color[name])
    for name in sorted(k for k in down if k != "TOTAL"):
        line(ax_dd, name, down[name], 1, domain_color.get(name))

    ax_tu.set_title("Total submitted (burn-up)")
    ax_td.set_title("Total remaining (burn-down)")
    ax_du.set_title("By domain — submitted (burn-up)")
    ax_dd.set_title("By domain — remaining (burn-down)")
    ax_tu.set_ylabel("submitted responses")
    ax_du.set_ylabel("submitted responses")
    ax_td.set_ylabel("records remaining")
    ax_dd.set_ylabel("records remaining")
    ax_du.legend(fontsize=7, ncol=2)

    # Burn-down panels start at 0 so "remaining" reads against an absolute
    # floor; burn-up panels keep their autoscaled range.
    ax_td.set_ylim(bottom=0)
    ax_dd.set_ylim(bottom=0)

    for ax in (ax_tu, ax_td, ax_du, ax_dd):
        ax.grid(True, alpha=0.3)
    for ax in (ax_du, ax_dd):
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate(rotation=45)
    _save(fig, out / "progress.png")
    return True


def _wilson_ci(n_true: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion. Robust at the
    extreme prevalences (near 0/1) where the labels of interest actually sit,
    and always brackets the observed fraction (so the error bars stay >= 0)."""
    p = n_true / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return centre - half, centre + half


def plot_label_prevalence(snap: dict, out: Path) -> bool:
    """Per-task horizontal bars of fraction-true (95% Wilson CI); degenerate/rare highlighted."""
    labels_by_task = snap["total"].get("labels") or {}
    tasks = [t for t in TASK_ORDER if labels_by_task.get(t)]
    if not tasks:
        return False
    fig, axes = plt.subplots(1, len(tasks), figsize=(5 * len(tasks), 4), squeeze=False)
    for ax, task in zip(axes[0], tasks):
        items = [(k, v) for k, v in labels_by_task[task].items() if v["n"] > 0]
        names = [k for k, _ in items]
        # Drive bar and CI from the same exact fraction so the error bars (which
        # bracket the observed proportion) never go negative on a rounding mismatch.
        prev = [v["n_true"] / v["n"] for _, v in items]
        ci = [_wilson_ci(v["n_true"], v["n"]) for _, v in items]
        xerr = [[max(0.0, p - lo) for p, (lo, _) in zip(prev, ci)],
                [max(0.0, hi - p) for p, (_, hi) in zip(prev, ci)]]
        colors = ["#d62728" if v["degenerate"] else "#ff7f0e" if v["near_degenerate"]
                  else "#1f77b4" for _, v in items]
        y = range(len(names))
        ax.barh(y, prev, color=colors, xerr=xerr,
                error_kw=dict(ecolor="#333333", capsize=3, lw=1))
        ax.set_yticks(list(y))
        ax.set_yticklabels(names, fontsize=8)
        ax.set_xlim(0, 1)
        ax.set_xlabel("prevalence")
        ax.set_title(task)
        ax.grid(True, axis="x", alpha=0.3)
    key = [
        mpatches.Patch(color="#1f77b4", label="normal"),
        mpatches.Patch(color="#ff7f0e", label="rare (near-degenerate)"),
        mpatches.Patch(color="#d62728", label="degenerate (one class only)"),
    ]
    fig.legend(handles=key, loc="lower center", ncol=3, fontsize=8,
               frameon=False, bbox_to_anchor=(0.5, -0.04))
    fig.suptitle("Label prevalence")
    fig.text(0.995, 0.005, "bars: fraction true · whiskers: 95% Wilson CI",
             ha="right", va="bottom", fontsize=7, style="italic", color="#555555")
    _save(fig, out / "label_prevalence.png")
    return True


def plot_pace(snap: dict, out: Path) -> bool:
    """Pooled median active gap by domain and by task (minutes)."""
    domains = snap.get("domains", {})
    dom = []
    for name, v in domains.items():
        g = (v.get("timing", {}).get("per_annotator") or {}).get("pooled_median_active_gap_s")
        if g is not None:
            dom.append((name, g / 60))
    task_agg: dict[str, list[tuple[float, int]]] = {}
    for v in domains.values():
        for task, tv in v.get("tasks", {}).items():
            for av in (tv["timing"]["per_annotator"].get("by_annotator") or {}).values():
                med, n = av.get("median_active_gap_s"), av.get("n_gaps_used", 0)
                if med is not None and n > 0:
                    task_agg.setdefault(task, []).append((med, n))
    tasks = [(t, sum(m * n for m, n in p) / sum(n for _, n in p) / 60)
             for t in TASK_ORDER if (p := task_agg.get(t))]
    if not dom and not tasks:
        return False
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    dom.sort(key=lambda r: r[1])
    axes[0].barh([d[0] for d in dom], [d[1] for d in dom], color="#1f77b4")
    axes[0].set_title("Pooled pace by domain")
    axes[0].set_xlabel("median active gap (min/record)")
    # Both panels horizontal (x = gap); reverse so retrieval reads at the top.
    trev = list(reversed(tasks))
    axes[1].barh([t[0] for t in trev], [t[1] for t in trev], color="#2ca02c")
    axes[1].set_title("Pooled pace by task")
    axes[1].set_xlabel("median active gap (min/record)")
    for ax in axes:
        ax.grid(True, axis="x", alpha=0.3)
    _save(fig, out / "pace.png")
    return True


def plot_discards(snap: dict, out: Path) -> bool:
    """Stacked discard counts by domain × reason."""
    domains = snap.get("domains", {})
    reasons: list[str] = []
    data: dict[str, dict[str, int]] = {}
    for name, v in domains.items():
        br = (v.get("discards") or {}).get("by_reason") or {}
        if br:
            data[name] = br
            reasons += [r for r in br if r not in reasons]
    if not data:
        return False
    fig, ax = plt.subplots(figsize=(9, 4))
    names = sorted(data)
    bottom = [0] * len(names)
    for r in reasons:
        vals = [data[n].get(r, 0) for n in names]
        ax.bar(names, vals, bottom=bottom, label=r)
        bottom = [b + v for b, v in zip(bottom, vals)]
    ax.set_ylabel("discarded responses")
    ax.set_title("Discards by reason")
    ax.tick_params(axis="x", rotation=30, labelsize=8)
    ax.legend(fontsize=8)
    _save(fig, out / "discards.png")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jsonl", default=ws.LOGS_DIR / "log.jsonl", type=Path)
    ap.add_argument("--line", default=-1, type=int)
    ap.add_argument("--out-dir", type=Path, default=None)
    args = ap.parse_args()
    ws.load_env()  # for REPORT_TZ (local-date out-dir, matches report_tables)

    snaps = [json.loads(ln) for ln in args.jsonl.read_text().splitlines() if ln.strip()]
    if not snaps:
        sys.exit(f"no snapshots in {args.jsonl}")
    snap = snaps[args.line]
    if args.out_dir:
        out = args.out_dir
        out.mkdir(parents=True, exist_ok=True)
    else:
        out = ws.report_dir(snap["run_at"])
        ws.link_latest(out)

    made = sum([
        plot_progress(snaps, out),
        plot_label_prevalence(snap, out),
        plot_pace(snap, out),
        plot_discards(snap, out),
    ])
    print(f"{made} plot(s) -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
