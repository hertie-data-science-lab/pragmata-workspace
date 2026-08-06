"""Shared helpers + path constants for pragmata-workspace Python glue scripts.

Centralizes the workspace layout, the .env / config loader ("existing env wins", matching
scripts/lib/common.sh), the domain list (derived from configs/annotation/ rather than
hardcoded), JSONL read/write, and the `.provenance.json` record every report CSV ships
with.

Import from any script under scripts/ with a two-line preamble:

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    import workspace as ws
"""

from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

# This file is scripts/lib/workspace.py -> parents[2] is the workspace root.
ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"  # pragmata base_dir; tools write DATA_DIR/<tool>
SETTINGS = ROOT / "configs" / "settings.conf"  # workspace-global operational tunables

# Annotation stage paths — the only stage with a fixed set of them. The eval scripts
# resolve their output dir through stage_report_dir("eval") and read explicit paths
# otherwise, so they need none here.
DOMAINS_DIR = ROOT / "configs" / "annotation" / "domains"  # per-domain task YAMLs
LOGS_DIR = ROOT / "logs" / "annotation"  # log.jsonl + run logs (flat)
REPORTS_DIR = ROOT / "reports" / "annotation"  # rendered tables + plots
EXPORTS_DIR = DATA_DIR / "annotation" / "exports"  # pragmata's exports/imports
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

    Reads the whole log — indexing needs the line count — but parses JSON for the
    selected line only: the file is tens of MB and the reporting scripts need a single
    snapshot out of it. Prefer find_snapshot() when the wanted ``run_at`` is known; that
    one streams and stops at the match.
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


