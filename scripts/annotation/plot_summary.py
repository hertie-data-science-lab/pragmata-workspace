#!/usr/bin/env python3
"""Render summary-stat plots from logs/annotation/monitor.jsonl to PNGs.

Burn-up uses the full snapshot history (a time series); the rest use one snapshot
(latest by default). Outputs land in reports/annotation/<snapshot-date>/.

Usage:
  scripts/annotation/plot_summary.py                 # latest snapshot -> reports/annotation/<date>/
  scripts/annotation/plot_summary.py --line N        # 0-based index; negative from end
  scripts/annotation/plot_summary.py --out-dir DIR   # write PNGs here instead
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import workspace as ws  # noqa: E402

TASK_ORDER = ["retrieval", "grounding", "generation"]


def _save(fig, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {path}", file=sys.stderr)


def plot_burnup(snaps: list[dict], out: Path) -> bool:
    """Submitted responses over time, one line per domain + total."""
    series: dict[str, list[tuple[str, int]]] = {}
    for s in snaps:
        t = s["run_at"]
        series.setdefault("TOTAL", []).append((t, s["total"]["count"]["submitted_responses"]))
        for name, v in s.get("domains", {}).items():
            if "count" in v:
                series.setdefault(name, []).append((t, v["count"]["submitted_responses"]))
    if len(snaps) < 2:
        return False
    fig, ax = plt.subplots(figsize=(9, 5))
    for name, pts in sorted(series.items()):
        xs = [p[0][:16] for p in pts]
        ax.plot(xs, [p[1] for p in pts], marker="o", lw=2 if name == "TOTAL" else 1,
                label=name)
    ax.set_ylabel("submitted responses")
    ax.set_title("Annotation progress over time")
    ax.tick_params(axis="x", rotation=45, labelsize=7)
    ax.legend(fontsize=7, ncol=2)
    ax.grid(True, alpha=0.3)
    _save(fig, out / "progress.png")
    return True


def plot_label_prevalence(snap: dict, out: Path) -> bool:
    """Per-task horizontal bars of fraction-true; degenerate/rare highlighted."""
    labels_by_task = snap["total"].get("labels") or {}
    tasks = [t for t in TASK_ORDER if labels_by_task.get(t)]
    if not tasks:
        return False
    fig, axes = plt.subplots(1, len(tasks), figsize=(5 * len(tasks), 4), squeeze=False)
    for ax, task in zip(axes[0], tasks):
        items = [(k, v) for k, v in labels_by_task[task].items() if v["n"] > 0]
        names = [k for k, _ in items]
        prev = [v["prevalence"] for _, v in items]
        colors = ["#d62728" if v["degenerate"] else "#ff7f0e" if v["near_degenerate"]
                  else "#1f77b4" for _, v in items]
        y = range(len(names))
        ax.barh(y, prev, color=colors)
        ax.set_yticks(list(y))
        ax.set_yticklabels(names, fontsize=8)
        ax.set_xlim(0, 1)
        ax.set_xlabel("prevalence (fraction true)")
        ax.set_title(task)
        ax.grid(True, axis="x", alpha=0.3)
    fig.suptitle("Label prevalence — red=degenerate, orange=rare")
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
    axes[1].bar([t[0] for t in tasks], [t[1] for t in tasks], color="#2ca02c")
    axes[1].set_title("Pooled pace by task")
    axes[1].set_ylabel("median active gap (min/record)")
    for ax in axes:
        ax.grid(True, alpha=0.3)
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
    ap.add_argument("--jsonl", default=ws.LOGS_DIR / "monitor.jsonl", type=Path)
    ap.add_argument("--line", default=-1, type=int)
    ap.add_argument("--out-dir", type=Path, default=None)
    args = ap.parse_args()
    ws.load_env()  # for REPORT_TZ (local-date out-dir, matches report_tables)

    snaps = [json.loads(ln) for ln in args.jsonl.read_text().splitlines() if ln.strip()]
    if not snaps:
        sys.exit(f"no snapshots in {args.jsonl}")
    snap = snaps[args.line]
    out = args.out_dir or (ws.REPORTS_DIR / f"{ws.local_dt(snap['run_at']):%Y-%m-%d}")
    out.mkdir(parents=True, exist_ok=True)

    made = sum([
        plot_burnup(snaps, out),
        plot_label_prevalence(snap, out),
        plot_pace(snap, out),
        plot_discards(snap, out),
    ])
    print(f"{made} plot(s) -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
