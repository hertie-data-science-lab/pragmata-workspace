#!/usr/bin/env python3
"""Refuse an export tree that still carries real annotator identities.

    check_pseudonymised.py <tree>      exit 0 = clean, 1 = names present,
                                            2 = could not check, 3 = checker crashed

Only exit 1 means "this tree carries names". Anything else non-zero means the tree was
NOT cleared - a callable gate has to be able to tell those apart, because "unchecked" is
not "clean" and it is not "dirty" either.

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


class SchemaError(Exception):
    """A surface this checker reads is not the shape it knows how to read.

    Loud rather than absorbed: a renamed IAA key would otherwise make every pair
    vacuously clean, and the gate would bless a tree it never looked inside.
    """


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
        if "tasks" not in report:
            raise SchemaError(f"{p} has no `tasks` key")
        pairs = []
        for block in report["tasks"]:
            if "pairwise_kappa" not in block:
                raise SchemaError(
                    f"{p} has a `tasks` entry with no `pairwise_kappa` key"
                )
            pairs.extend(block["pairwise_kappa"])
        if not all(
            _ok(pair.get(k)) for pair in pairs for k in ("annotator_a", "annotator_b")
        ):
            bad.append(p)
    return bad


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} <tree>", file=sys.stderr)
        return 2
    root = Path(sys.argv[1])
    # A typo'd or not-yet-written path has nothing to walk, and a walk over nothing finds
    # nothing: without this the gate would report a path that does not exist as clean.
    if not root.is_dir():
        print(
            f"no tree to check at {root} (not an existing directory)", file=sys.stderr
        )
        return 2
    try:
        bad = offenders(root)
    except SchemaError as exc:
        print(f"cannot check {root}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # Unreadable bytes, truncated JSON, a permission error: the tree is unchecked, and
        # saying so is not the same as saying it holds names (exit 1).
        print(f"checker crashed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
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
