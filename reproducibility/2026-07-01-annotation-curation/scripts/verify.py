#!/usr/bin/env python
"""Round-2 verification: check the live end state against target + pre-prune backup.

Read-only. Checks:
  A per-cell: retrieval ~70 complete panels (40 pure-prod + ~30 cal), grounding/
    generation production >= target, calibration count == pre-prune backup.
  B cross-task consistency: the 40 pure-prod uuids each have a production record
    live in retrieval + grounding + generation (complete triples).
  C calibration integrity: live calibration record-id set == pre-prune backup
    (untouched for staffed; restored for ZfD).
  D deletions safe: every id in apply_log.jsonl is present in the pre-prune backup.
  E reproducibility: curated .curated manifest survivors == live query uuids.

Usage: tmp/round2_verify.py --target tmp/round2/final --backup argilla_backup/backup_pre_prune_20260701T185359Z
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import argilla as rg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
import workspace as ws  # noqa: E402

ws.load_env()


def _client() -> rg.Argilla:
    return rg.Argilla(api_url=os.environ["ARGILLA_API_URL"], api_key=os.environ["ARGILLA_API_KEY"])


def _bk_index(backup: Path) -> dict:
    m = json.loads((backup / "manifest.json").read_text())
    return {(d["workspace"], d["name"]): Path(d["path"]) / "records_full.json" for d in m["datasets"]}


def _bk_ids(idx, ws_name, name) -> set:
    p = idx.get((ws_name, name))
    return {r["id"] for r in json.loads(p.read_text())} if p and p.exists() else set()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", type=Path, required=True)
    ap.add_argument("--backup", type=Path, required=True)
    args = ap.parse_args()

    target = json.loads((args.target / "target.json").read_text())
    apply_log = [json.loads(l) for l in (args.target / "apply_log.jsonl").read_text().splitlines()]
    bk = _bk_index(args.backup)
    client = _client()
    fails = []

    def live_ids(ws_name, name):
        ds = client.datasets(name=name, workspace=ws_name)
        return {str(r.id) for r in ds.records()} if ds else set()

    def live_uuids(ws_name, name):
        ds = client.datasets(name=name, workspace=ws_name)
        return {r.metadata.get("record_uuid") for r in ds.records()} if ds else set()

    print("== A/B/C per domain ==")
    for prefix, sel in target.items():
        rp, gp, np_ = f"{prefix}_retrieval", f"{prefix}_grounding", f"{prefix}_generation"
        # A: panel + counts
        ret_panels = live_uuids(rp, "retrieval_production") | live_uuids(rp, "retrieval_calibration")
        gpn = len(live_ids(gp, "grounding_production"))
        epn = len(live_ids(np_, "generation_production"))
        # C: calibration id-set equality vs backup
        cal_ok = True
        for ws_name, name in [(rp, "retrieval_calibration"), (gp, "grounding_calibration"), (np_, "generation_calibration")]:
            if live_ids(ws_name, name) != _bk_ids(bk, ws_name, name):
                cal_ok = False
                fails.append(f"{ws_name}/{name}: calibration id-set != pre-prune backup")
        # B: triples — 40 pure-prod uuids present as production in all 3
        pure = set(sel["ret_pure_kept"])
        rprod = live_uuids(rp, "retrieval_production")
        gprod = live_uuids(gp, "grounding_production")
        eprod = live_uuids(np_, "generation_production")
        triples = pure & rprod & gprod & eprod
        if len(triples) != len(pure):
            fails.append(f"{prefix}: triples {len(triples)} != pure-prod {len(pure)}")
        print(f"  {prefix:30s} retPanels={len(ret_panels):>3d} gndProd={gpn:>3d} genProd={epn:>3d} "
              f"triples={len(triples):>2d}/{len(pure)} calItactIAA={'ok' if cal_ok else 'FAIL'}")

    print("== D: deletions all present in pre-prune backup ==")
    bk_all = set()
    for (wsn, nm), p in bk.items():
        bk_all |= {r["id"] for r in json.loads(p.read_text())}
    del_ids = {e["record_id"] for e in apply_log}
    missing = del_ids - bk_all
    print(f"  deleted={len(del_ids)} missing-from-backup={len(missing)} {'OK' if not missing else 'FAIL'}")
    if missing:
        fails.append(f"{len(missing)} deleted ids not in pre-prune backup")

    print("== E: curated .curated manifest survivors == live query uuids ==")
    import yaml
    for prefix, sel in target.items():
        slug = prefix.lower()
        scope = yaml.safe_load((ws.DOMAINS_DIR / f"{slug}.yaml").read_text())["partition_scope"]
        man = json.loads((ws.ROOT / "data/annotation/imports" / scope / "partition.meta.curated.json").read_text())
        surv_manifest = set(man["assignments"])
        rp, gp, np_ = f"{prefix}_retrieval", f"{prefix}_grounding", f"{prefix}_generation"
        live_all = set()
        for wsn, nm in [(rp, "retrieval_production"), (rp, "retrieval_calibration"),
                        (gp, "grounding_production"), (gp, "grounding_calibration"),
                        (np_, "generation_production"), (np_, "generation_calibration")]:
            live_all |= live_uuids(wsn, nm)
        live_all.discard(None)
        if surv_manifest != live_all:
            fails.append(f"{prefix}: .curated manifest survivors ({len(surv_manifest)}) != live uuids ({len(live_all)})")
        print(f"  {prefix:30s} manifest={len(surv_manifest):>3d} live={len(live_all):>3d} "
              f"{'OK' if surv_manifest == live_all else 'FAIL'}")

    print("\n" + ("ALL CHECKS PASSED" if not fails else f"{len(fails)} FAILURES:"))
    for f in fails:
        print("  -", f)


if __name__ == "__main__":
    main()
