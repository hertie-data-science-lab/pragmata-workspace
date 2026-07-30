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
import sys
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


# Annotation stage paths. The eval scripts resolve their own output dir through
# stage_report_dir("eval") and read explicit paths otherwise, so they need no bundle here.
_A = stage("annotation")
DOMAINS_DIR = _A.configs / "domains"  # per-domain annotation task YAMLs
LOGS_DIR = _A.logs  # log.jsonl + run logs (flat)
REPORTS_DIR = _A.reports  # rendered tables + plots
EXPORTS_DIR = _A.data / "exports"  # pragmata annotation tool: exports/imports
RUNS_DIR = DATA_DIR / "querygen" / "runs"  # querygen tool (pragmata sibling)
OUT_DIR = DATA_DIR / "publikationsbot"  # workspace bot output (sibling)

# Shape of one logs/annotation/log.jsonl snapshot. Lives here because log.py writes it and
# the reporting scripts read it, and a duplicated constant would drift. Bump on an
# incompatible change; check_snapshot then refuses anything older.
#   2: agreement is a single pooled α per (task, label) under `pooled_agreement`; the
#      per-domain and total n_items-weighted `mean_alpha` blocks are gone.
#   3: the pooled gap statistics and the IAA bootstrap parameters (`iaa` block: resamples,
#      seed) are part of the shape. Both had been added without a bump, which forced
#      check_snapshot to probe for individual fields; the version now carries that.
SNAPSHOT_SCHEMA_VERSION = 3

SNAPSHOT_LOG = LOGS_DIR / "log.jsonl"


def _snapshot_log(path: Path | None) -> Path:
    """Resolve the snapshot log path, or exit if there is none yet."""
    path = path or SNAPSHOT_LOG
    if not path.exists():
        raise SystemExit(
            f"no snapshot log at {path} - run `make annotation-log` first."
        )
    return path


def read_snapshots(path: Path | None = None) -> list[dict]:
    """Every snapshot in the log, oldest first — for callers that need the history.

    One snapshot per line. Prefer select_snapshot() when only one is wanted: this parses
    all of them.
    """
    path = _snapshot_log(path)
    snapshots = read_jsonl(path)
    if not snapshots:
        raise SystemExit(f"no snapshots in {path}")
    return snapshots


def select_snapshot(path: Path | None = None, line: int = -1) -> dict:
    """One checked snapshot by 0-based index, negative counting from the end.

    Splits the log but parses only the selected line: the file is tens of MB and the
    reporting scripts need a single snapshot out of it.
    """
    path = _snapshot_log(path)
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    if not lines:
        raise SystemExit(f"no snapshots in {path}")
    try:
        snapshot = json.loads(lines[line])
    except IndexError:
        raise SystemExit(f"line {line} out of range ({len(lines)} snapshots)") from None
    check_snapshot(snapshot)
    return snapshot


def check_snapshot(snapshot: dict) -> None:
    """Refuse a snapshot the current reporting code cannot render faithfully.

    The failure mode is silent rather than loud: an older snapshot renders a section
    blank or, worse, from a different definition, with no error. The version alone
    decides — every shape change now comes with a bump, so no per-field probing.
    """
    at = snapshot.get("run_at", "unknown date")
    got = snapshot.get("schema_version", 1)
    if got < SNAPSHOT_SCHEMA_VERSION:
        raise SystemExit(
            f"snapshot {at} is schema v{got}; this code needs "
            f"v{SNAPSHOT_SCHEMA_VERSION}.\n"
            "The shapes are not interchangeable: agreement moved to a single pooled alpha "
            "per task x label, and the cadence and IAA-parameter blocks arrived later.\n"
            "Re-run `make annotation-log` for a current snapshot, or check out a revision "
            "from before the change to render this one."
        )


