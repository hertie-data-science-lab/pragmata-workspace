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
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

# This file is scripts/lib/workspace.py -> parents[2] is the workspace root.
ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"                          # pragmata base_dir; tools write DATA_DIR/<tool>
SETTINGS = ROOT / "configs" / "settings.conf"     # workspace-global operational tunables


def stage(tool: str) -> SimpleNamespace:
    """Per-stage path bundle for a pipeline tool (annotation, eval, ...).

    Mirrors pragmata's tool_root model: ``data`` is the pragmata tool dir
    (DATA_DIR/<tool>); the others are the workspace-owned stage dirs.
    """
    return SimpleNamespace(
        scripts=ROOT / "scripts" / tool,
        configs=ROOT / "configs" / tool,
        data=DATA_DIR / tool,
        logs=ROOT / "logs" / tool,
        reports=ROOT / "reports" / tool,
    )


# Annotation stage (the only stage today; eval/ is a stub). Eval scripts call stage("eval").
_A = stage("annotation")
SCRIPTS_DIR = _A.scripts
CONFIGS_DIR = _A.configs                           # domains/, querygen_specs/, users.*
DOMAINS_DIR = CONFIGS_DIR / "domains"              # per-domain annotation task YAMLs
SPECS_DIR = CONFIGS_DIR / "querygen_specs"
LOGS_DIR = _A.logs                                 # monitor.jsonl + run logs (flat)
REPORTS_DIR = _A.reports                           # rendered tables + plots
EXPORTS_DIR = _A.data / "exports"                  # pragmata annotation tool: exports/imports
RUNS_DIR = DATA_DIR / "querygen" / "runs"          # querygen tool (pragmata sibling)
OUT_DIR = DATA_DIR / "publikationsbot"             # workspace bot output (sibling)


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
    """Load configs/settings.conf then .env (a pre-set environment beats both)."""
    load_dotenv(SETTINGS)
    load_dotenv(ROOT / ".env")


def local_dt(run_at: str) -> datetime:
    """UTC ISO timestamp -> REPORT_TZ-aware datetime for display (defaults UTC).

    Snapshots store run_at in UTC; reports show it in the configured local zone.
    """
    return datetime.fromisoformat(run_at).astimezone(ZoneInfo(os.environ.get("REPORT_TZ", "UTC")))


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
