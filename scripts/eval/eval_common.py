"""Shared vocabulary for the eval-stage report scripts.

The report CSVs are a contract with the report author, so the things that decide a
number — which rows count, what a scoring unit is, which population a statistic
describes — are defined once here rather than re-derived per script.

Import with the same preamble the annotation scripts use::

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    import eval_common as ec
    import workspace as ws
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import workspace as ws  # noqa: E402

ws.load_env()  # configs/settings.conf + .env; existing env wins

TASKS = ("retrieval", "grounding", "generation")

# Label columns per task, mirroring pragmata's LABEL_COLUMNS_BY_TASK
# (core/schemas/eval_input.py). Duplicated deliberately: these scripts read exported
# CSVs and must not depend on importing pragmata just to name columns.
LABELS: dict[str, tuple[str, ...]] = {
    "retrieval": ("topically_relevant", "evidence_sufficient", "misleading"),
    "grounding": (
        "support_present",
        "unsupported_claim_present",
        "contradicted_claim_present",
        "source_cited",
        "fabricated_source",
    ),
    "generation": ("proper_action", "response_on_topic", "helpful", "incomplete", "unsafe_content"),
}

# The unit that must be unique before scoring, matching pragmata's
# _DUPLICATE_KEY_COLUMNS_BY_TASK (core/eval/transforms.py). Retrieval fans one query
# out into one row per chunk; the other two are one row per query.
UNIT_KEYS: dict[str, tuple[str, ...]] = {
    "retrieval": ("record_uuid", "chunk_id"),
    "grounding": ("record_uuid",),
    "generation": ("record_uuid",),
}

# The default input tree: the frozen canonical export, not the live one the nightly
# cron overwrites. See reproducibility/2026-07-29-eval-report-freeze/.
FROZEN_EXPORTS = ws.DATA_DIR / "annotation" / "exports-frozen" / "2026-07-29"

# The bot output that was actually curated into Argilla, so it joins to the annotations.
CURATED_SUFFIX = "_combined.curated.jsonl"

# Excluded from every report output: the programme was seeded in Argilla (70 panels
# imported) but never staffed, so it has zero annotations. Recorded in each provenance
# sidecar.
EXCLUDED_PROGRAMMES = frozenset({"zentrum-fuer-datenmanagement"})


def add_common_args(parser, *, default_exports: Path | None = None) -> None:
    """Register the arguments every eval report script shares."""
    parser.add_argument(
        "--exports",
        type=Path,
        default=default_exports or FROZEN_EXPORTS,
        help="Annotation export tree to read (default: the frozen canonical export).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory (default: reports/eval/<today>/).",
    )


def out_dir(explicit: Path | None) -> Path:
    """Resolve the eval report output directory, creating it.

    Thin alias over ws.stage_report_dir so corpus_catalog.py, which cannot import this
    pandas-dependent module, still resolves the same directory from the same clock.
    """
    return ws.stage_report_dir("eval", explicit)


def programmes(exports: Path) -> list[str]:
    """Programme slugs present in an export tree, sorted, minus the excluded set.

    Read from the tree rather than configs/annotation/domains/ so a programme with an
    export but no config (or vice versa) surfaces as a mismatch instead of being
    silently dropped. EXCLUDED_PROGRAMMES is the one deliberate omission.
    """
    if not exports.is_dir():
        raise SystemExit(f"no such export tree: {exports}")
    return sorted(p.name for p in exports.iterdir() if p.is_dir() and p.name not in EXCLUDED_PROGRAMMES)


# Columns every filter and metric below depends on. Asserted once at read time so a
# rename upstream fails loudly here, instead of being absorbed by each filter's
# "column absent -> pass everything through" guard. That failure mode is the dangerous
# one: a missing `panel_complete` would silently score partial retrieval panels, which
# is the exact defect this pipeline exists to prevent.
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "retrieval": (
        "record_uuid", "annotator_id", "response_status", "calibration",
        "chunk_id", "chunk_rank", "n_retrieved_chunks", "panel_complete",
    ),
    "grounding": ("record_uuid", "annotator_id", "response_status", "calibration"),
    "generation": ("record_uuid", "annotator_id", "response_status", "calibration"),
}

# Columns the filters read as booleans, which must also be non-null. A blank cell makes
# pandas type the column `object`, and a bare `.astype(bool)` then maps NaN to TRUE,
# because bool(nan) is truthy - silently, with no error. The consequences invert the
# filters: a blank `calibration` marks a production query as calibration and drops it
# from the corpus, and a blank `panel_complete` lets an incomplete panel through the
# STRICT filter. Labels are deliberately NOT here: a discarded response legitimately
# has none.
NON_NULL_COLUMNS: dict[str, tuple[str, ...]] = {
    "retrieval": ("response_status", "calibration", "panel_complete", "n_retrieved_chunks"),
    "grounding": ("response_status", "calibration"),
    "generation": ("response_status", "calibration"),
}


def read_task(exports: Path, programme: str, task: str) -> pd.DataFrame:
    """Read one programme's task CSV, asserting the schema the filters rely on.

    Returns an empty frame when the file is absent or empty. That is a real state, not
    an error: zentrum-fuer-datenmanagement has imported records and zero annotations,
    and must still appear as an n=0 row.
    """
    path = exports / programme / f"{task}.csv"
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    frame = pd.read_csv(path)
    if frame.empty:
        return pd.DataFrame()
    missing = [c for c in REQUIRED_COLUMNS[task] + LABELS[task] if c not in frame.columns]
    if missing:
        raise SystemExit(
            f"{path} is missing column(s) the report filters require: {', '.join(missing)}.\n"
            f"The export schema has changed; fix the scripts rather than letting a filter "
            f"silently pass every row through."
        )
    nulls = {c: int(frame[c].isna().sum()) for c in NON_NULL_COLUMNS[task] if frame[c].isna().any()}
    if nulls:
        detail = ", ".join(f"{c} ({n} row(s))" for c, n in nulls.items())
        raise SystemExit(
            f"{path} has null values in column(s) the filters read as booleans: {detail}.\n"
            f"A blank would coerce to True and invert the filter - see NON_NULL_COLUMNS."
        )
    return frame


def has_subrows(task: str) -> bool:
    """Whether a task fans one query out into several rows.

    Retrieval does (one row per chunk); grounding and generation are one row per query.
    Derived from UNIT_KEYS so the structural fact lives in one place, rather than
    restating ``task == "retrieval"`` at every site that depends on it - panel
    completeness, chunks-per-query, and the calibration grain all turn on this.
    """
    return len(UNIT_KEYS[task]) > 1


def export_inputs(exports: Path, *, include_iaa: bool = True) -> list[Path]:
    """The export files a report derives from, for provenance hashing.

    One definition so the hashed input set is identical across reports and their
    sidecars stay comparable.
    """
    inputs = sorted(exports.rglob("*.csv"))
    if include_iaa:
        inputs += sorted(exports.rglob("iaa/report.json"))
    return inputs


def export_meta(exports: Path, programme: str) -> dict:
    """A programme's ``annotation_export.meta.json`` sidecar, or {} if absent."""
    path = exports / programme / "annotation_export.meta.json"
    return json.loads(path.read_text()) if path.exists() else {}


