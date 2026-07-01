#!/usr/bin/env python
"""Round-2 curation: offline selection + diff engine (read-only, pure stdlib).

Computes, per domain, the target "essential" set and diffs it against the current
live state, emitting the restore + delete + reproducibility artifacts. No server
access, no pragmata import -> deterministic and safe to run repeatedly.

Target rule (see plan we-have-a-problem-sprightly-locket.md):
  - calibration: kept as-is (untouched); ZfD restored from universe.
  - keep ALL completed production records (never discard finished work).
  - grounding/generation: keep all completed + top up UNFINISHED to ~40
    completable (0 top-up if completed >= 40). Unfinished = 0 submissions ->
    zero work lost.
  - retrieval: ~40 completable PANELS total = force-keep calibration panels
    (whole) + cheapest-to-finish pure-production panels (<=CUTOFF remaining),
    steered toward panels whose grounding+generation are already done.
  - soft cross-task consistency: grounding/generation top-up prefers the
    retrieval-kept panels, maximising complete triples.

Outputs under <out>/:
  plan.json                         argilla_prune drop-lists (production only)
  restore/<ws>__<dataset>.ids       ids to restore (target - live)
  target.json                       per-domain target uuids + id sets
  report.md                         human review gate

Usage:
  tmp/round2_select.py plan --universe argilla_backup/<pre-round1> \
      --live argilla_backup/<current> --out tmp/round2/<ts> \
      [--target 40] [--ret-cutoff 5]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

TASKS = ["retrieval", "grounding", "generation"]


def _load_manifest(backup_dir: Path) -> dict[tuple[str, str], Path]:
    """(workspace, dataset_name) -> records_full.json path."""
    man = json.loads((backup_dir / "manifest.json").read_text())
    out = {}
    for d in man["datasets"]:
        out[(d["workspace"], d["name"])] = Path(d["path"]) / "records_full.json"
    return out


def _recs(idx: dict, ws: str, name: str) -> list[dict]:
    p = idx.get((ws, name))
    if p is None or not p.exists():
        return []
    return json.loads(p.read_text())


def _submitted(rec: dict) -> bool:
    return any(x.get("status") == "submitted" for x in (rec.get("responses") or []))


def _domains(idx: dict) -> list[str]:
    """Domain prefixes derived from workspace names (<Prefix>_<task>)."""
    prefixes = set()
    for ws, _name in idx:
        base, _, task = ws.rpartition("_")
        if task in TASKS:
            prefixes.add(base)
    return sorted(prefixes)


class DomainData:
    """All per-query facts for one domain, from a backup dir."""

    def __init__(self, idx: dict, prefix: str):
        self.prefix = prefix
        rp = f"{prefix}_retrieval"
        gp = f"{prefix}_grounding"
        np_ = f"{prefix}_generation"

        # retrieval panels
        self.panel_prod_ids: dict[str, list[str]] = defaultdict(list)  # uuid -> prod chunk ids
        self.panel_prod_done: dict[str, int] = defaultdict(int)        # submitted prod chunks
        self.ret_cal_uuids: set[str] = set()
        self.ret_cal_ids: dict[str, list[str]] = defaultdict(list)
        for r in _recs(idx, rp, "retrieval_production"):
            u = r.get("metadata", {}).get("record_uuid", "")
            if not u:
                continue
            self.panel_prod_ids[u].append(r["id"])
            if _submitted(r):
                self.panel_prod_done[u] += 1
        for r in _recs(idx, rp, "retrieval_calibration"):
            u = r.get("metadata", {}).get("record_uuid", "")
            if not u:
                continue
            self.ret_cal_uuids.add(u)
            self.ret_cal_ids[u].append(r["id"])

        # grounding / generation (1 record per uuid per purpose)
        self.gnd_prod_done: dict[str, bool] = {}
        self.gnd_cal_uuids: set[str] = set()
        for r in _recs(idx, gp, "grounding_production"):
            u = r.get("metadata", {}).get("record_uuid", "")
            if u:
                self.gnd_prod_done[u] = _submitted(r)
        for r in _recs(idx, gp, "grounding_calibration"):
            u = r.get("metadata", {}).get("record_uuid", "")
            if u:
                self.gnd_cal_uuids.add(u)

        self.gen_prod_done: dict[str, bool] = {}
        self.gen_cal_uuids: set[str] = set()
        for r in _recs(idx, np_, "generation_production"):
            u = r.get("metadata", {}).get("record_uuid", "")
            if u:
                self.gen_prod_done[u] = _submitted(r)
        for r in _recs(idx, np_, "generation_calibration"):
            u = r.get("metadata", {}).get("record_uuid", "")
            if u:
                self.gen_cal_uuids.add(u)

        self.cal_union = self.ret_cal_uuids | self.gnd_cal_uuids | self.gen_cal_uuids
        # eligible = production in all 3 tasks (has a retrieval panel, not calibration anywhere)
        self.eligible = {u for u in self.panel_prod_ids if u not in self.cal_union}

    def ret_remaining(self, u: str) -> int:
        return len(self.panel_prod_ids[u]) - self.panel_prod_done[u]

    def ret_k(self, u: str) -> int:
        return len(self.panel_prod_ids[u])

    def ret_progress(self, u: str) -> int:
        return self.panel_prod_done[u]


def select_domain(prefix: str, uni: DomainData, target: int, cutoff: int) -> dict:
    """Compute target sets for one domain (from the universe = full pre-round-1)."""
    # --- retrieval: 70 complete panels = ALL calibration-straddling panels
    #     (kept WHOLE and driven to completion, incl their calibration chunk at
    #     min_submitted) + `target` (40) pure-production completable panels.
    #     Calibration's 30 items are always kept and completed. Non-kept panels'
    #     production chunks are discarded (non-consolidated partial work).
    ret_cal_panels = set(uni.ret_cal_uuids)  # the ~30 panels the 30 cal items open
    cands = [u for u in uni.eligible if uni.ret_remaining(u) <= cutoff]

    def ret_key(u: str):
        gg_done = 1 if (uni.gnd_prod_done.get(u) and uni.gen_prod_done.get(u)) else 0
        return (-gg_done, uni.ret_remaining(u), -uni.ret_progress(u), uni.ret_k(u), u)

    cands.sort(key=ret_key)
    ret_pure_kept = cands[:target]  # 40 pure-production completable panels
    ret_target_panels = ret_cal_panels | set(ret_pure_kept)  # ~70 complete panels total

    # --- grounding/generation: keep all completed + the retrieval-kept pure-prod
    #     queries (to complete triples) + top up unfinished to `target` ---
    ret_pure_set = set(ret_pure_kept)

    def record_task_target(done_map: dict[str, bool], cal_uuids: set[str]) -> tuple[set[str], set[str]]:
        prod_uuids = [u for u in done_map]  # production records (not calibration)
        completed = {u for u in prod_uuids if done_map[u]}
        # force-include retrieval-kept pure-prod queries that exist as production here,
        # so every kept retrieval panel can become a complete triple (soft consistency).
        keep = set(completed) | (ret_pure_set & set(prod_uuids))
        if len(keep) < target:
            unfinished = [u for u in prod_uuids if not done_map[u] and u not in keep]
            unfinished.sort()  # deterministic
            for u in unfinished:
                if len(keep) >= target:
                    break
                keep.add(u)
        return keep, completed

    gnd_keep, gnd_completed = record_task_target(uni.gnd_prod_done, uni.gnd_cal_uuids)
    gen_keep, gen_completed = record_task_target(uni.gen_prod_done, uni.gen_cal_uuids)

    # --- target record-id sets per dataset ---
    ret_prod_ids = []
    dropped_ret_done = 0  # submitted chunks on dropped panels (work loss)
    kept_ret_done = 0
    for u, ids in uni.panel_prod_ids.items():
        if u in ret_target_panels:
            ret_prod_ids.extend(ids)
            kept_ret_done += uni.panel_prod_done[u]
        else:
            dropped_ret_done += uni.panel_prod_done[u]

    gnd_prod_ids = [f"gnd-{u}" for u in gnd_keep]
    gen_prod_ids = [f"gen-{u}" for u in gen_keep]

    # triples = queries production-kept in all 3 tasks
    triples = ret_pure_kept and (set(ret_pure_kept) & gnd_keep & gen_keep) or set()

    return {
        "prefix": prefix,
        "ret_target_panels": sorted(ret_target_panels),
        "ret_pure_kept": sorted(ret_pure_kept),
        "ret_cal_panels": sorted(uni.ret_cal_uuids),
        "ret_prod_ids": sorted(ret_prod_ids),
        "gnd_keep": sorted(gnd_keep),
        "gen_keep": sorted(gen_keep),
        "gnd_completed": len(gnd_completed),
        "gen_completed": len(gen_completed),
        "gnd_prod_ids": sorted(gnd_prod_ids),
        "gen_prod_ids": sorted(gen_prod_ids),
        "cal_union": sorted(uni.cal_union),
        "n_triples": len(set(ret_pure_kept) & gnd_keep & gen_keep),
        "kept_ret_done": kept_ret_done,
        "dropped_ret_done": dropped_ret_done,
        # k-bucket of kept vs dropped panels (selection-bias view)
        "kbucket_kept": _kbucket(uni, ret_target_panels),
        "kbucket_dropped": _kbucket(uni, set(uni.panel_prod_ids) - ret_target_panels),
    }


def _kbucket(uni: DomainData, uuids: set[str]) -> dict:
    b = {"k<=5": 0, "k6-9": 0, "k>9": 0}
    for u in uuids:
        k = uni.ret_k(u)
        b["k<=5" if k <= 5 else ("k6-9" if k <= 9 else "k>9")] += 1
    return b


def cmd_plan(universe: Path, live: Path, out: Path, target: int, cutoff: int) -> None:
    uni_idx = _load_manifest(universe)
    live_idx = _load_manifest(live)
    out.mkdir(parents=True, exist_ok=True)
    (out / "restore").mkdir(exist_ok=True)

    domains = _domains(uni_idx)
    plan_datasets = []
    target_json = {}
    report = [f"# Round-2 selection — universe={universe.name} live={live.name}",
              f"\nTarget: {target} completable production/task; retrieval cutoff <= {cutoff} remaining chunks.\n"]

    def live_ids(ws, name):
        return {r["id"] for r in _recs(live_idx, ws, name)}

    def uni_ids(ws, name):
        return {r["id"] for r in _recs(uni_idx, ws, name)}

    tot_restore = tot_delete = 0
    for prefix in domains:
        uni = DomainData(uni_idx, prefix)
        sel = select_domain(prefix, uni, target, cutoff)
        target_json[prefix] = sel

        rp, gp, np_ = f"{prefix}_retrieval", f"{prefix}_grounding", f"{prefix}_generation"
        # production target id sets
        targets = {
            (rp, "retrieval_production"): set(sel["ret_prod_ids"]),
            (gp, "grounding_production"): set(sel["gnd_prod_ids"]),
            (np_, "generation_production"): set(sel["gen_prod_ids"]),
        }
        # calibration target = keep as-is (universe cal ids) — restore for ZfD, no drop
        cal_targets = {
            (rp, "retrieval_calibration"): uni_ids(rp, "retrieval_calibration"),
            (gp, "grounding_calibration"): uni_ids(gp, "grounding_calibration"),
            (np_, "generation_calibration"): uni_ids(np_, "generation_calibration"),
        }

        # DELETE plan (production only): universe - target, intersect handled live by prune
        for (ws, name), tset in targets.items():
            drop = sorted(uni_ids(ws, name) - tset)
            task = name.split("_")[0]
            plan_datasets.append({"domain": prefix, "task": task, "workspace": ws,
                                  "drop_ids": {name: drop}})
            tot_delete += len(set(drop) & live_ids(ws, name))

        # RESTORE lists: target - live (production + calibration)
        for (ws, name), tset in {**targets, **cal_targets}.items():
            need = sorted(tset - live_ids(ws, name))
            if need:
                (out / "restore" / f"{ws}__{name}.ids").write_text("\n".join(need) + "\n")
                tot_restore += len(need)

        # report row
        report.append(f"## {prefix}")
        report.append(f"- retrieval: {len(sel['ret_pure_kept'])} completable pure-prod panels "
                      f"+ {len(sel['ret_cal_panels'])} calibration chunks kept as-is (straddling panels' "
                      f"prod chunks dropped); submitted prod chunks kept={sel['kept_ret_done']} "
                      f"dropped={sel['dropped_ret_done']}")
        report.append(f"- grounding: keep {len(sel['gnd_keep'])} ("
                      f"{sel['gnd_completed']} completed + {len(sel['gnd_keep'])-sel['gnd_completed']} unfinished top-up)")
        report.append(f"- generation: keep {len(sel['gen_keep'])} ("
                      f"{sel['gen_completed']} completed + {len(sel['gen_keep'])-sel['gen_completed']} unfinished top-up)")
        report.append(f"- complete production triples: {sel['n_triples']}")
        report.append(f"- retrieval panels k-bucket kept={sel['kbucket_kept']} dropped={sel['kbucket_dropped']}")
        report.append("")

    (out / "plan.json").write_text(json.dumps(
        {"created_from": {"universe": str(universe), "live": str(live)},
         "target": target, "ret_cutoff": cutoff, "datasets": plan_datasets}, indent=2))
    (out / "target.json").write_text(json.dumps(target_json, indent=2))
    report.insert(2, f"**TOTAL restore={tot_restore}  delete(live)={tot_delete}**\n")
    (out / "report.md").write_text("\n".join(report))

    print(f"written: {out}")
    print(f"  plan.json  target.json  report.md  restore/*.ids")
    print(f"  TOTAL restore={tot_restore}  delete(live)={tot_delete}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--universe", type=Path, required=True)
    p.add_argument("--live", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--target", type=int, default=40)
    p.add_argument("--ret-cutoff", type=int, default=5)
    args = ap.parse_args()
    if args.cmd == "plan":
        cmd_plan(args.universe, args.live, args.out, args.target, args.ret_cutoff)


if __name__ == "__main__":
    main()
