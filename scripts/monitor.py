#!/usr/bin/env python3
"""
Daily annotation monitor for the BSt pragmata-workspace.

Reads the live Argilla annotation state (across all domains/workspaces/tasks)
and emits three progress metrics, rolled up task -> domain -> total:

  1. Counts — three distinct quantities, split production vs calibration:
       • submitted_responses (work units; a record done by 3 people = 3),
       • completed_records   (records that met their min_submitted threshold),
       • total_records       (imported; the denominator).
     Record counts come from Argilla ``dataset.progress()``; response counts
     from the REST records endpoint.
  2. Calibration agreement — per-label Krippendorff's alpha (+ an n_items-weighted
     mean as a headline), straight off pragmata's IAA (calibration overlap only).
  3. Annotation cadence — median time between consecutive submissions, both
     per-annotator (true individual pace) and global (team throughput), each
     session-guarded (see below).

Each run appends one JSON object to logs/monitor.jsonl (for trend-watching) and
prints a human-readable summary to stdout (which cron can mail). Diagnostics go
to stderr. A domain that fails is recorded and skipped; the run continues.

Timestamps: per-response submission times come from the Argilla v2 REST records
endpoint (``response.inserted_at`` + ``user_id``). The SDK and the export CSVs
drop them, and record ``updated_at`` is unreliable (bulk/import ops bump it), so
REST is the only true source — which is why cadence reads it directly.

Session guard: an annotator's submissions are sorted by time and the gaps between
them taken. Any gap longer than MONITOR_SESSION_GAP_MIN (default 30 min) is a
*session break* (a pause, e.g. overnight) — excluded from the median and reported
under ``excluded_gaps`` (global view) so the exclusion is auditable. The headline
``median_active_gap_s`` is the median of the within-session gaps only.

Reuses pragmata where it fits: ``export_annotations`` + ``compute_iaa`` for
agreement, ``dataset.progress()`` for record counts, ``build_user_lookup`` /
``dataset_name`` helpers; a thin REST call supplies per-response timestamps.

Usage:
  scripts/monitor.py                 # run all domains, append jsonl + print summary
  scripts/monitor.py --domain X      # one domain only (smoke test)
  scripts/monitor.py --no-jsonl      # print summary, don't append history
  scripts/monitor.py --self-check    # run the cadence unit self-check and exit
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import workspace as ws

ws.load_env()  # config/workspace.env + .env; existing env wins

# Build against the demo deployment's pragmata. (NB: the data
# was imported by the demo-branch pragmata (partition_scope topology), so we read
# it back through the same branch). PRAGMATA_SRC (config/workspace.env) shadows the
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
from pragmata.core.schemas.annotation_task import Task  # noqa: E402
from pragmata.core.settings.annotation_settings import AnnotationSettings  # noqa: E402

# Defensive config sanitizer, keyed off the *active* pragmata's schema: drop any
# top-level config key the loaded AnnotationSettings doesn't accept (extra="forbid").
# Under the demo branch this is a no-op (partition_scope etc. are real fields); it
# only bites if run against a pragmata that doesn't know a key in the shared config.
# We never edit the shared config — the import path may need keys export/IAA don't.
_VALID_CONFIG_KEYS = set(AnnotationSettings.model_fields)

EXPORT_ID = "monitor"  # fixed → overwritten each run (no unbounded disk growth)
TASKS = [Task.RETRIEVAL, Task.GROUNDING, Task.GENERATION]

SESSION_GAP_S = float(os.environ.get("MONITOR_SESSION_GAP_MIN", "30")) * 60
MIN_RECORDS = int(os.environ.get("MONITOR_MIN_RECORDS_FOR_TIMING", "5"))
IAA_RESAMPLES = int(os.environ.get("MONITOR_IAA_RESAMPLES", "200"))

JSONL_PATH = ws.LOGS_DIR / "monitor.jsonl"

# Argilla user_id (UUID str) → username, populated once per run for readable
# per-annotator cadence keys.
USER_LOOKUP: dict[str, str] = {}


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
            excluded.append({
                "after_record": pts[i - 1][0],
                "before_record": pts[i][0],
                "gap_s": round(gap, 1),
                "after_at": pts[i - 1][1].isoformat(),
            })
        else:
            kept.append(gap)
    return pts, kept, excluded


def _summarize(n: int, kept: list[float], excluded: list[dict], threshold_s: float,
               min_records: int, *, with_excluded: bool = True) -> dict:
    out = {
        "median_active_gap_s": None,
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
    if n >= min_records and kept:
        out["median_active_gap_s"] = round(statistics.median(kept), 1)
    return out


def cadence(points: list[tuple[str, datetime]], *, threshold_s: float | None = None,
            min_records: int | None = None, with_excluded: bool = True) -> dict:
    """Session-guarded median gap between consecutive submission timestamps.

    ``points`` is a list of ``(key, submitted_at)``. Gaps above ``threshold_s``
    are session breaks: excluded from the median and listed in ``excluded_gaps``.
    ``median_active_gap_s`` is null below ``min_records`` events (too noisy).
    """
    threshold_s = SESSION_GAP_S if threshold_s is None else threshold_s
    min_records = MIN_RECORDS if min_records is None else min_records
    pts, kept, excluded = _split_gaps(points, threshold_s)
    return _summarize(len(pts), kept, excluded, threshold_s, min_records, with_excluded=with_excluded)


def cadence_report(events: list[dict]) -> dict:
    """Both cadence views over response events ``[{user_id, at, record_id}, …]``.

    - ``global``: all submissions pooled and sorted by time = team throughput.
    - ``per_annotator``: each annotator's own submission stream session-guarded
      (their overnight breaks excluded), the kept gaps pooled into one median =
      true individual annotation speed, plus a per-annotator breakdown.
    """
    glob = cadence([(e.get("record_id") or str(i), e["at"]) for i, e in enumerate(events)])

    by_user: dict[str, list[dict]] = {}
    for e in events:
        uid = e.get("user_id")
        if uid:
            by_user.setdefault(uid, []).append(e)

    by_annotator: dict[str, dict] = {}
    pooled_kept: list[float] = []
    for uid, evs in by_user.items():
        pts, kept, excluded = _split_gaps(
            [(ev.get("record_id") or str(i), ev["at"]) for i, ev in enumerate(evs)], SESSION_GAP_S)
        name = USER_LOOKUP.get(uid, uid)
        by_annotator[name] = _summarize(len(pts), kept, excluded, SESSION_GAP_S, MIN_RECORDS,
                                        with_excluded=False)
        pooled_kept.extend(kept)

    per = {
        "pooled_median_active_gap_s": (round(statistics.median(pooled_kept), 1)
                                       if len(pooled_kept) >= MIN_RECORDS else None),
        "n_annotators": len(by_user),
        "n_gaps_used": len(pooled_kept),
        "by_annotator": by_annotator,
    }
    return {"per_annotator": per, "global": glob}


def wmean(pairs: list[tuple[float, int]]) -> float | None:
    """n_items-weighted mean of (alpha, n_items); None if no weight."""
    total = sum(n for _, n in pairs)
    if not total:
        return None
    return round(sum(a * n for a, n in pairs) / total, 4)


# Three distinct quantities, all reported:
#   submitted_responses — response-level work units (a record done by 3 people = 3)
#   total_records       — records imported into the dataset (the denominator)
#   completed_records   — records that reached their min_submitted threshold (Argilla "completed")
# (pending_records = total - completed.) Record counts come from Argilla's
# dataset.progress(); submitted_responses (and per-annotator timestamps) from the
# REST records endpoint — the only source of true per-response submission times.
_COUNT_FIELDS = ("submitted_responses", "total_records", "completed_records", "pending_records")


def _empty_purpose() -> dict:
    return {k: 0 for k in _COUNT_FIELDS}


def empty_counts() -> dict:
    return {**_empty_purpose(), "production": _empty_purpose(), "calibration": _empty_purpose()}


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
    production = _purpose_count([e for e in events if e["purpose"] == "production"], prog.get("production"))
    calibration = _purpose_count([e for e in events if e["purpose"] == "calibration"], prog.get("calibration"))
    count = {k: production[k] + calibration[k] for k in _COUNT_FIELDS}
    count["production"] = production
    count["calibration"] = calibration
    annotators = {e["user_id"] for e in events if e.get("user_id")}
    return count, annotators


def fetch_responses(client, http: "httpx.Client", settings) -> dict[Task, list[dict]]:
    """Submitted-response events per task, via the Argilla REST records endpoint.

    The SDK drops per-response timestamps; the REST payload keeps them. Returns
    ``{Task: [{user_id, at: datetime, purpose, record_id}, …]}`` for submitted
    responses only.
    """
    out: dict[Task, list[dict]] = {t: [] for t in TASKS}
    for ws_name, wss in settings.workspaces.items():
        for task in wss.tasks:
            for cal in (False, True):
                name = dataset_name(task, calibration=cal, dataset_id=settings.dataset_id)
                ds = client.datasets(name=name, workspace=ws_name)
                if ds is None:
                    continue
                purpose = "calibration" if cal else "production"
                offset, limit = 0, 1000
                while True:
                    r = http.get(
                        f"/api/v1/datasets/{ds.id}/records",
                        params={"include": "responses", "response_statuses": "submitted",
                                "limit": limit, "offset": offset},
                    )
                    r.raise_for_status()
                    items = r.json().get("items", [])
                    for rec in items:
                        for resp in (rec.get("responses") or []):
                            ts = resp.get("inserted_at")
                            if resp.get("status") == "submitted" and ts:
                                out[task].append({
                                    "user_id": str(resp.get("user_id")),
                                    "at": datetime.fromisoformat(ts),
                                    "purpose": purpose,
                                    "record_id": str(resp.get("record_id")),
                                })
                    offset += len(items)
                    if len(items) < limit:
                        break
    return out


def task_agreement(task_agr) -> tuple[dict, list[tuple[float, int]]]:
    """Per-label alpha + n_items-weighted mean, from a TaskAgreement (or None)."""
    if task_agr is None:
        return {"per_label": {}, "mean_alpha": None, "n_labels": 0}, []
    per_label = {}
    weighted: list[tuple[float, int]] = []
    for lab in task_agr.labels:
        per_label[lab.label] = {
            "alpha": lab.alpha,
            "n_items": lab.n_items,
            "n_annotators": lab.n_annotators,
            "pct_agreement": lab.pct_agreement,
        }
        if lab.alpha is not None and lab.n_items > 0:
            weighted.append((lab.alpha, lab.n_items))
    return {"per_label": per_label, "mean_alpha": wmean(weighted), "n_labels": len(weighted)}, weighted


# --- per-domain orchestration ----------------------------------------------

def sanitized_config(domain: str) -> tuple[Path, dict]:
    """Write a temp copy of the domain config with non-schema keys dropped.

    Returns (temp path, cleaned dict). Leaves the shared config untouched (the
    demo import path needs the extra keys).
    """
    raw = yaml.safe_load((ws.CONFIGS_DIR / f"{domain}.yaml").read_text()) or {}
    dropped = sorted(k for k in raw if k not in _VALID_CONFIG_KEYS)
    clean = {k: v for k, v in raw.items() if k in _VALID_CONFIG_KEYS}
    if dropped:
        log(f"  (dropped non-schema config keys: {', '.join(dropped)})")
    fd, path = tempfile.mkstemp(prefix=f"monitor_{domain}_", suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(clean, f, sort_keys=False)
    return Path(path), clean


def fetch_progress(client, settings) -> dict[Task, dict]:
    """Per-task record-level progress from Argilla ``dataset.progress()``.

    Returns ``{Task: {"production": {total, completed, pending}|absent,
    "calibration": ...}}``. Datasets that don't exist are simply absent.
    """
    out: dict[Task, dict] = {t: {} for t in TASKS}
    for ws_name, wss in settings.workspaces.items():
        for task in wss.tasks:
            for cal in (False, True):
                name = dataset_name(task, calibration=cal, dataset_id=settings.dataset_id)
                try:
                    ds = client.datasets(name=name, workspace=ws_name)
                    if ds is None:
                        continue
                    prog = dict(ds.progress())
                except Exception as e:
                    log(f"  ! progress({ws_name}/{name}) failed: {type(e).__name__}: {e}")
                    continue
                out[task]["calibration" if cal else "production"] = prog
    return out


def process_domain(domain: str, client, http: "httpx.Client") -> dict:
    """Assemble one domain's metrics: counts (progress + REST), agreement (IAA),
    cadence (REST per-response timestamps).

    Raises on a hard fetch failure. IAA failure is caught locally so counts/timing
    still surface.
    """
    base_dir = str(ws.ROOT)
    cfg_path, clean_cfg = sanitized_config(domain)
    try:
        cfg = str(cfg_path)
        settings = AnnotationSettings.resolve(
            config=clean_cfg,
            overrides={"argilla": {"api_url": os.environ.get("ARGILLA_API_URL")}},
        )
        progress = fetch_progress(client, settings)
        responses = fetch_responses(client, http, settings)

        # Export must run so compute_iaa has CSVs to read (we don't read them here).
        export_annotations(config_path=cfg, export_id=EXPORT_ID, base_dir=base_dir,
                           include_discarded=False)

        agr_by_task: dict = {}
        iaa_error: str | None = None
        try:
            report = compute_iaa(EXPORT_ID, config_path=cfg, base_dir=base_dir,
                                 tasks=TASKS, n_resamples=IAA_RESAMPLES)
            agr_by_task = {ta.task: ta for ta in report.tasks}
        except Exception as e:  # IAA-only failure shouldn't sink the counts
            iaa_error = f"{type(e).__name__}: {e}"
            log(f"  ! {domain}: IAA failed ({iaa_error}); reporting counts/timing only")
    finally:
        cfg_path.unlink(missing_ok=True)

    tasks_out: dict = {}
    dom_counts = empty_counts()
    dom_annotators: set[str] = set()
    dom_weighted: list[tuple[float, int]] = []
    dom_events: list[dict] = []

    for task in TASKS:
        events = responses.get(task, [])
        count, annotators = task_counts(events, progress.get(task, {}))
        agr, weighted = task_agreement(agr_by_task.get(task))

        tasks_out[task.value] = {
            "count": {**count, "n_annotators": len(annotators)},
            "agreement": agr,
            "timing": cadence_report(events),
        }
        add_counts(dom_counts, count)
        dom_annotators |= annotators
        dom_weighted.extend(weighted)
        dom_events.extend(events)

    block = {
        "count": {**dom_counts, "n_annotators": len(dom_annotators)},
        "agreement": {"mean_alpha": wmean(dom_weighted), "n_labels": len(dom_weighted)},
        "timing": cadence_report(dom_events),
        "tasks": tasks_out,
    }
    if iaa_error:
        block["iaa_error"] = iaa_error
    # Carried out-of-band for total rollup (not serialized at domain level).
    block["_rollup"] = {"weighted": dom_weighted, "counts": dom_counts,
                        "annotators": dom_annotators, "events": dom_events}
    return block


def run(domains: list[str]) -> dict:
    url = os.environ.get("ARGILLA_API_URL")
    key = os.environ["ARGILLA_API_KEY"]
    client = resolve_argilla_client(url, key)
    USER_LOOKUP.update({str(uid): name for uid, name in build_user_lookup(client).items()})

    domains_out: dict = {}
    tot_counts = empty_counts()
    tot_annotators: set[str] = set()
    tot_weighted: list[tuple[float, int]] = []
    tot_events: list[dict] = []

    with httpx.Client(base_url=url, headers={"X-Argilla-Api-Key": key}, timeout=60.0) as http:
        for domain in domains:
            log(f"=== {domain} ===")
            try:
                block = process_domain(domain, client, http)
            except Exception as e:
                domains_out[domain] = {"error": f"{type(e).__name__}: {e}"}
                log(f"  ! {domain}: {type(e).__name__}: {e}")
                continue
            roll = block.pop("_rollup")
            domains_out[domain] = block
            add_counts(tot_counts, roll["counts"])
            tot_annotators |= roll["annotators"]
            tot_weighted.extend(roll["weighted"])
            tot_events.extend(roll["events"])

    return {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "session_gap_threshold_s": int(SESSION_GAP_S),
        "total": {
            "count": {**tot_counts, "n_annotators": len(tot_annotators)},
            "agreement": {"mean_alpha": wmean(tot_weighted), "n_labels": len(tot_weighted)},
            "timing": cadence_report(tot_events),
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
    %=completion, ann=annotators, mean_a=mean calibration alpha, a-gap=per-annotator
    active cadence (median gap within an annotator's own stream, session-guarded).
    """
    print(f"Annotation monitor — {result['run_at']}")
    print(f"(session gap threshold: {result['session_gap_threshold_s'] // 60} min)\n")
    hdr = (f"{'domain':<34} {'resp':>6} {'done':>7} {'total':>8} {'%':>5} "
           f"{'ann':>4} {'mean_a':>7} {'a-gap':>7}")
    print(hdr)
    print("-" * len(hdr))

    def row(name: str, block: dict) -> None:
        if "error" in block:
            print(f"{name:<34} {'ERROR: ' + block['error'][:60]}")
            return
        c, a, t = block["count"], block["agreement"], block["timing"]
        total, done = c["total_records"], c["completed_records"]
        pct = f"{100 * done / total:.0f}%" if total else "—"
        agap = t["per_annotator"]["pooled_median_active_gap_s"]
        print(f"{name:<34} {c['submitted_responses']:>6} {done:>7} {total:>8} {pct:>5} "
              f"{c['n_annotators']:>4} {_fmt_alpha(a['mean_alpha']):>7} {_fmt_gap(agap):>7}")

    for name, block in result["domains"].items():
        row(name, block)
    print("-" * len(hdr))
    row("TOTAL", result["total"])
    print("\na-gap = median active gap between an annotator's consecutive "
          "submissions (session-guarded). Global cadence + breakdowns in the JSONL.")