def panel_totals(exports: Path, programme: str) -> tuple[int, int]:
    """(n_panels, n_panels_complete) for a programme, from the export's own sidecar.

    Taken from ``completeness_summary`` rather than counted off the CSV rows, because
    the rows only cover panels that received at least one submitted response. Counting
    them undercounts the DENOMINATOR: zentrum-fuer-datenmanagement has 70 imported
    panels and zero annotations, so a row-derived count reports 0 panels and hides the
    programme's coverage gap entirely, and four other programmes undercount by 2-6.
    Use n_queries() for "panels that have responses"; use this for "panels that exist".
    """
    summary = export_meta(exports, programme).get("completeness_summary") or {}
    return int(summary.get("n_panels", 0)), int(summary.get("n_complete", 0))


def load_iaa(exports: Path, programme: str) -> dict[tuple[str, str], dict]:
    """(task, label) -> agreement stats from a programme's ``iaa/report.json``.

    Every entry describes CALIBRATION items only: pragmata's IAA runner keeps
    ``calibration == True`` submitted rows and drops production, because agreement is
    only meaningful on overlapped records. So an alpha attached to a production metric
    is evidence about the labelling scheme, not about those specific rows.
    """
    path = exports / programme / "iaa" / "report.json"
    if not path.exists():
        return {}
    report = json.loads(path.read_text())
    return {
        (block["task"], label["label"]): label
        for block in report.get("tasks", [])
        for label in block.get("labels", [])
    }


