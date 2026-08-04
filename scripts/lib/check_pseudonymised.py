#!/usr/bin/env python3
"""Refuse an export tree that still carries real annotator identities.

    check_pseudonymised.py <tree>      exit 0 = clean, 1 = names present

The no-real-names invariant, at both boundaries that need it: `transfer-push` before a
tree leaves the box, and `annotation-freeze` before an immutable copy of one exists.
pragmata writes the Argilla *username* into `annotator_id` and the usernames on this
instance are `firstname.lastname`, so a tree that skipped
`scripts/annotation/pseudonymize_export.py` holds real names. Every export runs it
(export.sh, fatal on failure), but a killed export or a hand-placed tree never went
through it - which is exactly what these two boundaries see.

Any CSV carrying an `annotator_id` column and any `iaa/report.json` pairwise key must
hold UUIDs only; trees without those surfaces (predictions, checkpoints) pass untouched.
Offending values are never echoed - only the files holding them.

Stdlib only, and run with `python3` rather than the venv: both callers are shell scripts
whose other work needs no venv.
"""

from __future__ import annotations

import csv
import json
import sys
import uuid
from pathlib import Path


def _ok(value) -> bool:
    """Whether one identity is pseudonymous. Empty counts as clean: no identity, no leak."""
    if not value:
        return True
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


def offenders(root: Path) -> list[Path]:
    """Files under ``root`` holding a non-UUID annotator identity, sorted."""
    bad = []
    for p in sorted(root.rglob("*.csv")):
        with p.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames or "annotator_id" not in reader.fieldnames:
                continue
            if not all(_ok(row.get("annotator_id")) for row in reader):
                bad.append(p)
    for p in sorted(root.rglob("iaa/report.json")):
        report = json.loads(p.read_text())
        pairs = [
            pair
            for block in report.get("tasks") or []
            for pair in block.get("pairwise_kappa") or []
        ]
        if not all(
            _ok(pair.get(k)) for pair in pairs for k in ("annotator_a", "annotator_b")
        ):
            bad.append(p)
    return bad


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} <tree>", file=sys.stderr)
        return 2
    bad = offenders(Path(sys.argv[1]))
    if bad:
        print(
            "non-pseudonymised annotator identities in: "
            + ", ".join(str(p) for p in bad),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
