"""Shared helpers + path constants for pragmata-workspace Python glue scripts.

Import from any script in scripts/annotation/ with a two-line preamble:

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    import workspace as ws

Centralizes the workspace layout, the .env / config loader ("existing env
wins", matching scripts/lib/common.sh), the domain list (derived from
configs/annotation/ rather than hardcoded), and JSONL read/write.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# This file is scripts/lib/workspace.py -> parents[2] is the workspace root.
ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts" / "annotation"
CONFIGS_DIR = ROOT / "configs" / "annotation"     # settings.conf, users.*, domains/, querygen_specs/
DOMAINS_DIR = CONFIGS_DIR / "domains"              # per-domain annotation task YAMLs
SPECS_DIR = CONFIGS_DIR / "querygen_specs"
DATA_DIR = ROOT / "data"                          # pragmata base_dir
EXPORTS_DIR = DATA_DIR / "annotation" / "exports"
RUNS_DIR = DATA_DIR / "querygen" / "runs"          # querygen output (pragmata tool sibling)
OUT_DIR = DATA_DIR / "publikationsbot"             # workspace bot output (sibling)
LOGS_DIR = ROOT / "logs" / "annotation"            # monitor.jsonl + run logs (flat)
REPORTS_DIR = ROOT / "reports" / "annotation"      # rendered tables + plots (unchanged)


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ; existing env wins. No inline comments."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def load_env() -> None:
    """Load configs/annotation/settings.conf then .env (a pre-set environment beats both)."""
    load_dotenv(CONFIGS_DIR / "settings.conf")
    load_dotenv(ROOT / ".env")


def domains() -> list[str]:
    """All domain stems, derived from configs/annotation/domains/*.yaml (sorted).

    Single source of truth for "which domains exist" — replaces hardcoded lists.
    Underscore-prefixed helper files are excluded.
    """
    return sorted(
        p.stem for p in DOMAINS_DIR.glob("*.yaml") if not p.name.startswith("_")
    )


def read_jsonl(path: Path) -> list[dict]:
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