def submitted(frame: pd.DataFrame) -> pd.DataFrame:
    """Keep only submitted responses.

    Applied explicitly rather than trusting that today's exports happen to carry no
    discarded rows: exports run with include_discarded=true, a discarded response is
    an abstention with no labels, and a null label would silently poison a mean.
    """
    if frame.empty or "response_status" not in frame.columns:
        return frame
    return frame[frame["response_status"] == "submitted"]


def drop_calibration_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Keep production ROWS only. For prevalence, never for scoring — see below."""
    if frame.empty or "calibration" not in frame.columns:
        return frame
    return frame[~frame["calibration"].astype(bool)]


def keep_calibration_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Keep calibration ROWS only — the population every IAA alpha describes."""
    if frame.empty or "calibration" not in frame.columns:
        return frame.iloc[0:0]
    return frame[frame["calibration"].astype(bool)]


def drop_calibration_queries(frame: pd.DataFrame) -> pd.DataFrame:
    """Drop queries that are ENTIRELY calibration. The correct filter for scoring.

    ``calibration`` is a per-record flag, and at retrieval grain a record is one
    *chunk*, not one query — so calibration marks which chunks were given extra
    annotators for overlap. Panels are routinely mixed: every programme has 16-30
    retrieval panels holding both, e.g. a demokratie panel with K=7 where ranks 1-6
    are production and rank 7 is double-annotated calibration.

    Filtering calibration at ROW grain therefore deletes individual chunks out of
    otherwise-complete panels and silently breaks the @K denominators — a mixed panel
    would score precision over 6 of 7 chunks. Filtering at QUERY grain instead keeps
    every panel whole and only removes queries that are wholly calibration (0-13 per
    programme), which are the genuine calibration items.

    Grounding and generation carry one record per query, so their records are never
    mixed and this reduces to the plain row filter — one rule covers all three tasks.
    """
    if frame.empty or "calibration" not in frame.columns or "record_uuid" not in frame.columns:
        return frame
    calibration = frame["calibration"].astype(bool)
    all_calibration = calibration.groupby(frame["record_uuid"]).transform("all")
    return frame[~all_calibration]


def complete_panels(frame: pd.DataFrame) -> pd.DataFrame:
    """Keep retrieval rows whose panel has every chunk submitted (STRICT).

    `pragmata eval score` applies no completeness policy of its own — it groups by
    record_uuid and averages whatever chunks are present — so an incomplete panel
    yields a precision/nDCG/MRR computed over a partial chunk set. The export already
    carries the flag; this is the filter that consumes it.
    """
    if frame.empty or "panel_complete" not in frame.columns:
        return frame
    return frame[frame["panel_complete"].astype(bool)]


