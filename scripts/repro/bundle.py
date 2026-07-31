#!/usr/bin/env python
"""Reproducibility bundle tool — pin, verify, reproduce.

One convention: `reproducibility/<YYYY-MM-DD>-<name>/` holds a `README.md` (what/why/
how-to-verify, with a `kind:` header line) and a generated `pins.sha256` listing
repo-root-relative paths in `sha256sum` format. See `reproducibility/README.md` for the
contract; this script is its whole tooling.

  bundle.py pin NAME PATH...     create today's bundle and pin the given files/trees
  bundle.py verify [BUNDLE]      check pins per file: OK / MISMATCH / ABSENT
  bundle.py reproduce BUNDLE     replay the lineage onto its composed end state

`reproduce` always composes the whole lineage chain in date order — a lineage bundle is
only meaningful in sequence, so replaying a prefix of the chain would land on a state
that was never live. BUNDLE names the bundle you are replaying toward, and is what the
`kind:` check is applied to; it does not select how much of the chain is composed.

Exit codes for `verify`: 0 all OK, 2 any mismatch, 3 absent only. A mismatch outranks an
absence: missing bytes can be fetched, changed bytes mean the pin no longer holds.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import workspace as ws

ws.load_env()  # configs/settings.conf + .env; existing env wins

BUNDLES = ws.ROOT / "reproducibility"
PINS = "pins.sha256"
# lineage: replayed in date order to rebuild the live instance. freeze: a self-contained
# record of a run that happened once, never replayed.
KINDS = ("lineage", "freeze")


# --- bundle basics ------------------------------------------------------------------


def bundles() -> list[Path]:
    """Every bundle, in lineage order — the dir names start with an ISO date."""
    return sorted(p for p in BUNDLES.iterdir() if (p / "README.md").exists())


def bundle_dir(name: str) -> Path:
    """Resolve a bundle by directory name, or exit listing the ones that exist."""
    path = BUNDLES / name
    if not (path / "README.md").exists():
        known = "\n  ".join(b.name for b in bundles())
        raise SystemExit(
            f"no bundle {name!r} under reproducibility/. Known:\n  {known}"
        )
    return path


def header(bundle: Path, field: str) -> str | None:
    """Value of a `<field>: <value>` line in the bundle README, or None.

    Three headers are machine-read: `kind` (lineage or freeze), `status` (`retired` drops a
    bundle out of replay composition), and `fetch` (where out-of-git artefacts live,
    printed when a pin comes out ABSENT).
    """
    for line in (bundle / "README.md").read_text().splitlines():
        if line.lower().startswith(f"{field}:"):
            return line.split(":", 1)[1].strip()
    return None


def kind(bundle: Path) -> str:
    """The bundle's `kind:` header, or exit.

    Never defaulted: a bundle whose README lost the header would be silently dropped from
    the lineage composition, and a wrong end state computed without complaint.
    """
    value = header(bundle, "kind")
    if value not in KINDS:
        raise SystemExit(
            f"{bundle.name}/README.md needs a `kind: lineage` or `kind: freeze` header "
            f"line (found: {value!r})"
        )
    return value


def read_pins(bundle: Path) -> list[tuple[str, str]]:
    """(digest, repo-relative path) pairs from the bundle's pins.sha256."""
    path = bundle / PINS
    if not path.exists():
        raise SystemExit(
            f"{bundle.name} has no {PINS} — create one with `make repro-pin`"
        )
    entries = []
    for line in path.read_text().splitlines():
        if line.strip():
            digest, _, rel = line.partition("  ")
            entries.append((digest, rel.strip()))
    return entries


# --- pin ---------------------------------------------------------------------------