def append_jsonl(result: dict) -> None:
    ws.LOGS_DIR.mkdir(exist_ok=True)
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
        ("d", base + timedelta(hours=16)),          # overnight pause > 30m
        ("e", base + timedelta(hours=16, minutes=4)),
    ]
    r = cadence(recs, threshold_s=1800, min_records=5)
    assert r["n_gaps_total"] == 4, r
    assert r["n_gaps_used"] == 3, r            # 5m, 5m, 4m kept
    assert r["n_pause_breaks"] == 1, r
    assert r["n_sessions"] == 2, r
    assert r["median_active_gap_s"] == 300.0, r  # median([300, 300, 240])
    assert len(r["excluded_gaps"]) == 1 and r["excluded_gaps"][0]["after_record"] == "c", r
    assert r["longest_pause_s"] == r["excluded_gaps"][0]["gap_s"], r
    # Below min_records → median suppressed, but breaks still reported.
    r2 = cadence(recs[:3], threshold_s=1800, min_records=5)
    assert r2["median_active_gap_s"] is None and r2["n_gaps_used"] == 2, r2
    print("self-check OK")
    return 0


# --- main -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--domain", help="Process only this domain (smoke test).")
    ap.add_argument("--no-jsonl", action="store_true", help="Don't append to logs/monitor.jsonl.")
    ap.add_argument("--self-check", action="store_true", help="Run the cadence self-check and exit.")
    args = ap.parse_args()

    if args.self_check:
        return self_check()

    log(f"pragmata: {Path(pragmata.__file__).resolve().parent}")

    domains = [args.domain] if args.domain else ws.domains()
    if not domains:
        log("No domains found under annotation_configs/")
        return 1

    result = run(domains)
    if not args.no_jsonl:
        append_jsonl(result)
    print_summary(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
