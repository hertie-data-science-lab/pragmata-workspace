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

import csv
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

# This file is scripts/lib/workspace.py -> parents[2] is the workspace root.
ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"  # pragmata base_dir; tools write DATA_DIR/<tool>
SETTINGS = ROOT / "configs" / "settings.conf"  # workspace-global operational tunables


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
CONFIGS_DIR = _A.configs  # domains/, querygen_specs/, users.*
DOMAINS_DIR = CONFIGS_DIR / "domains"  # per-domain annotation task YAMLs
SPECS_DIR = CONFIGS_DIR / "querygen_specs"
LOGS_DIR = _A.logs  # log.jsonl + run logs (flat)
REPORTS_DIR = _A.reports  # rendered tables + plots
EXPORTS_DIR = _A.data / "exports"  # pragmata annotation tool: exports/imports
RUNS_DIR = DATA_DIR / "querygen" / "runs"  # querygen tool (pragmata sibling)
OUT_DIR = DATA_DIR / "publikationsbot"  # workspace bot output (sibling)

# Shape of one logs/annotation/log.jsonl snapshot. Lives here because log.py writes it and
# report_tables.py reads it, and a duplicated constant would drift. Bump on an incompatible
# change; report_tables.py refuses anything older rather than rendering a partial report.
#   2: agreement is a single pooled α per (task, label) under `pooled_agreement`; the
#      per-domain and total n_items-weighted `mean_alpha` blocks are gone.
SNAPSHOT_SCHEMA_VERSION = 2


def eval_pragmata() -> SimpleNamespace:
    """Resolve the eval-side pragmata pin: source tree, venv python, and CLI binary.

    The annotation pipeline pins ``PRAGMATA_SRC`` at a frozen demo checkout that has
    no eval module at all, so eval needs its own pin. Kept separate (rather than
    moving the shared pin forward) so the live Argilla instance's annotation and
    export behaviour stays frozen while eval tracks upstream.

    The eval CLI runs from the eval checkout's OWN venv: it needs ``pandera``, which
    the workspace venv does not carry. Workspace-side glue keeps using ``$PY``.
    """
    src = os.environ.get("PRAGMATA_EVAL_SRC")
    if not src:
        raise SystemExit("PRAGMATA_EVAL_SRC is unset — see .env.example (eval needs its own pragmata pin).")
    src_path = Path(src).resolve()
    if not (src_path / "pragmata").is_dir():
        raise SystemExit(f"PRAGMATA_EVAL_SRC does not look like a pragmata src tree: {src_path}")
    venv = Path(os.environ.get("PRAGMATA_EVAL_VENV") or src_path.parent / ".venv").resolve()
    binary = venv / "bin" / "pragmata"
    if not binary.exists():
        raise SystemExit(f"no pragmata CLI at {binary} — set PRAGMATA_EVAL_VENV to the eval checkout's venv.")
    return SimpleNamespace(src=src_path, repo=src_path.parent, venv=venv, bin=binary)


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
    return datetime.fromisoformat(run_at).astimezone(
        ZoneInfo(os.environ.get("REPORT_TZ", "UTC"))
    )


def report_dir(run_at: str) -> Path:
    """Per-snapshot report subdir reports/annotation/<local-date>/ (created).

    Both the markdown report and the PNGs for one snapshot live here together;
    link_latest() points reports/annotation/_latest at the newest one.
    """
    d = REPORTS_DIR / f"{local_dt(run_at):%Y-%m-%d}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def stage_report_dir(stage: str, explicit: Path | None = None) -> Path:
    """Dated output dir for a stage's generated reports, ``reports/<stage>/<today>/``.

    Lives here rather than in a stage helper so every script in one bundle resolves the
    same date from the same clock. corpus_catalog.py runs as a standalone uv script and
    cannot import the pandas-dependent eval helpers, and when it computed its own date
    it drifted: a UTC date against the others' local date puts the CSVs in two different
    directories for the last hours of each local day.
    """
    from datetime import date

    target = explicit or (ROOT / "reports" / stage / date.today().isoformat())
    target.mkdir(parents=True, exist_ok=True)
    return target


def link_latest(target: Path) -> None:
    """Point reports/annotation/_latest at ``target`` (relative link, atomic swap)."""
    link = REPORTS_DIR / "_latest"
    tmp = REPORTS_DIR / "_latest.tmp"
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(target.name)  # relative: just the date dir name
    tmp.replace(link)


def domains() -> list[str]:
    """All domain stems, derived from configs/annotation/domains/*.yaml (sorted).

    Single source of truth for "which domains exist" — replaces hardcoded lists.
    Underscore-prefixed helper files are excluded.
    """
    return sorted(
        p.stem for p in DOMAINS_DIR.glob("*.yaml") if not p.name.startswith("_")
    )


# --- provenance: every report CSV ships with a sidecar saying how it was made ----
#
# Report numbers get lifted into figures and then into a published document, so a
# bare CSV is not enough — each one needs to name the code and inputs it came from.
# This mirrors the manifest convention in scripts/eval/sync.sh (sorted per-file
# sha256) and the dated bundles under reproducibility/.


def sha256_file(path: Path) -> str:
    """sha256 of a file's bytes, as a hex digest."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def git_describe(repo: Path) -> dict:
    """Commit SHA, branch and dirty flag for a git repo (all None if unavailable).

    ``dirty`` matters as much as the SHA: a number scored from a modified tree is
    not reproducible from the SHA alone, so callers can refuse to run on True.
    """

    def _git(*args: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True,
                text=True,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None
        return out.stdout.strip()

    sha = _git("rev-parse", "HEAD")
    if sha is None:
        return {"sha": None, "branch": None, "dirty": None}
    status = _git("status", "--porcelain")
    return {
        "sha": sha,
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(status) if status is not None else None,
    }


def provenance(
    *,
    script: str,
    inputs: list[Path] | None = None,
    pragmata_src: Path | None = None,
    **extra,
) -> dict:
    """Provenance record for a generated artifact.

    ``inputs`` are hashed individually so a changed export is detectable without
    re-deriving anything. ``extra`` carries the caller's own decisions — filter
    policy, bootstrap seed, source snapshot — which are the part a reader most
    needs and the part no generic helper can infer.
    """
    inputs = inputs or []
    record = {
        "generated_at": datetime.now(UTC).isoformat(),
        "script": script,
        "workspace_git": git_describe(ROOT),
        "inputs": [
            {"path": str(p.relative_to(ROOT) if p.is_relative_to(ROOT) else p), "sha256": sha256_file(p)}
            for p in inputs
            if p.exists()
        ],
    }
    if pragmata_src is not None:
        # The pin is <checkout>/src, so the repo is its parent.
        record["pragmata_git"] = git_describe(pragmata_src.parent)
        record["pragmata_src"] = str(pragmata_src)
    record.update(extra)
    return record


def write_csv(path: Path, rows: list[dict], *, columns: list[str], prov: dict) -> None:
    """Write ``rows`` to ``path`` plus ``<path>.provenance.json``.

    ``columns`` is explicit rather than inferred from the first row: these CSVs are
    a contract with the report author, so column order and presence must not depend
    on which rows happen to be non-empty. Missing keys are written as empty.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})
    sidecar = path.with_suffix(path.suffix + ".provenance.json")
    sidecar.write_text(json.dumps(prov, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
