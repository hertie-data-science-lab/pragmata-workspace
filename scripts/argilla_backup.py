#!/usr/bin/env python
"""Status-preserving, restore-capable backup of the live Argilla instance.

Two subcommands:

  dump   (default) -- snapshot EVERY dataset to disk. Read-only against the
         server. For each dataset writes:
           <ws>__<name>/settings.json      (rg.Settings, via to_json)
           <ws>__<name>/records_full.json  (records WITH response status)
         plus a top-level manifest.json. Unlike the SDK's to_disk, the record
         serialization keeps each response's `status` (submitted/draft/
         discarded) -- proven necessary, since to_disk drops it.

  verify -- prove the dump restores faithfully, INCLUDING submitted status.
         Recreates a representative subset inside a throwaway workspace
         (_backup_verify_<ts>) using Settings.from_json + the Dataset(workspace=)
         constructor (NOT from_disk -- from_disk silently ignores a Workspace
         object and falls back to the default workspace), logs the status-
         preserving records, and asserts restored record + submitted-response
         counts equal the manifest. Then tears the throwaway down (guarded:
         only deletes datasets whose workspace name starts with the throwaway
         prefix) and asserts the global dataset count returns to baseline.

Run ``dump`` first; it is the actual backup. ``verify`` is the safety proof.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import argilla as rg

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
BACKUP_ROOT = WORKSPACE_ROOT / "argilla_backup"
THROWAWAY_PREFIX = "_backup_verify_"


def _client() -> rg.Argilla:
    url = key = None
    for line in (WORKSPACE_ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            if k.strip() == "ARGILLA_API_URL":
                url = v.strip()
            elif k.strip() == "ARGILLA_API_KEY":
                key = v.strip()
    if not (url and key):
        sys.exit("missing ARGILLA_API_URL / ARGILLA_API_KEY in .env")
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
             "status": (r.status.value if hasattr(r.status, "value") else r.status)}
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


def _submitted(rec) -> int:
    return sum(1 for r in (rec.responses or [])
               if str(getattr(r, "status", "")).lower().endswith("submitted"))


def _counts(dataset) -> tuple[int, int]:
    """(n_records, n_submitted_responses) via a read-only iteration."""
    n_rec = n_sub = 0
    for rec in dataset.records(with_responses=True):
        n_rec += 1
        n_sub += _submitted(rec)
    return n_rec, n_sub


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


# --- verify (writes ONLY to a throwaway workspace) -----------------------------

def cmd_verify(backup_dir: str) -> None:
    root = Path(backup_dir)
    manifest = json.loads((root / "manifest.json").read_text())
    with_resp = [m for m in manifest["datasets"] if m["submitted_responses"] > 0]
    without = [m for m in manifest["datasets"] if m["submitted_responses"] == 0]
    subset = with_resp[:6] + without[:2]
    print(f"verifying {len(subset)} of {len(manifest['datasets'])} datasets (status-preserving)")

    client = _client()
    baseline = len(list(client.datasets))
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ws_name = f"{THROWAWAY_PREFIX}{ts}"
    throwaway = rg.Workspace(name=ws_name, client=client).create()
    print(f"throwaway workspace: {ws_name} (baseline dataset count {baseline})")

    ok = True
    try:
        for m in subset:
            p = Path(m["path"])
            settings = rg.Settings.from_json(p / "settings.json")
            # constructor honours the Workspace OBJECT (from_disk does not)
            ds = rg.Dataset(name=f"v_{m['key']}"[:200], workspace=throwaway,
                            settings=settings, client=client).create()
            assert ds.workspace.name == ws_name, \
                f"restored dataset landed in {ds.workspace.name!r}, not throwaway -- aborting"
            records = [deserialize_record(d) for d in json.loads((p / "records_full.json").read_text())]
            ds.records.log(records)

            n_rec, n_sub = _counts(ds)
            match = (n_rec == m["records"] and n_sub == m["submitted_responses"])
            ok &= match
            print(f"  [{'OK ' if match else 'MISMATCH'}] {m['key']:50s} "
                  f"records {n_rec}/{m['records']} submitted {n_sub}/{m['submitted_responses']}")
    finally:
        assert throwaway.name.startswith(THROWAWAY_PREFIX), "refusing to delete non-throwaway workspace"
        for ds in list(throwaway.datasets):
            assert ds.workspace.name == ws_name, "guard: dataset not in throwaway"
            ds.delete()
        throwaway.delete()
        final = len(list(client.datasets))
        print(f"torn down {ws_name}; dataset count {final} (baseline {baseline})")
        assert final == baseline, f"LEAK: dataset count {final} != baseline {baseline}"

    print("\nVERIFY PASSED -- records + submitted status round-trip faithfully"
          if ok else "\nVERIFY FAILED -- counts diverged")
    sys.exit(0 if ok else 1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("dump", help="status-preserving snapshot of every dataset (read-only)")
    v = sub.add_parser("verify", help="prove the dump restores (status included) in a throwaway workspace")
    v.add_argument("backup_dir", help="path to argilla_backup/<ts>/")
    args = ap.parse_args()

    if args.cmd in (None, "dump"):
        cmd_dump()
    elif args.cmd == "verify":
        cmd_verify(args.backup_dir)


if __name__ == "__main__":
    main()
