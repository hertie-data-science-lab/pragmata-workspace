#!/usr/bin/env python
"""Status-preserving backup + restore of the live Argilla instance.

dump   (default) -- snapshot EVERY dataset to disk. Read-only against the
       server. For each dataset writes:
         <ws>__<name>/settings.json      (rg.Settings, via to_json)
         <ws>__<name>/records_full.json  (records WITH response status)
       plus a top-level manifest.json. Unlike the SDK's to_disk, the record
       serialization keeps each response's `status` (submitted/draft/
       discarded) -- so annotations restore faithfully (to_disk drops it).

restore <dir> [--workspace WS ...] [--dataset NAME ...] [--record-id ID ...]
       [--only {metadata,suggestions,responses} ...] [--apply] -- restore the
       full snapshot (fields, metadata, suggestions, responses) back into
       Argilla, creating missing datasets/workspaces and writing onto existing
       ones alike. Narrow scope with --workspace/--dataset/--record-id
       (repeatable, AND'd together; default: everything in the manifest); pass
       --only to restore just some attributes (fields are always included).
       Always prints a preview of what would change first; nothing is written
       unless --apply is given.

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
            {
                "question_name": s.question_name,
                "value": s.value,
                "score": s.score,
                "agent": s.agent,
            }
            for s in (rec.suggestions or [])
        ],
        "responses": [
            {
                "question_name": r.question_name,
                "value": r.value,
                "user_id": str(r.user_id) if r.user_id else None,
                "status": _status(r),
            }
            for r in (rec.responses or [])
        ],
    }


_RESTORABLE_ATTRS = (
    "metadata",
    "suggestions",
    "responses",
)  # fields are always restored


def deserialize_record(
    d: dict, only: set[str] | None = None, live: dict | None = None
) -> rg.Record:
    """Build an rg.Record from a serialize_record dict.

    ``only`` restricts which of {metadata, suggestions, responses} to include
    (fields are always included - Argilla requires them present for any
    write). ``live`` is the record's current live state (serialize_record
    shape), if it exists - used to safely no-op an excluded attribute.

    Excluded attributes are NOT handled uniformly, because Argilla does not
    treat "omitted" the same way for every attribute - verified empirically
    against the live server: omitting ``responses`` (None) genuinely leaves
    existing responses untouched, but omitting ``metadata`` (None) silently
    WIPES it to {} rather than preserving it (and suggestions were not ruled
    out either, given no real suggestion data exists to test against). So:
    - responses: omitted (None) when excluded - proven safe.
    - metadata/suggestions: resend the live record's *current* value when
      excluded (an identity resend - proven to be a safe no-op) rather than
      omit, since omission cannot be trusted to preserve them. If there's no
      live record (a brand-new record), there is nothing to preserve, so an
      excluded attribute is simply empty.
    """
    include = _RESTORABLE_ATTRS if only is None else only

    def or_live(attr: str, empty):
        return live[attr] if live is not None else empty

    metadata = d["metadata"] if "metadata" in include else or_live("metadata", {})
    suggestions_src = (
        d["suggestions"] if "suggestions" in include else or_live("suggestions", [])
    )
    responses_src = d["responses"] if "responses" in include else None

    return rg.Record(
        id=d["id"],
        fields=d["fields"],
        metadata=metadata,
        suggestions=[
            rg.Suggestion(
                question_name=s["question_name"],
                value=s["value"],
                score=s.get("score"),
                agent=s.get("agent"),
            )
            for s in suggestions_src
        ],
        responses=(
            [
                rg.Response(
                    question_name=r["question_name"],
                    value=r["value"],
                    user_id=UUID(r["user_id"]) if r["user_id"] else None,
                    status=r["status"],
                )
                for r in responses_src
            ]
            if responses_src is not None
            else None
        ),
    )


def _status(r) -> str | None:
    return r.status.value if hasattr(r.status, "value") else r.status


def _submitted(rec) -> int:
    return sum(1 for r in (rec.responses or []) if _status(r) == "submitted")


def _diff_record(live: dict | None, backup: dict) -> dict:
    """Pure comparison of two serialize_record-shaped dicts.

    ``live=None`` means the record doesn't exist yet (pure create - nothing to
    diff). Responses are compared per (user_id, question_name): a user present
    in the backup but not live is a safe add (never listed as a diff, since
    nothing live is touched); a user present in both with a differing value
    for any question is the one real risk (silently overwriting a live value,
    including the case where the live user answered a question absent from
    their backup-time response set - the wholesale per-user upsert would drop
    it, so this must be visible too).
    """
    if live is None:
        return {"new": True, "any_change": False, "response_diffs": []}

    def by_user(responses: list[dict]) -> dict[str | None, dict[str, tuple]]:
        out: dict[str | None, dict[str, tuple]] = {}
        for r in responses:
            out.setdefault(r["user_id"], {})[r["question_name"]] = (
                r["value"],
                r["status"],
            )
        return out

    live_by_user = by_user(live["responses"])
    backup_by_user = by_user(backup["responses"])
    response_diffs = []
    for uid, backup_qs in backup_by_user.items():
        live_qs = live_by_user.get(uid)
        if live_qs is None:
            continue  # brand-new user for this record -- safe add, not an overwrite
        for q in set(live_qs) | set(backup_qs):
            lv, bv = live_qs.get(q), backup_qs.get(q)
            if lv != bv:
                response_diffs.append(
                    {"user_id": uid, "question_name": q, "live": lv, "backup": bv}
                )

    fields_changed = live["fields"] != backup["fields"]
    metadata_changed = live["metadata"] != backup["metadata"]
    suggestions_changed = live["suggestions"] != backup["suggestions"]
    return {
        "new": False,
        "response_diffs": response_diffs,
        "fields_changed": fields_changed,
        "metadata_changed": metadata_changed,
        "suggestions_changed": suggestions_changed,
        "any_change": fields_changed
        or metadata_changed
        or suggestions_changed
        or bool(response_diffs),
    }


def _parse_snapshot_ts(created_utc: str) -> datetime | None:
    try:
        return datetime.strptime(created_utc, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except (ValueError, TypeError):
        return None


def _as_utc(dt: datetime | None) -> datetime | None:
    """Argilla's Record.updated_at comes back timezone-naive; treat it as UTC
    (the server's own convention) so it's comparable to our aware snapshot_ts."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


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

        ds.settings.to_json(target / "settings.json")  # structure (read)

        n_rec = n_sub = 0
        records_out = []
        for rec in ds.records(with_responses=True, with_suggestions=True):  # read
            records_out.append(serialize_record(rec))
            n_rec += 1
            n_sub += _submitted(rec)
        (target / "records_full.json").write_text(
            json.dumps(records_out, ensure_ascii=False)
        )

        manifest.append(
            {
                "key": key,
                "workspace": ws,
                "name": ds.name,
                "records": n_rec,
                "submitted_responses": n_sub,
                "path": str(target),
            }
        )
        print(
            f"[{i:2d}/{len(datasets)}] {key:55s} records={n_rec:5d} submitted={n_sub:5d}"
        )

    (root / "manifest.json").write_text(
        json.dumps(
            {"created_utc": ts, "api_datasets": len(datasets), "datasets": manifest},
            indent=2,
        )
    )
    print(
        f"\nbackup complete: {len(manifest)} datasets, "
        f"{sum(m['records'] for m in manifest)} records, "
        f"{sum(m['submitted_responses'] for m in manifest)} submitted responses"
    )
    print(f"manifest: {root / 'manifest.json'}")


# --- restore -------------------------------------------------------------------


def cmd_restore(
    backup_dir: str,
    workspaces: list[str] | None,
    datasets: list[str] | None,
    record_ids: list[str] | None,
    only: list[str] | None,
    apply: bool,
) -> None:
    root = Path(backup_dir)
    manifest = json.loads((root / "manifest.json").read_text())
    snapshot_ts = _parse_snapshot_ts(manifest["created_utc"])
    client = _client()

    entries = manifest["datasets"]
    if workspaces:
        entries = [m for m in entries if m["workspace"] in workspaces]
    if datasets:
        entries = [m for m in entries if m["name"] in datasets]
    if not entries:
        sys.exit(
            "no datasets in the manifest match the given --workspace/--dataset filters"
        )

    record_id_set = set(record_ids) if record_ids else None
    only_set = set(only) if only else None

    # Load each matched dataset's backup records (filtered by --record-id) up front,
    # so a typo'd --record-id fails loudly instead of silently restoring nothing/everything.
    backup_by_entry: dict[int, list[dict]] = {}
    found_ids: set[str] = set()
    for i, m in enumerate(entries):
        records = json.loads((Path(m["path"]) / "records_full.json").read_text())
        if record_id_set is not None:
            records = [d for d in records if d["id"] in record_id_set]
        backup_by_entry[i] = records
        found_ids.update(d["id"] for d in records)
    if record_id_set is not None and (missing := record_id_set - found_ids):
        sys.exit(
            f"record id(s) not found in the selected backup scope: {sorted(missing)}"
        )

    print(
        f"restore preview: {root}  (scope: {len({m['workspace'] for m in entries})} workspace(s), "
        f"{len(entries)} dataset(s))"
    )

    # (workspace, dataset name) -> (manifest entry, [(backup record dict, live record
    # dict or None), ...]) for records that actually need writing (skips byte-identical
    # ones - keeps this minimal-touch and keeps a later re-run's touched-since-snapshot
    # signal honest, since re-logging a no-op record would otherwise bump its updated_at).
    by_dataset: dict[tuple[str, str], tuple[dict, list[tuple[dict, dict | None]]]] = {}
    ds_by_key: dict[
        tuple[str, str], object | None
    ] = {}  # resolved once, reused at write time
    n_new = n_identical = n_updated = n_overwrite_records = n_touched_since_snapshot = 0
    n_metadata_changed = n_fields_changed = n_suggestions_changed = 0
    overwrite_lines: list[str] = []

    for i, m in enumerate(entries):
        backup_records = backup_by_entry[i]
        if not backup_records:
            continue
        ws_name = m["workspace"]
        key = (ws_name, m["name"])
        # workspace() being read-only here matters: a nonexistent workspace makes
        # client.datasets(workspace=...) raise (not return None), and dry-run must
        # never create anything, so check workspace existence first.
        existing_ds = (
            client.datasets(name=m["name"], workspace=ws_name)
            if client.workspaces(ws_name) is not None
            else None
        )
        ds_by_key[key] = existing_ds
        live_by_id: dict[str, tuple[dict, object]] = {}
        if existing_ds is not None:
            wanted_ids = {d["id"] for d in backup_records}
            for rec in existing_ds.records(with_responses=True, with_suggestions=True):
                if rec.id in wanted_ids:
                    live_by_id[rec.id] = (serialize_record(rec), rec)

        for d in backup_records:
            live_entry = live_by_id.get(d["id"])
            live_dict, live_rec = live_entry if live_entry else (None, None)
            diff = _diff_record(live_dict, d)
            if diff["new"]:
                n_new += 1
            elif diff["any_change"]:
                n_updated += 1
                if diff["response_diffs"]:
                    n_overwrite_records += 1
                    for rd in diff["response_diffs"]:
                        overwrite_lines.append(
                            f"    record {d['id']}  user {rd['user_id']}  question={rd['question_name']}  "
                            f"live={rd['live']!r} -> backup={rd['backup']!r}"
                        )
                n_metadata_changed += diff["metadata_changed"]
                n_fields_changed += diff["fields_changed"]
                n_suggestions_changed += diff["suggestions_changed"]
            else:
                n_identical += 1
            live_updated_at = (
                _as_utc(live_rec.updated_at) if live_rec is not None else None
            )
            if (
                live_updated_at is not None
                and snapshot_ts
                and live_updated_at > snapshot_ts
            ):
                n_touched_since_snapshot += 1
            if diff["new"] or diff["any_change"]:
                by_dataset.setdefault(key, (m, []))[1].append((d, live_dict))

    print(f"  {n_new} record(s) will be newly created")
    print(f"  {n_identical} record(s) already exist and are identical (no-op)")
    print(f"  {n_updated} record(s) already exist and will be updated")
    if n_overwrite_records:
        print(
            f"    -> {n_overwrite_records} of these overwrite a response with a different live value:"
        )
        for line in overwrite_lines[:20]:
            print(line)
        if len(overwrite_lines) > 20:
            print(f"    ... (+{len(overwrite_lines) - 20} more)")
    for count, label in (
        (n_metadata_changed, "metadata differences (replaced wholesale)"),
        (n_fields_changed, "field differences"),
        (n_suggestions_changed, "suggestion differences"),
    ):
        if count:
            print(f"    -> {count} of these have {label}")
    if n_touched_since_snapshot:
        print(
            f"  {n_touched_since_snapshot} record(s) touched live after this backup's snapshot time "
            f"({manifest['created_utc']}) - review closely"
        )
    print()

    if not apply:
        print("dry-run: no changes written. Re-run with --apply to write the above.")
        return

    ws_cache: dict[str, "rg.Workspace"] = {}

    def workspace(name: str) -> "rg.Workspace":  # resolve/create once per name
        if name not in ws_cache:
            ws_cache[name] = (
                client.workspaces(name)
                or rg.Workspace(name=name, client=client).create()
            )
        return ws_cache[name]

    n_written = 0
    for (ws_name, name), (m, backup_records) in by_dataset.items():
        ds = ds_by_key.get((ws_name, name))
        if ds is None:
            wspace = workspace(
                ws_name
            )  # resolve/create first — datasets() needs the ws to exist
            settings = rg.Settings.from_json(Path(m["path"]) / "settings.json")
            ds = rg.Dataset(
                name=name, workspace=wspace, settings=settings, client=client
            ).create()
        records = [
            deserialize_record(d, only_set, live_dict)
            for d, live_dict in backup_records
        ]
        ds.records.log(records)
        n_written += len(records)
        print(f"  [restored] {ws_name}/{name}  ({len(records)} records)")

    print(
        f"\nrestore complete: {n_written} record(s) written across {len(by_dataset)} dataset(s)"
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser(
        "dump", help="status-preserving snapshot of every dataset (read-only)"
    )
    r = sub.add_parser(
        "restore",
        help="restore a snapshot (full or scoped); previews first, --apply to write",
    )
    r.add_argument("backup_dir", help="path to argilla_backup/<ts>/")
    r.add_argument(
        "--workspace",
        action="append",
        dest="workspaces",
        default=None,
        help="restore only dataset(s) in this workspace (repeatable). default: every workspace in the manifest",
    )
    r.add_argument(
        "--dataset",
        action="append",
        dest="datasets",
        default=None,
        help="restore only dataset(s) with this name, e.g. retrieval_calibration (repeatable). "
        "default: every dataset",
    )
    r.add_argument(
        "--record-id",
        action="append",
        dest="record_ids",
        default=None,
        help="restore only this record id (repeatable). default: every record in scope",
    )
    r.add_argument(
        "--record-id-file",
        action="append",
        dest="record_id_files",
        default=None,
        help="file of newline-delimited record ids to restore (repeatable); unioned with --record-id",
    )
    r.add_argument(
        "--only",
        action="append",
        dest="only",
        default=None,
        choices=list(_RESTORABLE_ATTRS),
        help="restore only these attributes (repeatable; fields are always restored). default: full snapshot",
    )
    r.add_argument(
        "--apply",
        action="store_true",
        help="write the changes; default is a dry-run preview only",
    )
    args = ap.parse_args()

    if args.cmd in (None, "dump"):
        cmd_dump()
    elif args.cmd == "restore":
        record_ids = list(args.record_ids) if args.record_ids else []
        for f in args.record_id_files or []:
            record_ids.extend(
                line.strip() for line in Path(f).read_text().splitlines() if line.strip()
            )
        cmd_restore(
            args.backup_dir,
            args.workspaces,
            args.datasets,
            record_ids or None,
            args.only,
            args.apply,
        )


if __name__ == "__main__":
    main()