def expand(paths: list[str]) -> list[Path]:
    """Every file under the given paths (trees walked), sorted, repo-relative.

    A pin is a repo-relative path plus a hash, so bytes that live outside the repo must
    never get pinned under an in-repo name — they would verify OK in a checkout that
    cannot hold them. Every file is therefore resolved, not just the argument: a symlink
    met while walking a tree escapes an argument-only check. An outside argument is
    fatal; an outside file inside a tree is skipped with a warning, since the rest of
    that tree is still worth pinning.
    """
    found: set[Path] = set()
    for arg in paths:
        p = Path(arg).resolve()
        if not p.exists():
            raise SystemExit(f"no such path: {arg}")
        if not p.is_relative_to(ws.ROOT):
            raise SystemExit(f"outside the repo, cannot be pinned: {arg}")
        for q in [p] if p.is_file() else sorted(p.rglob("*")):
            if not q.is_file():
                continue
            if not q.resolve().is_relative_to(ws.ROOT):
                print(
                    f"skipped, leaves the repo: {q.relative_to(ws.ROOT)}",
                    file=sys.stderr,
                )
                continue
            found.add(q)
    return sorted(q.relative_to(ws.ROOT) for q in found)


def cmd_pin(args: argparse.Namespace) -> int:
    # Local date, matching the other dated output dirs in the workspace.
    bundle = BUNDLES / f"{date.today().isoformat()}-{args.name}"  # noqa: DTZ011
    rel = bundle.relative_to(ws.ROOT)
    if bundle.exists():
        raise SystemExit(
            f"{rel} already exists — a bundle is a record, never rewritten"
        )

    files = expand(args.paths)
    if not files:
        raise SystemExit("nothing to pin")
    bundle.mkdir(parents=True)
    (bundle / PINS).write_text(
        "".join(f"{ws.sha256_file(ws.ROOT / f)}  {f}\n" for f in files)
    )
    (bundle / "README.md").write_text(readme_stub(bundle, args.kind, len(files)))
    print(f"created {rel}/ with {len(files)} pins")
    print(f"next: fill in {rel}/README.md (what, why, how to verify)")
    return 0


def readme_stub(bundle: Path, kind: str, nfiles: int) -> str:
    """Bundle README skeleton: the machine-read headers plus the sections to fill in."""
    git = ws.git_describe(ws.ROOT)
    rows = [
        f"| Files | {nfiles} |",
        f"| Workspace git | `{git['sha']}` (dirty: {git['dirty']}) |",
    ]
    if sha := ws.pragmata_pin()["sha"]:
        rows.append(f"| pragmata git | `{sha}` |")
    return f"""# {bundle.name}

kind: {kind}

**What** — TODO: the operation or run this bundle records.

**Why** — TODO: why it is pinned, and what breaks if it drifts.

**How to verify** — `make repro-verify PIN={bundle.name}`

If a pinned artefact lives outside git, add a `fetch: <where to get it>` header line
beside `kind:` — `repro-verify` prints it whenever a pin comes out ABSENT.

## Pins

| Pin | Value |
|---|---|
{chr(10).join(rows)}
"""


# --- verify ------------------------------------------------------------------------


def cmd_verify(args: argparse.Namespace) -> int:
    worst = 0
    for bundle in [bundle_dir(args.bundle)] if args.bundle else bundles():
        ok = mismatch = absent = 0
        for digest, rel in read_pins(bundle):
            path = ws.ROOT / rel
            if not path.exists():
                state, absent = "ABSENT", absent + 1
            elif ws.sha256_file(path) != digest:
                state, mismatch = "MISMATCH", mismatch + 1
            else:
                state, ok = "OK", ok + 1
            print(f"{rel}: {state}")
        print(f"== {bundle.name}: {ok} OK, {mismatch} MISMATCH, {absent} ABSENT")
        if absent:
            hint = header(bundle, "fetch") or f"see {bundle.name}/README.md"
            print(f"   absent artefacts are not in git — fetch: {hint}")
        worst = max(worst, 2 if mismatch else 3 if absent else 0)
    return worst


# --- reproduce ---------------------------------------------------------------------


def retired(bundle: Path) -> bool:
    """Whether the bundle is excluded from replay by a `status: retired` header.

    A retired bundle declared an end state that was never applied and is no longer wanted,
    so replaying it would move live away from where it actually is. `kind:` stays whatever
    it was — the bundle is still an honest record of a decision, just not a live one.
    """
    return header(bundle, "status") == "retired"