def find_snapshot(run_at: str, path: Path | None = None) -> tuple[dict, dict]:
    """(snapshot, identity) for the snapshot with this exact ``run_at``.

    Reports pin the snapshot they read by timestamp rather than taking whichever is
    last, so re-running a report later reproduces it. ``identity`` is
    ``{run_at, sha256}`` where the digest covers that ONE line: the log is append-only,
    so hashing the whole file would change every night and pin nothing.
    """
    path = _snapshot_log(path)
    # Streamed and parsed line by line: the log grows without rotation, and matching on
    # the raw text would couple this lookup to the writer's JSON separator convention.
    with path.open(encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            snapshot = json.loads(line)
            if snapshot.get("run_at") != run_at:
                continue
            check_snapshot(snapshot)
            digest = hashlib.sha256(line.encode()).hexdigest()
            return snapshot, {"run_at": run_at, "sha256": digest}
    raise SystemExit(
        f"no snapshot with run_at={run_at} in {path}.\n"
        "The report pins its snapshot by timestamp; pass --snapshot-run-at to select "
        "another, or re-run `make annotation-log` and update the pin."
    )


def require_env(*names: str) -> list[str]:
    """Values of the named env vars, or exit naming the missing ones.

    Empty counts as unset, matching common.sh's require_env.
    """
    values = [os.environ.get(n, "") for n in names]
    missing = [n for n, v in zip(names, values) if not v]
    if missing:
        raise SystemExit(f"missing required env: {', '.join(missing)} (set in .env)")
    return values


def argilla_client():
    """Argilla client for the configured instance, built by pragmata.

    The pragmata import is function-local: this module is imported by standalone
    ``uv run --script`` consumers that have no pragmata, and importing it at module scope
    also cost every ``--help`` several seconds.
    """
    from pragmata.core.annotation.client import resolve_argilla_client

    url, key = require_env("ARGILLA_API_URL", "ARGILLA_API_KEY")
    return resolve_argilla_client(url, key)


def username_to_user_id(client=None) -> dict[str, str]:
    """username -> Argilla user id, from the live instance.

    The one definition of the pseudonymisation contract: the same annotator resolves to
    the same UUID everywhere (log snapshots and export rewrites both call this), so
    identities stay comparable across artifacts. Argilla user ids are assigned once and
    never change. The mapping is derived at runtime and never written down.
    """
    from pragmata.core.annotation.export_fetcher import build_user_lookup

    lookup = build_user_lookup(client if client is not None else argilla_client())
    return {name: str(uid) for uid, name in lookup.items()}


def eval_pragmata() -> SimpleNamespace:
    """Resolve the eval-side pragmata pin: source tree, its repo, and the CLI to run it.

    The annotation pipeline pins ``PRAGMATA_SRC`` at a frozen demo checkout that has
    no eval module at all, so eval needs its own pin. Kept separate (rather than
    moving the shared pin forward) so the live Argilla instance's annotation and
    export behaviour stays frozen while eval tracks upstream.

    One venv runs both: the CLI is the workspace venv's own ``pragmata``, invoked with
    the pin on ``PYTHONPATH``, which shadows whatever the venv installed. The only thing
    the eval module needs beyond the annotation side is ``pandera``, so the workspace
    venv carries it rather than a second venv existing to supply it.
    """
    src = os.environ.get("PRAGMATA_EVAL_SRC")
    if not src:
        raise SystemExit(
            "PRAGMATA_EVAL_SRC is unset — see .env.example (eval needs its own pragmata pin)."
        )
    src_path = Path(src).resolve()
    if not (src_path / "pragmata").is_dir():
        raise SystemExit(
            f"PRAGMATA_EVAL_SRC does not look like a pragmata src tree: {src_path}"
        )
    binary = ROOT / ".venv" / "bin" / "pragmata"
    if not binary.exists():
        raise SystemExit(f"no pragmata CLI at {binary} — create the workspace venv.")
    return SimpleNamespace(src=src_path, repo=src_path.parent, bin=binary)


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ; a non-empty existing value wins.

    No inline comments. An empty value counts as unset and gets filled - see the
    convention in scripts/lib/common.sh.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not os.environ.get(key):
            os.environ[key] = val.strip()


def load_env() -> None:
    """Load configs/settings.conf then .env, then apply the PRAGMATA_SRC pin.

    The Makefile's Python targets never go through common.sh, so the pin was silently
    ignored when they ran standalone. Applying it here covers every Python entrypoint.
    """
    load_dotenv(SETTINGS)
    load_dotenv(ROOT / ".env")
    src = os.environ.get("PRAGMATA_SRC")
    if src:
        # Front, not merely present: an installed pragmata may already be on sys.path and
        # the point of the pin is to shadow it. No PYTHONPATH export - no Python
        # entrypoint here spawns a pragmata subprocess off the inherited environment
        # (score_human builds its own env for the separate eval pin).
        sys.path.insert(0, src)


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

    Lives here, not in a stage helper, so every script in one bundle resolves the same
    date from the same clock — including corpus_catalog.py, which cannot import the
    pandas-dependent eval helpers. A script computing its own date drifts: a UTC date
    against the others' local date splits one bundle across two directories for the last
    hours of each local day.
    """
    from datetime import date

    # DTZ011 is suppressed deliberately: the LOCAL date is the point - report dirs are
    # named in the operator's timezone, not UTC, matching report_dir() above.
    target = explicit or (ROOT / "reports" / stage / date.today().isoformat())  # noqa: DTZ011
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
# This mirrors the manifest convention in scripts/transfer/sync.sh (sorted per-file
# sha256) and the dated bundles under reproducibility/.
#
# The sidecar records IDENTITY only: code, inputs, parameters, snapshot, dictionary.
# What a column means and how to read it belongs in the hand-authored data dictionary,
# which every sidecar pins by hash — one authored explanation, not a copy per artifact —
# so a CSV can always be paired with the wording that was current when it was written.
DATA_DICTIONARY = ROOT / "docs" / "eval-data-dictionary.md"


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
    snapshot: dict | None = None,
    **extra,
) -> dict:
    """Provenance record for a generated artifact — identity, not explanation.

    ``inputs`` are hashed individually so a changed export is detectable without
    re-deriving anything. ``snapshot`` is the ``{run_at, sha256}`` identity from
    find_snapshot, for the artifacts derived from a log snapshot. ``extra`` carries the
    caller's own parameters — filter policy, confidence level, seeds — which no generic
    helper can infer.

    The data dictionary is pinned unconditionally rather than passed in: every eval
    artifact is read alongside it, so no script gets to omit it.
    """
    inputs = inputs or []
    if not DATA_DICTIONARY.exists():
        raise SystemExit(
            f"no data dictionary at {DATA_DICTIONARY} — every eval sidecar pins it, and "
            "write_csv copies it next to the CSVs."
        )
    record = {
        "generated_at": datetime.now(UTC).isoformat(),
        "script": script,
        "workspace_git": git_describe(ROOT),
        "data_dictionary": {
            "path": str(DATA_DICTIONARY.relative_to(ROOT)),
            "sha256": sha256_file(DATA_DICTIONARY),
        },
        "inputs": [
            {
                "path": str(p.relative_to(ROOT) if p.is_relative_to(ROOT) else p),
                "sha256": sha256_file(p),
            }
            for p in inputs
            if p.exists()
        ],
    }
    if pragmata_src is not None:
        # The pin is <checkout>/src, so the repo is its parent.
        record["pragmata_git"] = git_describe(pragmata_src.parent)
        record["pragmata_src"] = str(pragmata_src)
    if snapshot is not None:
        record["snapshot"] = snapshot
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
    sidecar.write_text(
        json.dumps(prov, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    # The dictionary travels WITH the CSVs whose sidecars pin it: a bare CSV in a
    # handover folder is unreadable without the wording the pin refers to. Done here,
    # in the layer that already knows the output directory, so a direct script run and
    # a make run behave identically and no second clock resolves "today's dir".
    if "data_dictionary" in prov:
        (path.parent / DATA_DICTIONARY.name).write_bytes(DATA_DICTIONARY.read_bytes())


def read_jsonl(path: Path) -> list[dict]:
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