def _first_snapshot_matching(
    predicate, path: Path | None = None
) -> tuple[dict, dict] | None:
    """(snapshot, identity) for the first snapshot with a ``run_at`` predicate(...) accepts.

    Streamed and parsed line by line: the log grows without rotation, and matching on the
    raw text would couple this lookup to the writer's JSON separator convention.
    """
    path = _snapshot_log(path)
    with path.open(encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            snapshot = json.loads(line)
            run_at = snapshot.get("run_at")
            if not run_at or not predicate(run_at):
                continue
            check_snapshot(snapshot)
            digest = hashlib.sha256(line.encode()).hexdigest()
            return snapshot, {"run_at": run_at, "sha256": digest}
    return None


def find_snapshot(run_at: str, path: Path | None = None) -> tuple[dict, dict]:
    """(snapshot, identity) for the snapshot with this exact ``run_at``.

    Reports pin the snapshot they read by timestamp rather than taking whichever is
    last, so re-running a report later reproduces it. ``identity`` is
    ``{run_at, sha256}`` where the digest covers that ONE line: the log is append-only,
    so hashing the whole file would change every night and pin nothing.
    """
    found = _first_snapshot_matching(lambda at: at == run_at, path)
    if found is None:
        raise SystemExit(
            f"no snapshot with run_at={run_at} in {_snapshot_log(path)}.\n"
            "The report pins its snapshot by timestamp; pass --snapshot-run-at to select "
            "another, or re-run `make annotation-log` and update the pin."
        )
    return found


# A freeze pairs one export moment with the one log snapshot taken right after it. The
# nightly cron's actual gap is under a minute (scripts/daily.sh runs export then log in one
# invocation), so anything past this is almost certainly not the snapshot from the same run
# - a hand-typed RUN_AT naming the wrong day, or an export re-run without a matching log.
_MAX_EXPORT_TO_SNAPSHOT_LAG = timedelta(hours=2)


def export_created_at(export_dir: Path) -> str:
    """Newest ``created_at`` across an export tree's per-programme meta files.

    Every programme's annotation_export.meta.json is written by pragmata at export time,
    staggered a few seconds apart as export.sh loops over programmes. The newest one
    anchors the run's end - the moment the log snapshot taken right after it describes.
    """
    created_ats = [
        v
        for p in sorted(export_dir.glob("*/annotation_export.meta.json"))
        if (v := json.loads(p.read_text()).get("created_at"))
    ]
    if not created_ats:
        raise SystemExit(
            f"no annotation_export.meta.json with created_at under {export_dir}"
        )
    return max(created_ats, key=datetime.fromisoformat)


def find_first_snapshot_after(
    after: str, path: Path | None = None
) -> tuple[dict, dict]:
    """(snapshot, identity) for the earliest snapshot with run_at strictly after ``after``.

    Same streamed lookup and schema check as find_snapshot, but matched by ORDER rather
    than by exact value - for deriving RUN_AT from an export's created_at instead of an
    operator-supplied timestamp.
    """
    after_dt = datetime.fromisoformat(after)
    found = _first_snapshot_matching(
        lambda at: datetime.fromisoformat(at) > after_dt, path
    )
    if found is not None:
        return found
    raise SystemExit(
        f"no snapshot after {after} in {_snapshot_log(path)}.\n"
        "The export finished but `make annotation-log` has not logged one since - run it, "
        "or pass RUN_AT explicitly to pin a different snapshot."
    )


def _resolve_run_at(created_at: str, run_at: str | None) -> str:
    """The RUN_AT to pin given an export's created_at: derived if omitted, else checked.

    A snapshot predating the export describes the Argilla instance BEFORE these labels
    were exported, not beside them; one lagging by more than _MAX_EXPORT_TO_SNAPSHOT_LAG is
    implausibly far from a same-run pairing to be it by coincidence. The lag guard applies
    to the derived value too: "the first snapshot after the export" is only the export's
    own snapshot if one was logged at all, and the next nightly run also comes after.
    """
    created_dt = datetime.fromisoformat(created_at)
    if run_at is None:
        _snapshot, identity = find_first_snapshot_after(created_at)
        origin = f"the first snapshot after the export (run_at={identity['run_at']})"
    else:
        _snapshot, identity = find_snapshot(run_at)
        origin = f"RUN_AT={run_at}"
        if datetime.fromisoformat(identity["run_at"]) <= created_dt:
            raise SystemExit(
                f"RUN_AT={run_at} predates the export (created_at={created_at}) - that "
                "snapshot describes the Argilla instance before these labels were "
                "exported, not beside them."
            )
    lag = datetime.fromisoformat(identity["run_at"]) - created_dt
    if lag > _MAX_EXPORT_TO_SNAPSHOT_LAG:
        raise SystemExit(
            f"{origin} is {lag} after the export (created_at={created_at}) - too far from "
            "a same-run pairing (the nightly cron's gap is under a minute). Pin the "
            "snapshot `make annotation-log` printed for this export, or check that one "
            "was logged for it at all."
        )
    return identity["run_at"]


def resolve_freeze_pin(
    export_dir: Path, date: str | None, run_at: str | None
) -> tuple[str, str]:
    """(DATE, RUN_AT) to pin for a freeze - both derived from the export tree if omitted.

    Both name the same export moment: the newest ``created_at`` across the tree's
    programmes. DATE defaults to that moment's UTC calendar date, so a freeze cut a day
    late (or on a re-run export) is still named for the export it freezes, not for
    whenever the operator happened to run the command. RUN_AT defaults to the first log
    snapshot taken after that moment; derived or given, it is validated against the export
    - see ``_resolve_run_at``.
    """
    created_at = export_created_at(export_dir)
    resolved_date = date or datetime.fromisoformat(created_at).date().isoformat()
    resolved_run_at = _resolve_run_at(created_at, run_at)
    return resolved_date, resolved_run_at


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
    """Resolve the eval-side pragmata pin: source tree, its checkout, the CLI to run it.

    ``repo`` is the git checkout the source tree belongs to, or None when it is not in
    one — see pragmata_repo(); a caller that needs a SHA has to say what it does about
    that rather than get a plausible-looking wrong one.

    The installed pragmata is git-pinned in ``pyproject.toml`` to a frozen demo commit
    that has no eval module at all, so eval needs its own pin. Kept separate (rather
    than moving the shared pin forward) so the live Argilla instance's annotation and
    export behaviour stays frozen while eval tracks upstream. It cannot join
    ``pyproject.toml``: two commits of one package cannot be installed side by side.

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
    return SimpleNamespace(src=src_path, repo=pragmata_repo(src_path), bin=binary)


def pragmata_repo(src: Path) -> Path | None:
    """The git checkout a pragmata source tree belongs to, or None if there is none.

    Asked of git rather than assumed to be ``src.parent``: the documented layout is
    ``<checkout>/src``, but a tree one level deeper (or a src dir sitting loose inside
    another repo) would git-describe whatever repo happens to be above it and record that
    SHA as pragmata's. The workspace itself is excluded for the same reason — an installed
    pragmata under ``.venv/`` is inside THIS repo, and borrowing our own commit as the
    pin's would be worse than recording no SHA at all.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(src), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    top = Path(out.stdout.strip())
    return None if top == ROOT else top


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


def pragmata_pin() -> dict:
    """The annotation pragmata pin: ``{"sha": <commit of the installed package>}``.

    pragmata is a git dependency pinned to an exact SHA in pyproject.toml, so the
    authoritative record of what is installed is the wheel's own ``direct_url.json``
    (PEP 610) rather than anything the environment claims. ``sha`` is the empty string if
    pragmata was installed some other way - a caller writing provenance should notice.
    """
    try:
        raw = importlib.metadata.distribution("pragmata").read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        raw = None
    info = json.loads(raw) if raw else {}
    return {"sha": info.get("vcs_info", {}).get("commit_id", "")}


def load_env() -> None:
    """Load configs/settings.conf then .env.

    The annotation pragmata is a pinned git dependency installed into the venv, so
    there is nothing to shadow onto sys.path - importing it resolves the pin. Eval is
    the exception: it needs a different pragmata commit, which cannot be installed
    alongside this one, so ``eval_pragmata()`` shadows it per-call instead.
    """
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


# --- provenance: every report CSV ships with a .provenance.json saying how it was made ---
#
# Report numbers get lifted into figures and then into a published document, so a
# bare CSV is not enough — each one needs to name the code and inputs it came from.
# This mirrors the manifest convention in scripts/transfer/sync.sh (sorted per-file
# sha256) and the dated bundles under reproducibility/.
#
# The .provenance.json records IDENTITY only: code, inputs, parameters, snapshot, dictionary.
# What a column means and how to read it belongs in the hand-authored data dictionary,
# which every .provenance.json pins by hash — one authored explanation, not a copy per
# artifact —
# so a CSV can always be paired with the wording that was current when it was written.
DATA_DICTIONARY = ROOT / "docs" / "deliverables-data-dictionary.md"


def sha256_file(path: Path) -> str:
    """sha256 of a file's bytes, as a hex digest."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def git_describe(repo: Path | None) -> dict:
    """Commit SHA, branch and dirty flag for a git repo (all None if unavailable).

    ``dirty`` matters as much as the SHA: a number scored from a modified tree is
    not reproducible from the SHA alone, so callers can refuse to run on True. A caller
    that has no repo to describe (``None``) gets the same all-None shape, so the record
    keeps its field either way — and a ``sha`` of None is no more reproducible than a
    dirty one.
    """
    if repo is None:
        return {"sha": None, "branch": None, "dirty": None}

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


def _input_record(path: Path) -> dict:
    """One hashed input for a provenance record; a missing one is marked, never dropped.

    Omitting it would read as "this artifact was built without that input", which is a
    different (and untrue) claim from "the input it was built from is no longer there".
    """
    rel = str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path)
    if not path.exists():
        return {"path": rel, "sha256": None, "missing": True}
    return {"path": rel, "sha256": sha256_file(path)}


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
    re-deriving anything; one that does not exist is recorded as
    ``{"path": ..., "sha256": null, "missing": true}`` rather than left out, so the list
    always names every input the caller declared. ``snapshot`` is the ``{run_at, sha256}``
    identity from find_snapshot, for the artifacts derived from a log snapshot. ``extra``
    carries the caller's own parameters — filter policy, confidence level, seeds — which
    no generic helper can infer.

    The data dictionary is pinned unconditionally rather than passed in: every eval
    artifact is read alongside it, so no script gets to omit it.
    """
    inputs = inputs or []
    if not DATA_DICTIONARY.exists():
        raise SystemExit(
            f"no data dictionary at {DATA_DICTIONARY} — every eval .provenance.json pins it, and "
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
        "inputs": [_input_record(p) for p in inputs],
    }
    if pragmata_src is not None:
        # Which checkout the source tree belongs to is asked of git, not inferred from the
        # path: an installed pragmata has no checkout of its own, and its sha comes out
        # null rather than borrowed from whatever repo it happens to sit inside.
        record["pragmata_git"] = git_describe(pragmata_repo(pragmata_src))
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
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})
    provenance_path = path.with_suffix(path.suffix + ".provenance.json")
    provenance_path.write_text(
        json.dumps(prov, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    # The dictionary travels WITH the CSVs whose .provenance.json pins it: a bare CSV in a
    # handover folder is unreadable without the wording the pin refers to. Done here,
    # in the layer that already knows the output directory, so a direct script run and
    # a make run behave identically and no second clock resolves "today's dir".
    # Unconditional: provenance() pins the dictionary in every record it builds.
    (path.parent / DATA_DICTIONARY.name).write_bytes(DATA_DICTIONARY.read_bytes())


def read_jsonl(path: Path) -> list[dict]:
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