def compose_keep_lists(dest: Path) -> dict[str, list[tuple[str, int]]]:
    """Copy every active lineage bundle's keep-lists into `dest`, latest bundle winning.

    Bundles replay in date order (their dir names sort by ISO date). A later bundle's
    `<ws>__<dataset>.ids` REPLACES the earlier one wholesale: a keep-list is a declared
    end state for that dataset, not an addition. Unioning them instead would resurrect
    exactly the records a later bundle descoped.

    Returns each keep-list's declaring bundles in order, so the caller can report which
    ones were superseded.
    """
    seen: dict[str, list[tuple[str, int]]] = {}
    for bundle in bundles():
        if kind(bundle) != "lineage" or retired(bundle):
            continue
        for f in sorted((bundle / "keep_lists").glob("*.ids")):
            ids = [ln for ln in f.read_text().splitlines() if ln.strip()]
            seen.setdefault(f.name, []).append((bundle.name, len(ids)))
            shutil.copyfile(f, dest / f.name)
    return seen


def cmd_reproduce(args: argparse.Namespace) -> int:
    bundle = bundle_dir(args.bundle)
    if (found := kind(bundle)) != "lineage":
        raise SystemExit(
            f"{bundle.name} is kind: {found} — only lineage bundles replay. A freeze is a "
            "self-contained record of something that happened once; verify it instead."
        )
    if retired(bundle):
        raise SystemExit(
            f"{bundle.name} is status: retired — it is excluded from the composition, so "
            "replaying toward it would move live away from where it is. See its README."
        )

    with tempfile.TemporaryDirectory() as tmp:
        keep_lists = Path(tmp)
        seen = compose_keep_lists(keep_lists)
        if not seen:
            raise SystemExit("no keep-lists in any lineage bundle — nothing to replay")

        print(
            "== composed keep-lists: every lineage bundle in date order, latest wins =="
        )
        for skipped in bundles():
            if retired(skipped):
                print(f"  skipped, status: retired -> {skipped.name}")
        for name, chain in sorted(seen.items()):
            if len(chain) > 1:
                trail = " -> ".join(f"{n} ({src})" for src, n in chain)
                print(f"  {name[:-4].replace('__', '/')}: {trail}")
        total = sum(chain[-1][1] for chain in seen.values())
        print(f"expected end state: {total} records across {len(seen)} datasets")

        if args.apply and args.mode == "structure":
            for domain in ws.domains():
                print(f"== import {domain} ==")
                subprocess.run(
                    ["bash", "scripts/annotation/import.sh", domain],
                    cwd=ws.ROOT,
                    check=True,
                )
        elif args.apply and args.mode == "responses":
            if not args.backup:
                raise SystemExit(
                    "--mode responses needs --backup <dir> (the dump to restore)"
                )
            run_py(
                "scripts/annotation/argilla_backup.py",
                "restore",
                args.backup,
                "--apply",
            )

        print(
            "== prune live -> composed keep-lists (no --apply = preview, and the check) =="
        )
        prune = [
            "scripts/annotation/prune_to_keeplist.py",
            "--keep-lists",
            str(keep_lists),
        ]
        return run_py(*prune, *(["--apply"] if args.apply else [])).returncode


def run_py(*argv: str) -> subprocess.CompletedProcess:
    """Run a workspace script under this interpreter, from the repo root."""
    return subprocess.run([sys.executable, *argv], cwd=ws.ROOT, check=False)


# --- cli ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pin", help="create today's bundle and pin files/trees into it")
    p.add_argument("name", help="bundle name; the dir becomes <today>-<name>")
    p.add_argument("paths", nargs="+", help="files or trees to pin (trees walked)")
    p.add_argument(
        "--kind",
        choices=KINDS,
        default="lineage",
        help="README kind header: lineage replays in date order, freeze never replays",
    )
    p.set_defaults(fn=cmd_pin)

    p = sub.add_parser("verify", help="check bundle pins against the working tree")
    p.add_argument("bundle", nargs="?", help="bundle dir name; default every bundle")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser(
        "reproduce", help="replay the lineage onto its composed end state"
    )
    p.add_argument(
        "bundle",
        help="bundle dir name to replay toward; the whole lineage chain is composed either way",
    )
    p.add_argument(
        "--mode",
        choices=("structure", "responses"),
        help="how to rebuild the superset before pruning: re-import, or restore a backup",
    )
    p.add_argument("--backup", help="backup dir to restore, for --mode responses")
    p.add_argument("--apply", action="store_true", help="mutate; default preview only")
    p.set_defaults(fn=cmd_reproduce)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
