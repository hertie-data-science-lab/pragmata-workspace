#!/usr/bin/env python3
"""Rewrite annotator identities in an export tree to stable UUIDs.

Why the export must not carry names, and what this guarantees: `docs/data-transport.md`
(Data sensitivity), and `scripts/lib/check_pseudonymised.py`, which enforces it at both
boundaries.

`pragmata annotation export` writes the Argilla *username* into `annotator_id`, and the
usernames on this instance are `firstname.lastname`. This runs immediately after every
export (see `export.sh`) and replaces each with that user's Argilla user id, on both
surfaces that carry identities: `annotator_id` in `retrieval.csv` / `grounding.csv` /
`generation.csv`, and `annotator_a` / `annotator_b` under `pairwise_kappa` in
`iaa/report.json`. Argilla user ids never change, so the mapping is stable across exports
and cross-snapshot comparisons still work; it is derived at runtime, never written down,
and is not reversible from the export alone.

Forward-only: already-frozen trees keep whatever they were frozen with. Idempotent (a
value that is already a UUID is left alone) and strict — a username with no matching
Argilla user aborts the run, because passing it through would leave a real name inside a
tree labelled pseudonymous.

Usage:
  scripts/annotation/pseudonymize_export.py             # every domain in the export tree
  scripts/annotation/pseudonymize_export.py DOMAIN ...  # named domains only
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import workspace as ws

ws.load_env()  # configs/settings.conf + .env (pragmata itself is the pinned install)

TASK_FILES = ("retrieval.csv", "grounding.csv", "generation.csv")
IAA_REPORT = Path("iaa") / "report.json"


def is_uuid(value: str) -> bool:
    """Whether a value has already been pseudonymised."""
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def pseudonym(value: str, mapping: dict[str, str], where: Path) -> str:
    """One identity rewritten, or exit if it cannot be."""
    if not value or is_uuid(value):
        return value
    resolved = mapping.get(value)
    if resolved is None:
        # Deliberately does not echo the value: the whole point is to keep names out of
        # anything that leaves this box, including its logs.
        raise SystemExit(
            f"{where}: an annotator identity has no matching Argilla user, so it cannot "
            "be pseudonymised.\nRefusing to leave a real name in the export. Check the "
            "roster (configs/annotation/users.json) against the instance's user list."
        )
    return resolved


def rewrite_csv(path: Path, mapping: dict[str, str]) -> int:
    """Rewrite annotator_id in one task CSV; returns the rows changed."""
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or "annotator_id" not in reader.fieldnames:
            return 0
        columns = list(reader.fieldnames)
        rows = list(reader)
    changed = 0
    for row in rows:
        before = row["annotator_id"]
        row["annotator_id"] = pseudonym(before, mapping, path)
        changed += row["annotator_id"] != before
    # Written through a temp file in the same dir and moved into place, so an interrupted
    # run cannot leave a half-rewritten CSV that still holds some real names.
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)
    return changed


def rewrite_iaa(path: Path, mapping: dict[str, str]) -> int:
    """Rewrite the pairwise-kappa annotator keys in one IAA report."""
    report = json.loads(path.read_text())
    changed = 0
    for block in report.get("tasks") or []:
        for pair in block.get("pairwise_kappa") or []:
            for key in ("annotator_a", "annotator_b"):
                before = pair.get(key, "")
                pair[key] = pseudonym(before, mapping, path)
                changed += pair[key] != before
    # Same temp-file-then-move as rewrite_csv, for the same reason: an interrupted run
    # must not leave a half-written report that still holds some real names.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "domains", nargs="*", help="domain stems; default every exported one"
    )
    args = ap.parse_args()

    root = ws.EXPORTS_DIR
    names = args.domains or sorted(p.name for p in root.iterdir() if p.is_dir())
    if not names:
        raise SystemExit(f"no exported domains under {root}")

    mapping = ws.username_to_user_id()
    total = 0
    for name in names:
        directory = root / name
        if not directory.is_dir():
            raise SystemExit(f"no such export dir: {directory}")
        changed = sum(
            rewrite_csv(directory / f, mapping)
            for f in TASK_FILES
            if (directory / f).exists()
        )
        report = directory / IAA_REPORT
        if report.exists():
            changed += rewrite_iaa(report, mapping)
        total += changed
        print(f"  {name}: {changed} identities pseudonymised", file=sys.stderr)
    print(
        f"pseudonymised {total} identities across {len(names)} domain(s)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
