#!/usr/bin/env python3
"""Check the docs against the code they describe. Behind `make docs-check`.

Two kinds of drift this catches, both of which have happened:

1. **The README's target list falls behind the Makefile.** The README reproduces the
   targets with its own shortened wording rather than piping `make help`, because the
   grouping and the abbreviated flags are what make it readable. That curation is worth
   keeping, so this checks the *set* of target names both ways rather than the text -
   a target added to the Makefile and never documented fails, and so does a README
   line naming a target that no longer exists.
2. **A cross-reference points at a file or heading that was renamed.** The docs are
   heavily cross-linked and the anchors are generated from heading text, so a retitled
   section silently breaks every deep link into it.
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# `target: ## help text` - the same pattern the `help` target itself greps for.
MAKE_TARGET = re.compile(r"^([a-z][a-z0-9-]*):.*?##", re.MULTILINE)
# `make <target>` as written in prose or in the README's fenced block.
README_TARGET = re.compile(r"\bmake ([a-z][a-z0-9-]*)")
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)

# Targets deliberately absent from the README: `help` prints the list the README mirrors,
# and `setup` is covered by its own prose section rather than the target block.
README_EXEMPT = {"help", "setup"}


def anchor(heading: str) -> str:
    """GitHub's heading -> anchor slug: strip markup and punctuation, spaces to dashes."""
    s = re.sub(r"[`*]", "", heading.strip().lower())
    s = re.sub(r"[^\w\s-]", "", s)
    return re.sub(r"\s", "-", s)


def tracked(*globs: str) -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", *globs],
        capture_output=True,
        text=True,
        check=True,
    )
    return [ROOT / line for line in out.stdout.split() if (ROOT / line).exists()]


def check_targets() -> list[str]:
    makefile = (ROOT / "Makefile").read_text()
    readme = (ROOT / "README.md").read_text()
    documented = set(MAKE_TARGET.findall(makefile)) - README_EXEMPT
    mentioned = set(README_TARGET.findall(readme))

    bad = []
    for name in sorted(documented - mentioned):
        bad.append(
            f"Makefile target `{name}` has a ## help line but is not in README.md"
        )
    for name in sorted(mentioned - documented - README_EXEMPT):
        bad.append(
            f"README.md names `make {name}`, which is not a documented Makefile target"
        )
    return bad


def check_links() -> list[str]:
    anchors = {
        p: {anchor(h) for h in HEADING.findall(p.read_text())} for p in tracked("*.md")
    }
    bad = []
    for path in tracked(
        "*.md", "*.py", "*.sh", "*.toml", "*.conf", "*.txt", "Makefile"
    ):
        for target in MD_LINK.findall(path.read_text()):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            file_part, _, frag = target.partition("#")
            dest = path if not file_part else (path.parent / file_part).resolve()
            rel = path.relative_to(ROOT)
            if not dest.exists():
                bad.append(f"{rel}: link to a missing file - {target}")
            elif (
                frag
                and dest.suffix == ".md"
                and frag.lower() not in anchors.get(dest, set())
            ):
                bad.append(f"{rel}: link to a missing heading - {target}")
    return bad


def main() -> int:
    problems = check_targets() + check_links()
    if problems:
        print("\n".join(f"  {p}" for p in problems))
        print(
            f"\n{len(problems)} problem(s). The docs and the code have drifted apart."
        )
        return 1
    print(
        "docs-check: README target list matches the Makefile; every doc link resolves"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
