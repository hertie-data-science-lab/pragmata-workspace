#!/usr/bin/env python
"""One-off: prune live Argilla down to an "essential" scope per domain/task.

plan   (read-only) -- for the 7 staffed domains, tier-select which retrieval
       panels and grounding records to KEEP (calibration always kept, then
       anything with existing progress unless that alone exceeds the buffer,
       then cheapest zero-touched top-up); for zentrum-fuer-datenmanagement,
       mark everything (all 3 tasks) for drop. Writes plan.json (drop-lists)
       and report.md (human review gate) under argilla_prune/<ts>/.

apply <plan_dir> [--workspace WS ...] [--apply] -- re-fetches live record ids
       for the targeted datasets, intersects with the plan's drop-ids (tolerant
       of drift/re-runs), previews per-dataset delete counts, and only mutates
       with --apply. Writes apply_log.jsonl for post-hoc diffing against the
       backup.

Not part of the tracked pipeline - lives in tmp/ (gitignored via
.git/info/exclude), used once for the 2026-07 Argilla resource-reduction
exercise. Mirrors scripts/annotation/argilla_backup.py's conventions
(_client(), preview-then---apply).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
import workspace as ws  # noqa: E402

ws.load_env()  # configs/settings.conf + .env; existing env wins

# Same PRAGMATA_SRC shadowing as scripts/annotation/log.py - the annotation
# data was imported by the demo-branch pragmata (partition_scope topology),
# so it must be read back through the same branch.
_PRAGMATA_SRC = os.environ.get("PRAGMATA_SRC")
if _PRAGMATA_SRC:
    sys.path.insert(0, _PRAGMATA_SRC)

import argilla as rg  # noqa: E402
import pragmata  # noqa: E402,F401
from pragmata.core.annotation.argilla_task_definitions import dataset_name  # noqa: E402
from pragmata.core.annotation.export_fetcher import (  # noqa: E402
    resolve_task_purposes,
    walk_retrieval_records,
)
from pragmata.core.annotation.panel_status import compute_panel_status  # noqa: E402
from pragmata.core.schemas.annotation_task import Task  # noqa: E402
from pragmata.core.settings.annotation_settings import AnnotationSettings  # noqa: E402

PRUNE_ROOT = ws.ROOT / "argilla_prune"
ZFD_DOMAIN = "zentrum-fuer-datenmanagement"
DEFAULT_BUFFER = 90
_VALID_CONFIG_KEYS = set(AnnotationSettings.model_fields)


def _client() -> rg.Argilla:
    url = os.environ.get("ARGILLA_API_URL")
    key = os.environ.get("ARGILLA_API_KEY")
    if not (url and key):
        sys.exit("missing ARGILLA_API_URL / ARGILLA_API_KEY (set in .env)")
    print(f"connecting to {url}")
    return rg.Argilla(api_url=url, api_key=key)


def _load_settings(domain: str) -> AnnotationSettings:
    raw = yaml.safe_load((ws.DOMAINS_DIR / f"{domain}.yaml").read_text()) or {}
    clean = {k: v for k, v in raw.items() if k in _VALID_CONFIG_KEYS}
    return AnnotationSettings.resolve(
        config=clean,
        overrides={"argilla": {"api_url": os.environ.get("ARGILLA_API_URL")}},
    )


# --- plan data model ----------------------------------------------------------


@dataclass
class DomainPlan:
    domain: str
    task: str
    workspace: str
    # (dataset_name) -> list of record-id strings to drop
    drop_ids: dict[str, list[str]] = field(default_factory=dict)
    # reporting
    total_units: int = 0
    n_tier0_calibration: int = 0
    n_tier1_kept: int = 0
    n_tier1_dropped: int = 0
    n_tier2_kept: int = 0
    n_kept: int = 0
    n_dropped: int = 0
    tier0_over_buffer: bool = False
    k_bucket_kept: dict[str, int] = field(default_factory=lambda: {"k<=5": 0, "k=6-9": 0, "k>9": 0})
    k_bucket_dropped: dict[str, int] = field(default_factory=lambda: {"k<=5": 0, "k=6-9": 0, "k>9": 0})
    integrity_mismatches: list[str] = field(default_factory=list)


def _k_bucket(k: int) -> str:
    if k <= 5:
        return "k<=5"
    if k <= 9:
        return "k=6-9"
    return "k>9"


# --- retrieval: tiered panel selection -----------------------------------------


def _tiered_select(tier0, tier1, tier2, buffer):
    """Shared tiering logic. Each tier item is (key, cost). Returns (keep_set, tier0_over_buffer)."""
    tier0_sorted = sorted(tier0, key=lambda x: x[1])
    tier0_over_buffer = len(tier0_sorted) > buffer
    if tier0_over_buffer:
        keep = {k for k, _ in tier0_sorted[:buffer]}
        return keep, True

    keep = {k for k, _ in tier0_sorted}
    remaining = buffer - len(keep)

    tier1_sorted = sorted(tier1, key=lambda x: x[1])
    if len(tier1_sorted) > remaining:
        keep |= {k for k, _ in tier1_sorted[:remaining]}
        remaining = 0
    else:
        keep |= {k for k, _ in tier1_sorted}
        remaining -= len(tier1_sorted)

    if remaining > 0:
        tier2_sorted = sorted(tier2, key=lambda x: x[1])
        keep |= {k for k, _ in tier2_sorted[:remaining]}

    return keep, False


def build_retrieval_plan(client: rg.Argilla, domain: str, buffer: int) -> DomainPlan | None:
    settings = _load_settings(domain)
    workspace_name, _purposes = resolve_task_purposes(settings, Task.RETRIEVAL)
    if workspace_name is None:
        return None

    snapshots = walk_retrieval_records(client, settings)
    status_report = compute_panel_status(client, workspace=workspace_name, task="retrieval")

    by_uuid: dict[str, list] = defaultdict(list)
    for snap in snapshots:
        if snap.record_uuid:
            by_uuid[snap.record_uuid].append(snap)

    plan = DomainPlan(domain=domain, task="retrieval", workspace=workspace_name)
    plan.total_units = len(by_uuid)

    tier0, tier1, tier2 = [], [], []
    panel_k: dict[str, int] = {}
    for uuid, snaps in by_uuid.items():
        panel = status_report.panels.get((workspace_name, uuid))
        if panel is None:
            plan.integrity_mismatches.append(f"{uuid}: no panel_status entry (orphan?)")
            continue
        live_k_from_snapshots = len({s.chunk_id for s in snaps})
        if live_k_from_snapshots != panel.k_records:
            plan.integrity_mismatches.append(
                f"{uuid}: snapshot k={live_k_from_snapshots} vs panel_status k_records={panel.k_records}"
            )
        panel_k[uuid] = panel.k_records
        cost = panel.k_records - panel.n_submitted
        has_cal_chunk = any(s.calibration for s in snaps)
        if has_cal_chunk:
            tier0.append((uuid, cost))
        elif panel.n_submitted > 0:
            tier1.append((uuid, cost))
        else:
            tier2.append((uuid, panel.k_records))

    keep, tier0_over_buffer = _tiered_select(tier0, tier1, tier2, buffer)
    plan.tier0_over_buffer = tier0_over_buffer
    plan.n_tier0_calibration = len(tier0) if not tier0_over_buffer else len(keep)
    tier1_uuids = {u for u, _ in tier1}
    plan.n_tier1_kept = len(tier1_uuids & keep)
    plan.n_tier1_dropped = len(tier1_uuids - keep)
    tier2_uuids = {u for u, _ in tier2}
    plan.n_tier2_kept = len(tier2_uuids & keep)

    for uuid, snaps in by_uuid.items():
        if uuid not in panel_k:
            continue
        bucket = _k_bucket(panel_k[uuid])
        target = plan.k_bucket_kept if uuid in keep else plan.k_bucket_dropped
        target[bucket] += 1
        if uuid not in keep:
            for snap in snaps:
                ds_name = dataset_name(Task.RETRIEVAL, calibration=snap.calibration, dataset_id=settings.dataset_id)
                plan.drop_ids.setdefault(ds_name, []).append(str(snap.record.id))

    plan.n_kept = len(keep)
    plan.n_dropped = plan.total_units - len(keep)
    return plan


# --- grounding: tiered record selection ----------------------------------------


def build_grounding_plan(client: rg.Argilla, domain: str, buffer: int) -> DomainPlan | None:
    settings = _load_settings(domain)
    workspace_name, purposes = resolve_task_purposes(settings, Task.GROUNDING)
    if workspace_name is None:
        return None

    plan = DomainPlan(domain=domain, task="grounding", workspace=workspace_name)

    tier0, tier1, tier2 = [], [], []
    by_key: dict[str, tuple] = {}  # record_id_str -> (record, ds_name)
    for calibration in purposes:
        ds_name = dataset_name(Task.GROUNDING, calibration=calibration, dataset_id=settings.dataset_id)
        dataset = client.datasets(ds_name, workspace=workspace_name)
        if dataset is None:
            continue
        min_submitted = dataset.settings.distribution.min_submitted
        for rec in dataset.records(with_responses=True):
            rid = str(rec.id)
            n_submitted = sum(1 for r in (rec.responses or []) if getattr(r, "status", None) == "submitted")
            by_key[rid] = (rec, ds_name)
            remaining = max(0, min_submitted - n_submitted)
            if calibration:
                tier0.append((rid, remaining))
            elif n_submitted > 0:
                tier1.append((rid, remaining))
            else:
                tier2.append((rid, remaining))

    plan.total_units = len(by_key)
    keep, tier0_over_buffer = _tiered_select(tier0, tier1, tier2, buffer)
    plan.tier0_over_buffer = tier0_over_buffer
    plan.n_tier0_calibration = len(tier0) if not tier0_over_buffer else len(keep)
    tier1_ids = {rid for rid, _ in tier1}
    plan.n_tier1_kept = len(tier1_ids & keep)
    plan.n_tier1_dropped = len(tier1_ids - keep)
    tier2_ids = {rid for rid, _ in tier2}
    plan.n_tier2_kept = len(tier2_ids & keep)

    for rid, (rec, ds_name) in by_key.items():
        if rid not in keep:
            plan.drop_ids.setdefault(ds_name, []).append(rid)

    plan.n_kept = len(keep)
    plan.n_dropped = plan.total_units - len(keep)
    return plan


# --- ZfD: full descope, all 3 tasks --------------------------------------------


def build_zfd_plans(client: rg.Argilla) -> list[DomainPlan]:
    settings = _load_settings(ZFD_DOMAIN)
    plans = []
    for task in (Task.RETRIEVAL, Task.GROUNDING, Task.GENERATION):
        workspace_name, purposes = resolve_task_purposes(settings, task)
        if workspace_name is None:
            continue
        plan = DomainPlan(domain=ZFD_DOMAIN, task=str(task), workspace=workspace_name)
        for calibration in purposes:
            ds_name = dataset_name(task, calibration=calibration, dataset_id=settings.dataset_id)
            dataset = client.datasets(ds_name, workspace=workspace_name)
            if dataset is None:
                continue
            ids = [str(r.id) for r in dataset.records()]
            plan.drop_ids[ds_name] = ids
            plan.total_units += len(ids)
        plan.n_dropped = plan.total_units
        plan.n_kept = 0
        plans.append(plan)
    return plans


# --- plan command ---------------------------------------------------------------


def cmd_plan(domains: list[str], buffer: int, out_dir: Path | None) -> None:
    client = _client()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = out_dir or (PRUNE_ROOT / ts)
    out.mkdir(parents=True, exist_ok=True)

    staffed = [d for d in domains if d != ZFD_DOMAIN]
    all_plans: list[DomainPlan] = []

    for domain in staffed:
        print(f"planning {domain} retrieval...")
        rp = build_retrieval_plan(client, domain, buffer)
        if rp:
            all_plans.append(rp)
        print(f"planning {domain} grounding...")
        gp = build_grounding_plan(client, domain, buffer)
        if gp:
            all_plans.append(gp)

    if ZFD_DOMAIN in domains:
        print(f"planning {ZFD_DOMAIN} (full descope, all 3 tasks)...")
        all_plans.extend(build_zfd_plans(client))

    plan_json = {
        "created_utc": ts,
        "buffer": buffer,
        "domains": staffed + ([ZFD_DOMAIN] if ZFD_DOMAIN in domains else []),
        "datasets": [
            {
                "domain": p.domain,
                "task": p.task,
                "workspace": p.workspace,
                "drop_ids": p.drop_ids,
            }
            for p in all_plans
        ],
    }
    (out / "plan.json").write_text(json.dumps(plan_json, indent=2))

    report_lines = [f"# Argilla prune plan — {ts}", "", f"Buffer: {buffer}", ""]
    total_kept = total_dropped = 0
    for p in all_plans:
        total_kept += p.n_kept
        total_dropped += p.n_dropped
        report_lines.append(f"## {p.domain} / {p.task}")
        report_lines.append(f"- workspace: `{p.workspace}`")
        report_lines.append(f"- total units: {p.total_units}")
        if p.domain == ZFD_DOMAIN:
            report_lines.append(f"- **full descope: {p.n_dropped} dropped, 0 kept**")
        else:
            report_lines.append(
                f"- tier0 (calibration) kept: {p.n_tier0_calibration}"
                + (" **[OVER BUFFER — safety valve triggered]**" if p.tier0_over_buffer else "")
            )
            report_lines.append(f"- tier1 (any progress) kept/dropped: {p.n_tier1_kept}/{p.n_tier1_dropped}")
            report_lines.append(f"- tier2 (zero-touched) kept: {p.n_tier2_kept}")
            report_lines.append(f"- **total kept: {p.n_kept} / dropped: {p.n_dropped}**")
            if p.task == "retrieval":
                report_lines.append(f"- k-bucket kept: {p.k_bucket_kept}")
                report_lines.append(f"- k-bucket dropped: {p.k_bucket_dropped}")
            if p.integrity_mismatches:
                report_lines.append(f"- **{len(p.integrity_mismatches)} integrity mismatches** (see plan.json details below)")
                for m in p.integrity_mismatches[:10]:
                    report_lines.append(f"  - {m}")
                if len(p.integrity_mismatches) > 10:
                    report_lines.append(f"  - ... and {len(p.integrity_mismatches) - 10} more")
        report_lines.append("")

    report_lines.insert(3, f"**TOTAL kept: {total_kept} / dropped: {total_dropped}**\n")
    (out / "report.md").write_text("\n".join(report_lines))

    print(f"\nplan written: {out}")
    print(f"  {out / 'plan.json'}")
    print(f"  {out / 'report.md'}")
    print(f"\nTOTAL kept: {total_kept} / dropped: {total_dropped}")


# --- apply command ---------------------------------------------------------------


def cmd_apply(plan_dir: Path, workspace_filter: list[str] | None, apply: bool) -> None:
    plan_json = json.loads((plan_dir / "plan.json").read_text())
    client = _client()

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit_path = plan_dir / "apply_log.jsonl"
    audit_lines = []

    grand_would_delete = grand_matched = 0
    for entry in plan_json["datasets"]:
        if workspace_filter and entry["workspace"] not in workspace_filter:
            continue
        for ds_name, ids in entry["drop_ids"].items():
            if not ids:
                continue
            dataset = client.datasets(name=ds_name, workspace=entry["workspace"])
            if dataset is None:
                print(f"  {entry['workspace']}/{ds_name}: dataset not found (skipping)")
                continue
            live_ids = {str(r.id): r for r in dataset.records()}
            plan_ids = set(ids)
            present = plan_ids & set(live_ids)
            already_gone = plan_ids - set(live_ids)
            grand_would_delete += len(plan_ids)
            grand_matched += len(present)
            print(
                f"  {entry['workspace']}/{ds_name}: would delete {len(present)}/{len(plan_ids)} "
                f"(already gone: {len(already_gone)})"
            )
            if apply and present:
                records_to_delete = [live_ids[rid] for rid in present]
                dataset.records.delete(records_to_delete, batch_size=64)
                for rid in present:
                    audit_lines.append(
                        json.dumps(
                            {
                                "workspace": entry["workspace"],
                                "dataset": ds_name,
                                "domain": entry["domain"],
                                "task": entry["task"],
                                "record_id": rid,
                            }
                        )
                    )
                print(f"    deleted {len(present)} records")

    print(f"\nTOTAL: would delete {grand_matched}/{grand_would_delete} (scoped to filter: {workspace_filter or 'ALL'})")

    if not apply:
        print("(preview only; pass --apply to mutate)")
        return

    with audit_path.open("a") as f:
        for line in audit_lines:
            f.write(line + "\n")
    print(f"audit log appended: {audit_path} ({len(audit_lines)} lines)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan")
    p_plan.add_argument("--domain", action="append", dest="domains", help="repeatable; default: all")
    p_plan.add_argument("--buffer", type=int, default=DEFAULT_BUFFER)
    p_plan.add_argument("--out", type=Path, default=None)

    p_apply = sub.add_parser("apply")
    p_apply.add_argument("plan_dir", type=Path)
    p_apply.add_argument("--workspace", action="append", dest="workspaces")
    p_apply.add_argument("--apply", action="store_true")

    args = ap.parse_args()

    if args.cmd == "plan":
        domains = args.domains or ws.domains()
        cmd_plan(domains, args.buffer, args.out)
    elif args.cmd == "apply":
        cmd_apply(args.plan_dir, args.workspaces, args.apply)


if __name__ == "__main__":
    main()
