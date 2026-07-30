#!/usr/bin/env python3
"""
Daily annotation logger for the BSt pragmata-workspace.

This is the *logging* half of the pipeline: it computes one snapshot of the live
annotation state and appends it to logs/annotation/log.jsonl. Turning snapshots
into human-readable markdown + plots is the *reporting* half (manual; see
report_tables.py and plot_summary.py).

Reads the live Argilla annotation state (across all domains/workspaces/tasks)
and emits three progress metrics, rolled up task -> domain -> total:

  1. Counts — three distinct quantities, split production vs calibration:
       • submitted_responses (work units; a record done by 3 people = 3),
       • completed_records   (records that met their min_submitted threshold),
       • total_records       (imported; the denominator).
     Record counts come from Argilla ``dataset.progress()``; response counts
     from the REST records endpoint.
  2. Calibration agreement — Krippendorff's alpha over the calibration overlap, from
     pragmata's IAA. The headline is **pooled**: item-level data from every domain goes
     into one reliability matrix per (task, label), then those are averaged unweighted
     across labels per task. alpha = 1 - Do/De is a ratio, so per-domain alphas are never
     averaged; they are kept as a diagnostic only. Each pooled alpha is published with the
     marginals that produced it (n, minority count, prevalence, De) because a label that
     barely varies has De ~ 0, which makes alpha unstable — and at zero variance undefined,
     where pragmata returns 1.0 by convention.
  3. Annotation cadence — median time between consecutive submissions, both
     per-annotator (true individual pace) and global (team throughput), each
     session-guarded (see below).

Each run appends one JSON object to logs/annotation/log.jsonl (for trend-watching) and
prints a one-line status to stdout. The human-readable stats tables are NOT
printed here — they are rendered to reports/annotation/<date>/report.md by report_tables.py
(pass --summary for an ad-hoc table). Diagnostics go to stderr. A domain that
fails is recorded and skipped; the run continues.

Timestamps: per-response submission times come from the Argilla v2 REST records
endpoint (``response.inserted_at`` + ``user_id``). The SDK and the export CSVs
drop them, and record ``updated_at`` is unreliable (bulk/import ops bump it), so
REST is the only true source — which is why cadence reads it directly.

Session guard: an annotator's submissions are sorted by time and the gaps between
them taken. Any gap longer than LOG_SESSION_GAP_MIN (default 30 min) is a
*session break* (a pause, e.g. overnight) — excluded from the median and reported
under ``excluded_gaps`` (global view) so the exclusion is auditable. The headline
``median_active_gap_s`` is the median of the within-session gaps only.

Reuses pragmata where it fits: ``export_annotations`` + ``compute_iaa`` for
agreement, ``dataset.progress()`` for record counts, ``build_user_lookup`` /
``dataset_name`` helpers; a thin REST call supplies per-response timestamps.

Usage:
  scripts/annotation/log.py                 # run all domains, append jsonl + one-line status
  scripts/annotation/log.py --domain X      # one domain only (smoke test)
  scripts/annotation/log.py --summary       # also print the human-readable table to stdout
  scripts/annotation/log.py --no-jsonl      # don't append history
  scripts/annotation/log.py --use-export    # reuse scripts/annotation/export.sh's durable per-domain
                                            #   export for IAA instead of a throwaway one
  scripts/annotation/log.py --self-check    # run the cadence unit self-check and exit
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import tempfile
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import workspace as ws

ws.load_env()  # configs/settings.conf + .env; existing env wins

# Build against the demo deployment's pragmata. (NB: the data
# was imported by the demo-branch pragmata (partition_scope topology), so we read
# it back through the same branch). PRAGMATA_SRC (.env) shadows the
# installed package on sys.path; unset → installed pragmata.
_PRAGMATA_SRC = os.environ.get("PRAGMATA_SRC")
if _PRAGMATA_SRC:
    sys.path.insert(0, _PRAGMATA_SRC)

import pragmata  # noqa: E402
from pragmata.api.annotation_export import export_annotations  # noqa: E402
from pragmata.api.annotation_iaa import compute_iaa  # noqa: E402
from pragmata.core.annotation.argilla_task_definitions import dataset_name  # noqa: E402
from pragmata.core.annotation.client import resolve_argilla_client  # noqa: E402
from pragmata.core.annotation.export_fetcher import build_user_lookup  # noqa: E402
from pragmata.core.schemas.annotation_export import (  # noqa: E402
    GenerationAnnotation,
    GroundingAnnotation,
    RetrievalAnnotation,
)
from pragmata.core.schemas.annotation_task import Task  # noqa: E402
from pragmata.core.settings.annotation_settings import AnnotationSettings  # noqa: E402

# Defensive config sanitizer, keyed off the *active* pragmata's schema: drop any
# top-level config key the loaded AnnotationSettings doesn't accept (extra="forbid").
# Under the demo branch this is a no-op (partition_scope etc. are real fields); it
# only bites if run against a pragmata that doesn't know a key in the shared config.
# We never edit the shared config — the import path may need keys export/IAA don't.
_VALID_CONFIG_KEYS = set(AnnotationSettings.model_fields)

TASKS = [Task.RETRIEVAL, Task.GROUNDING, Task.GENERATION]

# Per-task label fields, discovered generically: a label is any export-schema field typed
# `bool | None` (mirrors iaa_runner's `== bool | None` test). No hardcoded column lists.
_TASK_SCHEMA = {
    Task.RETRIEVAL: RetrievalAnnotation,
    Task.GROUNDING: GroundingAnnotation,
    Task.GENERATION: GenerationAnnotation,
}
_LABELS: dict[Task, list[str]] = {
    t: [n for n, f in c.model_fields.items() if f.annotation == bool | None]
    for t, c in _TASK_SCHEMA.items()
}

NEAR_DEGENERATE_FRAC = (
    0.05  # minority class below this (but >0) → flagged "near-degenerate"
)

SESSION_GAP_S = float(os.environ.get("LOG_SESSION_GAP_MIN", "30")) * 60
MIN_RECORDS = int(os.environ.get("LOG_MIN_RECORDS_FOR_TIMING", "5"))
IAA_RESAMPLES = int(os.environ.get("LOG_IAA_RESAMPLES", "200"))

JSONL_PATH = ws.LOGS_DIR / "log.jsonl"

# username → Argilla user_id (UUID str), populated once per run. The CSV export carries the
# annotator *username*; we map it to the UUID so no real names appear in the output (the REST
# cadence path already keys by UUID directly).
NAME_TO_UUID: dict[str, str] = {}


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# --- metric primitives ------------------------------------------------------


def _split_gaps(points: list[tuple[str, datetime]], threshold_s: float):
    """Sort points by time; split consecutive gaps into kept vs session-break."""
    pts = sorted(points, key=lambda r: r[1])
    kept: list[float] = []
    excluded: list[dict] = []
    for i in range(1, len(pts)):
        gap = (pts[i][1] - pts[i - 1][1]).total_seconds()
        if gap > threshold_s:
            excluded.append(
                {
                    "after_record": pts[i - 1][0],
                    "before_record": pts[i][0],
                    "gap_s": round(gap, 1),
                    "after_at": pts[i - 1][1].isoformat(),
                }
            )
        else:
            kept.append(gap)
    return pts, kept, excluded


def _quantile(ordered: list[float], q: float) -> float:
    """Linear-interpolated quantile of an ALREADY-SORTED list (numpy/R type 7).

    Not ``statistics.quantiles``, which needs at least two data points and cuts into
    n intervals rather than answering for one q — this must survive a single kept gap
    (where every quantile is that gap). Takes the list pre-sorted so a caller wanting
    several quantiles sorts once.
    """
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


# The gap statistics, named once. Reported per-annotator under these names and pooled
# under a "pooled_" prefix; all four go null together below the noise threshold.
_GAP_STAT_KEYS = ("median_active_gap_s", "mean_gap_s", "gap_p25_s", "gap_p75_s")


def _gap_stats(values: list[float]) -> dict:
    """Centre and spread of a set of within-session gaps.

    Shared by the per-annotator and pooled views so the two cannot drift. The guard on
    whether there is enough data to publish stays at each call site, since they test
    different things.
    """
    ordered = sorted(values)
    return {
        "median_active_gap_s": round(statistics.median(ordered), 1),
        "mean_gap_s": round(statistics.fmean(ordered), 1),
        "gap_p25_s": round(_quantile(ordered, 0.25), 1),
        "gap_p75_s": round(_quantile(ordered, 0.75), 1),
    }


def _summarize(
    n: int,
    kept: list[float],
    excluded: list[dict],
    threshold_s: float,
    min_records: int,
    *,
    with_excluded: bool = True,
) -> dict:
    out = {
        # Centre and spread of the within-session gaps. The distribution is right-skewed
        # (a few slow items dominate), so the mean sits above the median and the
        # quartiles are what show the actual spread. Filled in below, or left null.
        **dict.fromkeys(_GAP_STAT_KEYS),
        "session_gap_threshold_s": int(threshold_s),
        "n_events": n,
        "n_gaps_total": max(n - 1, 0),
        "n_gaps_used": len(kept),
        "n_sessions": (len(excluded) + 1) if n >= 1 else 0,
        "active_span_s": round(sum(kept), 1),
        "n_pause_breaks": len(excluded),
        "total_pause_s": round(sum(g["gap_s"] for g in excluded), 1),
        "longest_pause_s": max((g["gap_s"] for g in excluded), default=None),
    }
    if with_excluded:
        out["excluded_gaps"] = excluded
    # One guard for all four: below min_records the whole distribution is too noisy to
    # publish, so they go null together rather than leaving a median with no spread.
    if n >= min_records and kept:
        out.update(_gap_stats(kept))
    return out


def cadence(
    points: list[tuple[str, datetime]],
    *,
    threshold_s: float | None = None,
    min_records: int | None = None,
    with_excluded: bool = True,
) -> dict:
    """Session-guarded median gap between consecutive submission timestamps.

    ``points`` is a list of ``(key, submitted_at)``. Gaps above ``threshold_s``
    are session breaks: excluded from the median and listed in ``excluded_gaps``.
    ``median_active_gap_s`` is null below ``min_records`` events (too noisy).
    """
    threshold_s = SESSION_GAP_S if threshold_s is None else threshold_s
    min_records = MIN_RECORDS if min_records is None else min_records
    pts, kept, excluded = _split_gaps(points, threshold_s)
    return _summarize(
        len(pts), kept, excluded, threshold_s, min_records, with_excluded=with_excluded
    )


def cadence_report(events: list[dict]) -> dict:
    """Both cadence views over response events ``[{user_id, at, record_id}, …]``.

    - ``global``: all submissions pooled and sorted by time = team throughput.
    - ``per_annotator``: each annotator's own submission stream session-guarded
      (their overnight breaks excluded), the kept gaps pooled into one median =
      true individual annotation speed, plus a per-annotator breakdown.
    """
    glob = cadence(
        [(e.get("record_id") or str(i), e["at"]) for i, e in enumerate(events)]
    )

    by_user: dict[str, list[dict]] = {}
    for e in events:
        uid = e.get("user_id")
        if uid:
            by_user.setdefault(uid, []).append(e)

    by_annotator: dict[str, dict] = {}
    pooled_kept: list[float] = []
    for uid, evs in by_user.items():
        pts, kept, excluded = _split_gaps(
            [(ev.get("record_id") or str(i), ev["at"]) for i, ev in enumerate(evs)],
            SESSION_GAP_S,
        )
        # Keyed by the Argilla user_id (UUID) — never a real username.
        by_annotator[uid] = _summarize(
            len(pts), kept, excluded, SESSION_GAP_S, MIN_RECORDS, with_excluded=False
        )
        pooled_kept.extend(kept)

    # Pooled spread mirrors the per-annotator block: the same four statistics over the
    # pooled within-session gaps, so a report can quote a distribution at task/domain
    # level without reaching into by_annotator.
    enough = len(pooled_kept) >= MIN_RECORDS
    pooled_stats = _gap_stats(pooled_kept) if enough else dict.fromkeys(_GAP_STAT_KEYS)
    per = {
        **{f"pooled_{key}": value for key, value in pooled_stats.items()},
        "n_annotators": len(by_user),
        "n_gaps_used": len(pooled_kept),
        "by_annotator": by_annotator,
    }
    return {"per_annotator": per, "global": glob}


def umean(values: list[float]) -> float | None:
    """Unweighted mean; None if empty.

    The headline rule: α is a ratio (1 − Do/De), so α values must never be averaged
    across *domains* — pool the item-level data and recompute instead (see
    :func:`pooled_agreement`). Averaging across *labels* is a different thing and is
    deliberate: it encodes "we care about each label equally".
    """
    if not values:
        return None
    return round(sum(values) / len(values), 4)


# --- pooled agreement -------------------------------------------------------


def expected_disagreement(n_true: int, n: int) -> float | None:
    """Krippendorff's De for one binary nominal label: 2·n₀·n₁/(n(n−1)).

    De is a function of the marginal alone. It is the denominator of α = 1 − Do/De, so
    when a label barely varies De → 0 and α becomes an unstable ratio of near-zero
    numbers; at zero variance De = 0 exactly and α is undefined (pragmata's
    ``krippendorff_alpha_nominal`` returns 1.0 there by convention). pragmata's
    ``LabelAgreement`` is frozen and doesn't expose De, so we derive it here to make that
    degeneracy visible in the report rather than silently averaged into a headline.
    """
    if n < 2:
        return None
    return round(2 * (n - n_true) * n_true / (n * (n - 1)), 4)


def calibration_marginals(path: Path, labels: list[str]) -> dict[str, dict]:
    """Per-label (n, n_true, n_minority, prevalence, De) over calibration-submitted rows.

    Deliberately the *same* row population pragmata's IAA uses — ``run_iaa`` filters to
    ``calibration and response_status == "submitted"`` — so these diagnostics are joinable
    with the α they explain. ``label_stats`` computes prevalence over *all* submitted rows,
    which is the right population for class balance but not for reading α.
    """
    counts = {lab: [0, 0] for lab in labels}  # [n_true, n]
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            if _parse_bool(row.get("calibration")) is not True:
                continue
            if row.get("response_status") != "submitted":
                continue
            for lab in labels:
                v = _parse_bool(row.get(lab))
                if v is None:
                    continue
                counts[lab][1] += 1
                counts[lab][0] += int(v)
    out = {}
    for lab, (n_true, n) in counts.items():
        out[lab] = {
            "n_ratings": n,
            "n_true": n_true,
            "n_minority": min(n_true, n - n_true),
            "prevalence": round(n_true / n, 4) if n else None,
            "expected_disagreement": expected_disagreement(n_true, n),
        }
    return out


# --- label-value statistics -------------------------------------------------


def label_summary(n_true: int, n: int) -> dict:
    """Class balance for one binary label: prevalence + degeneracy flags (descriptive).

    Raw fraction-true only — no good/bad polarity (the reader knows label semantics).
    ``degenerate`` = only one class ever observed (always- or never-true);
    ``near_degenerate`` = minority class present but below NEAR_DEGENERATE_FRAC.
    """
    if n == 0:
        return {
            "n": 0,
            "n_true": 0,
            "prevalence": None,
            "degenerate": False,
            "near_degenerate": False,
        }
    p = n_true / n
    minority = min(p, 1 - p)
    degenerate = n_true == 0 or n_true == n
    return {
        "n": n,
        "n_true": n_true,
        "prevalence": round(p, 4),
        "degenerate": degenerate,
        "near_degenerate": (not degenerate) and minority < NEAR_DEGENERATE_FRAC,
    }


def _parse_bool(s: str | None) -> bool | None:
    if s is None:
        return None
    s = s.strip().lower()
    return True if s == "true" else False if s == "false" else None


def label_stats(
    path: Path, labels: list[str], name_to_uuid: dict[str, str]
) -> tuple[dict, dict]:
    """One CSV pass over a task export → label distribution, discards, notes rate, bias.

    Returns ``(block, raw)``. ``block`` is the serialized stats; ``raw`` carries the
    per-label ``(n_true, n)`` and discard counts for correct (pool-then-recompute)
    rollup at the domain/total level — never average per-domain prevalences.

    Discarded rows (``response_status == "discarded"``) feed the discard breakdown only;
    label distribution and bias are over submitted rows. Annotators are keyed by UUID.
    """
    counts = {lab: [0, 0] for lab in labels}  # [n_true, n] over submitted, non-null
    by_ann: dict[str, dict[str, list[int]]] = {}
    n_submitted = n_notes = n_discarded = 0
    by_reason: dict[str, int] = {}

    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            status = (row.get("response_status") or "").strip()
            if status == "discarded":
                n_discarded += 1
                reason = (row.get("discard_reason") or "").strip() or "unspecified"
                by_reason[reason] = by_reason.get(reason, 0) + 1
                continue
            if status != "submitted":
                continue
            n_submitted += 1
            if (row.get("notes") or "").strip():
                n_notes += 1
            raw_id = row.get("annotator_id", "")
            uuid = name_to_uuid.get(raw_id, raw_id)
            ann = by_ann.setdefault(uuid, {lab: [0, 0] for lab in labels})
            for lab in labels:
                v = _parse_bool(row.get(lab))
                if v is None:
                    continue
                counts[lab][1] += 1
                ann[lab][1] += 1
                if v:
                    counts[lab][0] += 1
                    ann[lab][0] += 1

    per_label = {lab: label_summary(*counts[lab]) for lab in labels}
    pool_prev = {lab: per_label[lab]["prevalence"] for lab in labels}

    by_annotator: dict[str, dict] = {}
    for uuid, ann in by_ann.items():
        out = {}
        for lab in labels:
            nt, n = ann[lab]
            if n == 0:
                continue
            prev = nt / n
            delta = None if pool_prev[lab] is None else round(prev - pool_prev[lab], 4)
            out[lab] = {
                "n": n,
                "n_true": nt,
                "prevalence": round(prev, 4),
                "delta_vs_pool": delta,
            }
        by_annotator[uuid] = out

    total = n_submitted + n_discarded
    block = {
        "per_label": per_label,
        "discards": {
            "n_discarded": n_discarded,
            "n_submitted": n_submitted,
            "discard_rate": round(n_discarded / total, 4) if total else None,
            "by_reason": by_reason,
        },
        "notes_rate": round(n_notes / n_submitted, 4) if n_submitted else None,
        "by_annotator": by_annotator,
    }
    raw = {
        "per_label": {lab: tuple(counts[lab]) for lab in labels}
    }  # for pool-then-recompute rollup
    return block, raw


def read_export_meta(export_dir: Path) -> dict:
    """Constraint + completeness aggregates from the export's meta sidecar.

    The sidecar (``annotation_export.meta.json``) is written next to the CSVs by every
    export, so this works on both the throwaway and ``--use-export`` paths. Degrades to
    ``None`` blocks if the sidecar is missing or unreadable. Both aggregates are
    domain-level: ``constraint_summary`` is per-constraint_id across tasks; completeness
    is the retrieval panel-completeness summary.
    """
    path = export_dir / "annotation_export.meta.json"
    try:
        meta = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"constraints": None, "completeness": None}
    cs = meta.get("constraint_summary") or {}
    return {
        "constraints": {"total": sum(cs.values()), "by_constraint": cs},
        "completeness": meta.get("completeness_summary"),
    }


# --- label / discard / constraint / completeness rollup ---------------------


def empty_label_rollup() -> dict:
    return {t: {lab: [0, 0] for lab in _LABELS[t]} for t in TASKS}


def add_label_raw(acc: dict, raw_by_task: dict) -> None:
    for task, raw in raw_by_task.items():
        for lab, (nt, n) in raw["per_label"].items():
            acc[task][lab][0] += nt
            acc[task][lab][1] += n


def build_label_block(acc: dict) -> dict:
    """Pool (n_true, n) per (task, label), then recompute prevalence/CI/flags."""
    out = {}
    for task in TASKS:
        labs = {lab: label_summary(nt, n) for lab, (nt, n) in acc[task].items()}
        if any(v["n"] > 0 for v in labs.values()):
            out[task.value] = labs
    return out


def empty_discards() -> dict:
    return {"n_discarded": 0, "n_submitted": 0, "by_reason": {}}


def add_discards(acc: dict, d: dict) -> None:
    acc["n_discarded"] += d["n_discarded"]
    acc["n_submitted"] += d["n_submitted"]
    for reason, c in d["by_reason"].items():
        acc["by_reason"][reason] = acc["by_reason"].get(reason, 0) + c


def finalize_discards(acc: dict) -> dict:
    total = acc["n_discarded"] + acc["n_submitted"]
    return {
        **acc,
        "discard_rate": round(acc["n_discarded"] / total, 4) if total else None,
    }


def add_constraints(acc: dict, c: dict | None) -> None:
    if not c:
        return
    for cid, n in c["by_constraint"].items():
        acc[cid] = acc.get(cid, 0) + n


def build_constraints(acc: dict) -> dict:
    return {"total": sum(acc.values()), "by_constraint": acc}


def empty_completeness() -> dict:
    return {"n_panels": 0, "n_complete": 0, "by_k_bucket": {}}


def add_completeness(acc: dict, c: dict | None) -> None:
    if not c:
        return
    acc["n_panels"] += c.get("n_panels", 0)
    acc["n_complete"] += c.get("n_complete", 0)
    for bucket, st in (c.get("by_k_bucket") or {}).items():
        b = acc["by_k_bucket"].setdefault(bucket, {"n_panels": 0, "n_complete": 0})
        b["n_panels"] += st.get("n_panels", 0)
        b["n_complete"] += st.get("n_complete", 0)


def build_completeness(acc: dict) -> dict | None:
    if acc["n_panels"] == 0:
        return None
    # by_k_bucket kept as raw {n_panels, n_complete} — matches the sidecar shape; the
    # renderer recomputes per-bucket fractions (_bucket_frac).
    return {
        "n_panels": acc["n_panels"],
        "n_complete": acc["n_complete"],
        "fraction_complete": round(acc["n_complete"] / acc["n_panels"], 4),
        "by_k_bucket": acc["by_k_bucket"],
    }


# Three distinct quantities, all reported:
#   submitted_responses — response-level work units (a record done by 3 people = 3)
#   total_records       — records imported into the dataset (the denominator)
#   completed_records   — records that reached their min_submitted threshold (Argilla "completed")
# (pending_records = total - completed.) Record counts come from Argilla's
# dataset.progress(); submitted_responses (and per-annotator timestamps) from the
# REST records endpoint — the only source of true per-response submission times.
_COUNT_FIELDS = (
    "submitted_responses",
    "total_records",
    "completed_records",
    "pending_records",
)


def _empty_purpose() -> dict:
    return {k: 0 for k in _COUNT_FIELDS}


def empty_counts() -> dict:
    return {
        **_empty_purpose(),
        "production": _empty_purpose(),
        "calibration": _empty_purpose(),
    }


def add_counts(acc: dict, c: dict) -> None:
    for k in _COUNT_FIELDS:
        acc[k] += c[k]
    for p in ("production", "calibration"):
        for k in _COUNT_FIELDS:
            acc[p][k] += c[p][k]


# --- per-task extraction ----------------------------------------------------


def _purpose_count(events: list[dict], prog: dict | None) -> dict:
    prog = prog or {}
    return {
        "submitted_responses": len(events),
        "total_records": int(prog.get("total", 0)),
        "completed_records": int(prog.get("completed", 0)),
        "pending_records": int(prog.get("pending", 0)),
    }


def task_counts(events: list[dict], prog: dict) -> tuple[dict, set[str]]:
    """Counts (prod/calib split, record totals from progress) + annotator set.

    ``events`` are submitted-response events ``[{user_id, at, purpose, record_id}]``;
    ``prog`` is ``{"production": {...}|None, "calibration": {...}|None}``.
    """
    production = _purpose_count(
        [e for e in events if e["purpose"] == "production"], prog.get("production")
    )
    calibration = _purpose_count(
        [e for e in events if e["purpose"] == "calibration"], prog.get("calibration")
    )
    count = {k: production[k] + calibration[k] for k in _COUNT_FIELDS}
    count["production"] = production
    count["calibration"] = calibration
    annotators = {e["user_id"] for e in events if e.get("user_id")}
    return count, annotators


def task_datasets(settings) -> Iterator[tuple[str, Task, str, str]]:
    """Every (workspace, task, purpose, dataset name) the configured workspaces define.

    Production and calibration are a dataset each. Yields coordinates rather than
    resolved datasets so each caller keeps its own failure policy: counts must raise on
    a hard fetch failure, progress logs it and continues.
    """
    for ws_name, wss in settings.workspaces.items():
        for task in wss.tasks:
            for cal in (False, True):
                yield (
                    ws_name,
                    task,
                    "calibration" if cal else "production",
                    dataset_name(task, calibration=cal, dataset_id=settings.dataset_id),
                )


def fetch_responses(client, http: "httpx.Client", settings) -> dict[Task, list[dict]]:
    """Submitted-response events per task, via the Argilla REST records endpoint.

    The SDK drops per-response timestamps; the REST payload keeps them. Returns
    ``{Task: [{user_id, at: datetime, purpose, record_id}, …]}`` for submitted
    responses only.
    """
    out: dict[Task, list[dict]] = {t: [] for t in TASKS}
    for ws_name, task, purpose, name in task_datasets(settings):
        ds = client.datasets(name=name, workspace=ws_name)
        if ds is None:
            continue
        offset, limit = 0, 1000
        while True:
            r = http.get(
                f"/api/v1/datasets/{ds.id}/records",
                params={
                    "include": "responses",
                    "response_statuses": "submitted",
                    "limit": limit,
                    "offset": offset,
                },
            )
            r.raise_for_status()
            items = r.json().get("items", [])
            for rec in items:
                for resp in rec.get("responses") or []:
                    ts = resp.get("inserted_at")
                    if resp.get("status") == "submitted" and ts:
                        out[task].append(
                            {
                                "user_id": str(resp.get("user_id")),
                                "at": datetime.fromisoformat(ts),
                                "purpose": purpose,
                                "record_id": str(resp.get("record_id")),
                                "task": task.value,
                            }
                        )
            offset += len(items)
            if len(items) < limit:
                break
    return out


def task_agreement(task_agr) -> dict:
    """Per-domain, per-label α from a TaskAgreement (or None) — a **diagnostic**.

    No aggregate here by design: α must not be averaged across domains (see
    :func:`pooled_agreement` for the headline). These per-domain values answer "which
    domain looks like it is struggling", and are read with the pooled marginals in mind —
    a domain whose label never varies reports α = 1.0 from a De of 0.
    """
    if task_agr is None:
        return {"per_label": {}}
    per_label = {}
    for lab in task_agr.labels:
        per_label[lab.label] = {
            "alpha": lab.alpha,
            "n_items": lab.n_items,
            "n_annotators": lab.n_annotators,
            "pct_agreement": lab.pct_agreement,
        }
    return {"per_label": per_label}


# --- per-domain orchestration ----------------------------------------------


def sanitized_config(domain: str) -> tuple[Path, dict]:
    """Write a temp copy of the domain config with non-schema keys dropped.

    Returns (temp path, cleaned dict). Leaves the shared config untouched (the
    demo import path needs the extra keys).
    """
    raw = yaml.safe_load((ws.DOMAINS_DIR / f"{domain}.yaml").read_text()) or {}
    dropped = sorted(k for k in raw if k not in _VALID_CONFIG_KEYS)
    clean = {k: v for k, v in raw.items() if k in _VALID_CONFIG_KEYS}
    if dropped:
        log(f"  (dropped non-schema config keys: {', '.join(dropped)})")
    fd, path = tempfile.mkstemp(prefix=f"annotation-log-cfg-{domain}-", suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(clean, f, sort_keys=False)
    return Path(path), clean


def fetch_progress(client, settings) -> dict[Task, dict]:
    """Per-task record-level progress from Argilla ``dataset.progress()``.

    Returns ``{Task: {"production": {total, completed, pending}|absent,
    "calibration": ...}}``. Datasets that don't exist are simply absent.
    """
    out: dict[Task, dict] = {t: {} for t in TASKS}
    for ws_name, task, purpose, name in task_datasets(settings):
        try:
            ds = client.datasets(name=name, workspace=ws_name)
            if ds is None:
                continue
            prog = dict(ds.progress())
        except Exception as e:
            log(f"  ! progress({ws_name}/{name}) failed: {type(e).__name__}: {e}")
            continue
        out[task][purpose] = prog
    return out


def process_domain(
    domain: str,
    client,
    http: "httpx.Client",
    *,
    use_export: bool = False,
    scratch_base: Path,
) -> dict:
    """Assemble one domain's metrics: counts (progress + REST), agreement (IAA),
    cadence (REST per-response timestamps), label statistics (export CSV).

    Raises on a hard fetch failure. IAA and label-stats failures are caught locally so
    counts/timing still surface.

    The export (read by both IAA and label-stats) is written with ``include_discarded=True``
    so discard rows are present for the discard breakdown; IAA filters them out, so this is
    safe. By default we run a throwaway export under ``scratch_base``, a temp tree owned by
    :func:`run` — never inside ``data/annotation/exports/``, which is published (see
    docs/eval-data-transport.md, "What may live in a pushed tree"). With ``use_export`` we
    instead reuse the durable per-domain export (e.g. scripts/export.sh, also
    include_discarded=True) and skip re-exporting; a missing export degrades gracefully.
    """
    base_dir = str(ws.DATA_DIR if use_export else scratch_base)
    cfg_path, clean_cfg = sanitized_config(domain)
    try:
        cfg = str(cfg_path)
        settings = AnnotationSettings.resolve(
            config=clean_cfg,
            overrides={"argilla": {"api_url": os.environ.get("ARGILLA_API_URL")}},
        )
        progress = fetch_progress(client, settings)
        responses = fetch_responses(client, http, settings)

        # The export feeds both IAA and label-stats. Default: write our own throwaway export
        # into the scratch tree, taking the dir from the writer rather than rebuilding
        # pragmata's layout. With use_export: reuse export.sh's durable per-domain CSVs.
        if use_export:
            export_dir = ws.EXPORTS_DIR / domain
        else:
            export_dir = export_annotations(
                config_path=cfg,
                export_id=domain,
                base_dir=base_dir,
                include_discarded=True,
            ).paths.export_dir

        agr_by_task: dict = {}
        iaa_error: str | None = None
        try:
            report = compute_iaa(
                domain,
                config_path=cfg,
                base_dir=base_dir,
                tasks=TASKS,
                n_resamples=IAA_RESAMPLES,
            )
            agr_by_task = {ta.task: ta for ta in report.tasks}
        except Exception as e:  # IAA-only failure shouldn't sink the counts
            iaa_error = f"{type(e).__name__}: {e}"
            log(f"  ! {domain}: IAA failed ({iaa_error}); reporting counts/timing only")
    finally:
        cfg_path.unlink(missing_ok=True)

    # Label-value statistics from the export CSVs (class balance, discards, per-annotator
    # bias). Degrades independently of IAA. Constraint + completeness aggregates come from
    # the export's meta sidecar (domain-level, reused not recomputed).
    label_by_task: dict[Task, dict] = {}
    label_raw_by_task: dict[Task, dict] = {}
    label_error: str | None = None
    try:
        for task in TASKS:
            csv_path = export_dir / f"{task.value}.csv"
            if not csv_path.exists():
                continue
            label_by_task[task], label_raw_by_task[task] = label_stats(
                csv_path, _LABELS[task], NAME_TO_UUID
            )
    except Exception as e:
        label_error = f"{type(e).__name__}: {e}"
        log(f"  ! {domain}: label stats failed ({label_error})")
    meta = read_export_meta(export_dir)

    tasks_out: dict = {}
    dom_counts = empty_counts()
    dom_annotators: set[str] = set()
    dom_events: list[dict] = []
    dom_discards = empty_discards()

    for task in TASKS:
        events = responses.get(task, [])
        count, annotators = task_counts(events, progress.get(task, {}))

        tasks_out[task.value] = {
            "count": {**count, "n_annotators": len(annotators)},
            "agreement": task_agreement(agr_by_task.get(task)),
            "timing": cadence_report(events),
            "labels": label_by_task.get(task),
        }
        add_counts(dom_counts, count)
        dom_annotators |= annotators
        dom_events.extend(events)
        if task in label_by_task:
            add_discards(dom_discards, label_by_task[task]["discards"])

    # No domain-level `agreement` aggregate: α is not averageable across labels-in-a-domain
    # any more than across domains, and a per-domain headline is exactly what the pooled
    # rule replaces. Per-label α stays under tasks.<task>.agreement.per_label.
    block = {
        "count": {**dom_counts, "n_annotators": len(dom_annotators)},
        "timing": cadence_report(dom_events),
        "discards": finalize_discards(dom_discards),
        "constraints": meta["constraints"],
        "completeness": meta["completeness"],
        "tasks": tasks_out,
    }
    if iaa_error:
        block["iaa_error"] = iaa_error
    if label_error:
        block["label_error"] = label_error
    # Carried out-of-band for total rollup (not serialized at domain level).
    block["_rollup"] = {
        "export_dir": export_dir,  # source for the pooled-α pass in run()
        "counts": dom_counts,
        "annotators": dom_annotators,
        "events": dom_events,
        "label_raw": label_raw_by_task,
        "discards": dom_discards,
        "constraints": meta["constraints"],
        "completeness": meta["completeness"],
    }
    return block


# --- pooled agreement across domains ----------------------------------------

POOLED_EXPORT_ID = "_pooled"


def pooled_agreement(
    export_dirs: list[tuple[str, Path]], scratch_base: Path, config_path: Path
) -> dict:
    """α computed once per (task, label) on item-level data pooled across all domains.

    This is the headline statistic. α = 1 − Do/De is a ratio of two aggregates, so the
    correct estimate is the ratio computed on the pooled data — not an average of
    per-domain α, which estimates no defined quantity and (where a label doesn't vary
    within a domain) averages values that are undefined. Per-domain α survives as a
    diagnostic only.

    Implemented by concatenating each task's per-domain export CSVs into one synthetic
    export and handing that to pragmata's ``compute_iaa``, so the α implementation, the
    calibration filter and the unit/coder pivot are exactly the per-domain ones. Units are
    globally unique (``record_uuid``, content-addressed; ``record_uuid:chunk_id`` for
    retrieval), so concatenation cannot merge units across domains.

    The pooled export is written under ``scratch_base`` — never into
    ``data/annotation/exports/``, which is published by eval-push and whose subdirectories
    are read downstream as the domain list.
    """
    pooled_dir = scratch_base / "annotation" / "exports" / POOLED_EXPORT_ID
    pooled_dir.mkdir(parents=True, exist_ok=True)

    n_rows: dict[str, int] = {}
    for task in TASKS:
        name = f"{task.value}.csv"
        header: list[str] | None = None
        rows: list[list[str]] = []
        for domain, d in export_dirs:
            src = d / name
            if not src.exists():
                continue
            with src.open(newline="") as f:
                rdr = csv.reader(f)
                h = next(rdr, None)
                if h is None:
                    continue
                if header is None:
                    header = h
                elif h != header:
                    log(f"  ! pooled: {name} header differs in {domain}; skipping it")
                    continue
                rows.extend(rdr)
        if header is None:
            continue
        with (pooled_dir / name).open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        n_rows[task.value] = len(rows)
    if not n_rows:
        return {"per_task": {}, "pooled_error": "no export CSVs to pool"}
    log(f"  pooled rows: {', '.join(f'{t}={n}' for t, n in n_rows.items())}")

    try:
        report = compute_iaa(
            POOLED_EXPORT_ID,
            config_path=str(config_path),
            base_dir=str(scratch_base),
            tasks=TASKS,
            n_resamples=IAA_RESAMPLES,
        )
    except Exception as e:
        log(f"  ! pooled IAA failed ({type(e).__name__}: {e})")
        return {"per_task": {}, "pooled_error": f"{type(e).__name__}: {e}"}

    per_task: dict = {}
    for ta in report.tasks:
        task = ta.task
        marginals = calibration_marginals(pooled_dir / f"{task.value}.csv", _LABELS[task])
        per_label = {}
        for lab in ta.labels:
            per_label[lab.label] = {
                "alpha": lab.alpha,
                "ci_lower": lab.ci_lower,
                "ci_upper": lab.ci_upper,
                "n_items": lab.n_items,
                "n_annotators": lab.n_annotators,
                "pct_agreement": lab.pct_agreement,
                **marginals.get(lab.label, {}),
            }
        alphas = [v["alpha"] for v in per_label.values() if v["alpha"] is not None]
        per_task[task.value] = {
            "per_label": per_label,
            # Unweighted across labels: each label counts equally, by choice.
            "mean_alpha": umean(alphas),
            "n_labels": len(alphas),
        }
    return {"per_task": per_task}


def run(domains: list[str], *, use_export: bool = False) -> dict:
    url = os.environ.get("ARGILLA_API_URL")
    key = os.environ["ARGILLA_API_KEY"]
    client = resolve_argilla_client(url, key)
    NAME_TO_UUID.update(
        {name: str(uid) for uid, name in build_user_lookup(client).items()}
    )

    domains_out: dict = {}
    tot_counts = empty_counts()
    tot_annotators: set[str] = set()
    tot_events: list[dict] = []
    export_dirs: list[tuple[str, Path]] = []
    tot_label = empty_label_rollup()
    tot_discards = empty_discards()
    tot_constraints: dict = {}
    tot_completeness = empty_completeness()

    # scratch: throwaway exports live here, never in the published exports tree.
    with httpx.Client(
        base_url=url, headers={"X-Argilla-Api-Key": key}, timeout=60.0
    ) as http, tempfile.TemporaryDirectory(prefix="annotation-log-export-") as scratch:
        for domain in domains:
            log(f"=== {domain} ===")
            try:
                block = process_domain(
                    domain,
                    client,
                    http,
                    use_export=use_export,
                    scratch_base=Path(scratch),
                )
            except Exception as e:
                domains_out[domain] = {"error": f"{type(e).__name__}: {e}"}
                log(f"  ! {domain}: {type(e).__name__}: {e}")
                continue
            roll = block.pop("_rollup")
            domains_out[domain] = block
            add_counts(tot_counts, roll["counts"])
            tot_annotators |= roll["annotators"]
            tot_events.extend(roll["events"])
            export_dirs.append((domain, roll["export_dir"]))
            add_label_raw(tot_label, roll["label_raw"])
            add_discards(tot_discards, roll["discards"])
            add_constraints(tot_constraints, roll["constraints"])
            add_completeness(tot_completeness, roll["completeness"])

        # The headline α, computed on item-level data pooled across every domain. Must run
        # inside the scratch context: it writes the pooled export there, and on the
        # throwaway path the per-domain exports it reads live there too.
        pooled: dict = {"per_task": {}}
        if export_dirs:
            log("=== pooled agreement ===")
            cfg_path, _ = sanitized_config(export_dirs[0][0])
            try:
                pooled = pooled_agreement(export_dirs, Path(scratch), cfg_path)
            finally:
                cfg_path.unlink(missing_ok=True)

    # True pooled cadence per task across domains (so the task-level pace table is a real
    # median, not a weighted-mean approximation of per-domain medians).
    events_by_task: dict[str, list[dict]] = {}
    for e in tot_events:
        events_by_task.setdefault(e["task"], []).append(e)
    timing_by_task = {t: cadence_report(evs) for t, evs in events_by_task.items()}

    return {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": ws.SNAPSHOT_SCHEMA_VERSION,
        "session_gap_threshold_s": int(SESSION_GAP_S),
        "pooled_agreement": pooled,
        "total": {
            "count": {**tot_counts, "n_annotators": len(tot_annotators)},
            "timing": cadence_report(tot_events),
            "timing_by_task": timing_by_task,
            "labels": build_label_block(tot_label),
            "discards": finalize_discards(tot_discards),
            "constraints": build_constraints(tot_constraints),
            "completeness": build_completeness(tot_completeness),
        },
        "domains": domains_out,
    }


# --- output -----------------------------------------------------------------


def _fmt_gap(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    m = seconds / 60
    return f"{m:.1f}m" if m < 90 else f"{m / 60:.1f}h"


def _fmt_alpha(a: float | None) -> str:
    return "—" if a is None else f"{a:+.3f}"


def print_summary(result: dict) -> None:
    """Human-readable table to stdout (cron-mailable).

    Columns: resp=submitted responses, done/total=completed/total records,
    %=completion, ann=annotators, a-gap=per-annotator active cadence (median gap within an
    annotator's own stream, session-guarded).

    No per-domain alpha column: alpha is pooled across domains, so a per-domain headline
    doesn't exist by design. The pooled figures print below the table.
    """
    print(f"Annotation log — {result['run_at']}")
    print(f"(session gap threshold: {result['session_gap_threshold_s'] // 60} min)\n")
    hdr = (
        f"{'domain':<34} {'resp':>6} {'done':>7} {'total':>8} {'%':>5} "
        f"{'ann':>4} {'a-gap':>7}"
    )
    print(hdr)
    print("-" * len(hdr))

    def row(name: str, block: dict) -> None:
        if "error" in block:
            print(f"{name:<34} {'ERROR: ' + block['error'][:60]}")
            return
        c, t = block["count"], block["timing"]
        total, done = c["total_records"], c["completed_records"]
        pct = f"{100 * done / total:.0f}%" if total else "—"
        agap = t["per_annotator"]["pooled_median_active_gap_s"]
        print(
            f"{name:<34} {c['submitted_responses']:>6} {done:>7} {total:>8} {pct:>5} "
            f"{c['n_annotators']:>4} {_fmt_gap(agap):>7}"
        )

    for name, block in result["domains"].items():
        row(name, block)
    print("-" * len(hdr))
    row("TOTAL", result["total"])

    per_task = (result.get("pooled_agreement") or {}).get("per_task") or {}
    if per_task:
        print("\npooled α (unweighted mean across labels, item-level data from all domains):")
        for task, tv in per_task.items():
            degen = [
                lab
                for lab, lv in (tv.get("per_label") or {}).items()
                if lv.get("n_minority") == 0
            ]
            note = f"  [{', '.join(degen)}: no variance, α undefined]" if degen else ""
            print(
                f"  {task:<12} {_fmt_alpha(tv.get('mean_alpha'))}"
                f"  ({tv.get('n_labels', 0)} labels){note}"
            )
    print(
        "\na-gap = median active gap between an annotator's consecutive "
        "submissions (session-guarded). Global cadence + breakdowns in the JSONL."
    )


def append_jsonl(result: dict) -> None:
    ws.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with JSONL_PATH.open("a") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")


# --- self-check -------------------------------------------------------------


def self_check() -> int:
    """Assert the session guard splits sessions and excludes pause gaps."""
    base = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    recs = [
        ("a", base),
        ("b", base + timedelta(minutes=5)),
        ("c", base + timedelta(minutes=10)),
        ("d", base + timedelta(hours=16)),  # overnight pause > 30m
        ("e", base + timedelta(hours=16, minutes=4)),
    ]
    r = cadence(recs, threshold_s=1800, min_records=5)
    assert r["n_gaps_total"] == 4, r
    assert r["n_gaps_used"] == 3, r  # 5m, 5m, 4m kept
    assert r["n_pause_breaks"] == 1, r
    assert r["n_sessions"] == 2, r
    assert r["median_active_gap_s"] == 300.0, r  # median([300, 300, 240])
    assert (
        len(r["excluded_gaps"]) == 1 and r["excluded_gaps"][0]["after_record"] == "c"
    ), r
    assert r["longest_pause_s"] == r["excluded_gaps"][0]["gap_s"], r
    # Spread over the SAME kept gaps as the median — sorted([240, 300, 300]).
    assert r["mean_gap_s"] == 280.0, r  # (300 + 300 + 240) / 3
    assert r["gap_p25_s"] == 270.0, r  # 240 + (300-240) * 0.5
    assert r["gap_p75_s"] == 300.0, r
    # Below min_records → the whole distribution is suppressed together, but the
    # session breaks are still reported.
    r2 = cadence(recs[:3], threshold_s=1800, min_records=5)
    assert r2["median_active_gap_s"] is None and r2["n_gaps_used"] == 2, r2
    assert r2["mean_gap_s"] is None and r2["gap_p25_s"] is None and r2["gap_p75_s"] is None, r2
    # A single kept gap must not blow up: every quantile is that gap.
    r3 = cadence(recs[:2], threshold_s=1800, min_records=1)
    assert r3["gap_p25_s"] == r3["gap_p75_s"] == r3["median_active_gap_s"] == 300.0, r3
    # The pooled view carries the same four statistics, so a report can quote a
    # distribution per task/domain without reaching into by_annotator. It applies the
    # module-level MIN_RECORDS, so 3 pooled gaps are suppressed...
    thin = cadence_report([{"user_id": "u1", "at": at, "record_id": k} for k, at in recs])
    assert thin["per_annotator"]["pooled_median_active_gap_s"] is None, thin
    assert thin["per_annotator"]["n_gaps_used"] == 3, thin
    # ...while 6 gaps (five of 300s, one of 360s) report the whole distribution.
    stream = [("r%d" % i, base + timedelta(minutes=m)) for i, m in enumerate([0, 5, 10, 15, 20, 25, 31])]
    pooled = cadence_report([{"user_id": "u1", "at": at, "record_id": k} for k, at in stream])["per_annotator"]
    assert pooled["n_gaps_used"] == 6, pooled
    assert pooled["pooled_median_active_gap_s"] == 300.0, pooled
    assert pooled["pooled_mean_gap_s"] == 310.0, pooled  # (300*5 + 360) / 6
    assert pooled["pooled_gap_p25_s"] == 300.0 and pooled["pooled_gap_p75_s"] == 300.0, pooled
    print("self-check OK")
    return 0


# --- main -------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--domain", help="Process only this domain (smoke test).")
    ap.add_argument(
        "--no-jsonl",
        action="store_true",
        help="Don't append to logs/annotation/log.jsonl.",
    )
    ap.add_argument(
        "--use-export",
        action="store_true",
        help="Reuse an existing per-domain export (export_id=<domain>, e.g. from "
        "scripts/annotation/export.sh) for IAA instead of running a throwaway export. "
        "IAA degrades gracefully if the export is missing.",
    )
    ap.add_argument(
        "--self-check", action="store_true", help="Run the cadence self-check and exit."
    )
    ap.add_argument(
        "--summary",
        action="store_true",
        help="Also print the human-readable table to stdout (off by default; "
        "the analysis tables live in reports/annotation/<date>.md via report_tables.py).",
    )
    args = ap.parse_args()

    if args.self_check:
        return self_check()

    log(f"pragmata: {Path(pragmata.__file__).resolve().parent}")

    domains = [args.domain] if args.domain else ws.domains()
    if not domains:
        log("No domains found under configs/annotation/domains/")
        return 1

    result = run(domains, use_export=args.use_export)
    if not args.no_jsonl:
        append_jsonl(result)
    if args.summary:
        print_summary(result)
    else:
        c = result["total"]["count"]
        print(
            f"log: {result['run_at']} — {len(result['domains'])} domains, "
            f"{c['submitted_responses']} submitted, {c['completed_records']} completed"
            f"{'' if args.no_jsonl else f'; appended {JSONL_PATH}'}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