def consolidated_prevalence(frame: pd.DataFrame, task: str, label: str) -> tuple[int, int]:
    """(n_items, n_true) for one label, majority-consolidated per annotation unit.

    Follows pragmata's consolidate_labels_by_majority: a strict majority decides the
    unit's label; an exact tie falls back to the first row's value in file order. So
    n_items counts annotated units (not annotator responses), and n_true counts units
    whose consolidated label is true - the same numbers eval score would ingest.
    """
    if frame.empty or label not in frame.columns:
        return 0, 0
    keys = list(UNIT_KEYS[task])
    grouped = frame.groupby(keys, sort=False)[label].agg(["sum", "count", "first"])
    positive = grouped["sum"] * 2
    consolidated = (positive > grouped["count"]) | ((positive == grouped["count"]) & grouped["first"].astype(bool))
    return len(grouped), int(consolidated.sum())


def latest_snapshot(nth: int = 1) -> dict:
    """The Nth-from-last snapshot in logs/annotation/log.jsonl (1 = last).

    Asserts the snapshot carries the pooled gap statistics, which were added to log.py
    additively without a SNAPSHOT_SCHEMA_VERSION bump (the version guard is for
    incompatible changes, and bumping would strand every existing entry for
    report_tables.py). An older snapshot would otherwise sail through and emit blank
    cadence columns, reading as "no cadence data" rather than "wrong snapshot".
    """
    path = ws.LOGS_DIR / "log.jsonl"
    if not path.exists():
        raise SystemExit(f"no snapshot log at {path} - run `make log` first.")
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    if nth < 1:
        # lines[-0] is lines[0], the oldest snapshot - the opposite of "latest".
        raise SystemExit(f"--snapshot counts back from the last and must be >= 1, got {nth}.")
    if nth > len(lines):
        raise SystemExit(f"only {len(lines)} snapshots available, asked for {nth} from last.")
    snapshot = json.loads(lines[-nth])
    timing = (((snapshot.get("total") or {}).get("timing") or {}).get("per_annotator")) or {}
    if "pooled_mean_gap_s" not in timing:
        raise SystemExit(
            f"snapshot {snapshot.get('run_at')} predates the pooled gap statistics, so the "
            f"cadence columns would come out blank. Re-run `make log` and try again."
        )
    return snapshot


def pooled_agreement(snapshot: dict) -> dict[tuple[str, str], dict]:
    """(task, label) -> pooled agreement stats from a log snapshot.

    The pooled alpha puts every domain's calibration items into ONE reliability matrix
    per (task, label) - the matching population for metrics that are themselves pooled
    across programmes. Carries alpha, ci_lower/ci_upper, n_items, pct_agreement.
    """
    per_task = (snapshot.get("pooled_agreement") or {}).get("per_task") or {}
    return {
        (task, label): stats
        for task, labels in per_task.items()
        for label, stats in (labels.get("per_label") or {}).items()
    }


def n_queries(frame: pd.DataFrame) -> int:
    """Distinct queries (record_uuid) — the unit every corpus metric averages over."""
    if frame.empty or "record_uuid" not in frame.columns:
        return 0
    return int(frame["record_uuid"].nunique())


def tied_label_units(frame: pd.DataFrame, task: str) -> tuple[int, int]:
    """(units with >=1 tied label, units with >1 annotator) for a task frame.

    pragmata consolidates repeated annotator rows by per-label majority, but sets
    `majority_threshold = len(group) / 2` and excludes exact ties from the strict
    majority — so a 1-of-2 split keeps whichever row was selected, i.e. the outcome
    depends on CSV row order. This quantifies how much of the data that touches.
    """
    if frame.empty:
        return 0, 0
    keys = [k for k in UNIT_KEYS[task] if k in frame.columns]
    labels = [c for c in LABELS[task] if c in frame.columns]
    if not keys or not labels:
        return 0, 0
    grouped = frame.groupby(list(keys), sort=False)
    n_tied = n_multi = 0
    for _, group in grouped:
        if len(group) < 2:
            continue
        n_multi += 1
        positives = group[labels].astype(float).sum(axis=0)
        if (positives == len(group) / 2).any():
            n_tied += 1
    return n_tied, n_multi
