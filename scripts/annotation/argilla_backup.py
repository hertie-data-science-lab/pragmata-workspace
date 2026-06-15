#!/usr/bin/env python
"""Status-preserving backup + restore of the live Argilla instance.

  dump   (default) -- snapshot EVERY dataset to disk. Read-only against the
         server. For each dataset writes:
           <ws>__<name>/settings.json      (rg.Settings, via to_json)
           <ws>__<name>/records_full.json  (records WITH response status)
         plus a top-level manifest.json. Unlike the SDK's to_disk, the record
         serialization keeps each response's `status` (submitted/draft/
         discarded) -- so annotations restore faithfully (to_disk drops it).

  restore <dir> [--workspace WS] -- recreate the datasets from a dump back into
         Argilla, status-preserving. By default each dataset goes back to its
         original workspace/name (from the manifest); pass --workspace to put
         them all in one target workspace instead (e.g. to inspect a backup
         without touching the originals). Existing datasets are skipped, never
         overwritten. Workspaces are created if missing.

         Uses Settings.from_json + the Dataset(workspace=) constructor, NOT
         from_disk -- from_disk silently ignores a Workspace object and falls
         back to the default workspace.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import argilla as rg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import workspace as ws  # noqa: E402

ws.load_env()  # configs/settings.conf + .env; existing env wins

BACKUP_ROOT = ws.ROOT / "argilla_backup"


def _client() -> rg.Argilla:
    url = os.environ.get("ARGILLA_API_URL")
    key = os.environ.get("ARGILLA_API_KEY")
    if not (url and key):
        sys.exit("missing ARGILLA_API_URL / ARGILLA_API_KEY (set in .env)")
    print(f"connecting to {url}")
    return rg.Argilla(api_url=url, api_key=key)


# --- status-preserving (de)serialization --------------------------------------

def serialize_record(rec) -> dict:
    return {
        "id": rec.id,
        "fields": dict(rec.fields),
        "metadata": dict(rec.metadata),
        "suggestions": [
            {"question_name": s.question_name, "value": s.value,
             "score": s.score, "agent": s.agent}
            for s in (rec.suggestions or [])
        ],
        "responses": [
            {"question_name": r.question_name, "value": r.value,
             "user_id": str(r.user_id) if r.user_id else None,
             "status": _status(r)}
            for r in (rec.responses or [])
        ],
    }


def deserialize_record(d: dict) -> rg.Record:
    return rg.Record(
        id=d["id"],
        fields=d["fields"],
        metadata=d["metadata"],
        suggestions=[
            rg.Suggestion(question_name=s["question_name"], value=s["value"],
                          score=s.get("score"), agent=s.get("agent"))
            for s in d["suggestions"]
        ],
        responses=[
            rg.Response(question_name=r["question_name"], value=r["value"],
                        user_id=UUID(r["user_id"]) if r["user_id"] else None,
                        status=r["status"])
            for r in d["responses"]
        ],
    )


def _status(r) -> str | None:
    return r.status.value if hasattr(r.status, "value") else r.status


def _submitted(rec) -> int:
    return sum(1 for r in (rec.responses or []) if _status(r) == "submitted")


# --- dump (read-only) ----------------------------------------------------------

def cmd_dump() -> None:
    client = _client()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = BACKUP_ROOT / ts
    root.mkdir(parents=True, exist_ok=False)
    print(f"backup root: {root}")

    datasets = list(client.datasets)
    print(f"{len(datasets)} datasets to back up\n")

    manifest: list[dict] = []
    for i, ds in enumerate(datasets, 1):
        ws = ds.workspace.name
        key = f"{ws}__{ds.name}"
        target = root / key
        target.mkdir(parents=True, exist_ok=False)

        ds.settings.to_json(target / "settings.json")          # structure (read)

        n_rec = n_sub = 0
        records_out = []
        for rec in ds.records(with_responses=True, with_suggestions=True):  # read
            records_out.append(serialize_record(rec))
            n_rec += 1
            n_sub += _submitted(rec)
        (target / "records_full.json").write_text(json.dumps(records_out, ensure_ascii=False))

        manifest.append({"key": key, "workspace": ws, "name": ds.name,
                         "records": n_rec, "submitted_responses": n_sub, "path": str(target)})
        print(f"[{i:2d}/{len(datasets)}] {key:55s} records={n_rec:5d} submitted={n_sub:5d}")

    (root / "manifest.json").write_text(json.dumps(
        {"created_utc": ts, "api_datasets": len(datasets), "datasets": manifest}, indent=2))
    print(f"\nbackup complete: {len(manifest)} datasets, "
          f"{sum(m['records'] for m in manifest)} records, "
          f"{sum(m['submitted_responses'] for m in manifest)} submitted responses")
    print(f"manifest: {root / 'manifest.json'}")


# --- restore -------------------------------------------------------------------

def cmd_restore(backup_dir: str, target_workspace: str | None) -> None:
    root = Path(backup_dir)
    manifest = json.loads((root / "manifest.json").read_text())
    client = _client()
    dsets = manifest["datasets"]
    where = f"workspace {target_workspace!r}" if target_workspace else "their original workspaces"
    print(f"restoring {len(dsets)} datasets from {root} into {where}")

    ws_cache: dict[str, "rg.Workspace"] = {}

    def workspace(name: str) -> "rg.Workspace":  # resolve/create once per name
        if name not in ws_cache:
            ws_cache[name] = client.workspaces(name) or rg.Workspace(name=name, client=client).create()
        return ws_cache[name]

    restored = skipped = 0
    for m in dsets:
        ws_name = target_workspace or m["workspace"]
        wspace = workspace(ws_name)  # resolve/create first — datasets() needs the ws to exist
        if client.datasets(name=m["name"], workspace=ws_name) is not None:
            print(f"  [skip] {ws_name}/{m['name']} already exists (not overwriting)")
            skipped += 1
            continue
        settings = rg.Settings.from_json(Path(m["path"]) / "settings.json")
        ds = rg.Dataset(name=m["name"], workspace=wspace, settings=settings, client=client).create()
        assert ds.workspace.name == ws_name, f"landed in {ds.workspace.name!r}, not {ws_name!r}"
        records = [deserialize_record(d) for d in json.loads((Path(m["path"]) / "records_full.json").read_text())]
        ds.records.log(records)
        print(f"  [restored] {ws_name}/{m['name']}  ({len(records)} records, "
              f"{m['submitted_responses']} submitted)")
        restored += 1
    print(f"\nrestore complete: {restored} restored, {skipped} skipped")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("dump", help="status-preserving snapshot of every dataset (read-only)")
    r = sub.add_parser("restore", help="recreate datasets from a dump (status-preserving)")
    r.add_argument("backup_dir", help="path to argilla_backup/<ts>/")
    r.add_argument("--workspace", default=None,
                   help="restore all into this workspace (default: each dataset's original)")
    args = ap.parse_args()

    if args.cmd in (None, "dump"):
        cmd_dump()
    elif args.cmd == "restore":
        cmd_restore(args.backup_dir, args.workspace)


if __name__ == "__main__":
    main()
