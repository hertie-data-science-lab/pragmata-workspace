#!/usr/bin/env python
"""Prune live Argilla down to the curated keep-lists (reproduction primitive).

For each `keep_lists/<workspace>__<dataset>.ids`, delete every live record in that
dataset whose id is NOT in the keep-list. This is the deterministic "reduce to the
exact curated set" step used by `make reproduce-curation`, after either:
  - importing the curated corpus (structure only), or
  - restoring the pre-prune backup (exact, incl. responses).

Deletes only; it does not add missing records (build the superset first, via import
or restore). Absolute keep-lists (not drop-lists) → independent of how the datasets
were built, so this converges to the exact end state from any superset. Read-only
unless --apply.

Usage:
  prune_to_keeplist.py --keep-lists <dir> [--workspace WS ...] [--apply]

Env: ARGILLA_API_URL, ARGILLA_API_KEY (point at the target instance).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import argilla as rg


def _client() -> rg.Argilla:
    return rg.Argilla(api_url=os.environ["ARGILLA_API_URL"], api_key=os.environ["ARGILLA_API_KEY"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keep-lists", type=Path, required=True, help="dir of <ws>__<dataset>.ids files")
    ap.add_argument("--workspace", action="append", default=None, help="limit to these workspace(s)")
    ap.add_argument("--apply", action="store_true", help="delete; default preview only")
    args = ap.parse_args()

    client = _client()
    grand_del = grand_keep = 0
    for f in sorted(args.keep_lists.glob("*.ids")):
        ws_name, ds_name = f.stem.split("__", 1)
        if args.workspace and ws_name not in args.workspace:
            continue
        keep = {ln.strip() for ln in f.read_text().splitlines() if ln.strip()}
        ds = client.datasets(name=ds_name, workspace=ws_name)
        if ds is None:
            print(f"  {ws_name}/{ds_name}: MISSING dataset (skip)")
            continue
        live = {str(r.id): r for r in ds.records()}
        to_delete = [r for rid, r in live.items() if rid not in keep]
        missing = keep - set(live)
        grand_del += len(to_delete)
        grand_keep += len(keep & set(live))
        note = f"  {ws_name}/{ds_name}: keep {len(keep & set(live))}/{len(keep)}, delete {len(to_delete)}/{len(live)}"
        if missing:
            note += f"  WARN {len(missing)} keep-ids absent from live (import/restore first?)"
        print(note)
        if args.apply and to_delete:
            ds.records.delete(to_delete, batch_size=64)
            print(f"    deleted {len(to_delete)}")

    print(f"\nTOTAL: keep {grand_keep}, delete {grand_del} (filter: {args.workspace or 'ALL'})")
    if not args.apply:
        print("(preview only; pass --apply to mutate)")


if __name__ == "__main__":
    main()
