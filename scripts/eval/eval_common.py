"""Shared vocabulary for the eval-stage report scripts.

The executable half of ``docs/eval-data-dictionary.md``, which defines response / record /
item / panel / query group and every column of every CSV in prose. The things that decide
a number — which rows count, what an item is, which population a statistic describes — are
defined once here rather than re-derived per script. Keep the two in step.

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
import os
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import workspace as ws

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
    "generation": (
        "proper_action",
        "response_on_topic",
        "helpful",
        "incomplete",
        "unsafe_content",
    ),
}

# The columns identifying one ITEM — one record's responses majority-consolidated, the
# grain eval ingests. Matches pragmata's _DUPLICATE_KEY_COLUMNS_BY_TASK
# (core/eval/transforms.py). A retrieval record is one chunk, so a query's panel fans out
# into one row per chunk; a grounding or generation record is one query.
ITEM_KEYS: dict[str, tuple[str, ...]] = {
    "retrieval": ("record_uuid", "chunk_id"),
    "grounding": ("record_uuid",),
    "generation": ("record_uuid",),
}

# --- the canonical freeze: one date and one snapshot behind every report number ------
#
# The pin is data, not code: it lives in configs/eval/freeze.conf so `make annotation-freeze`
# can write it, leaving only the commit to the operator. Read here rather than in each
# script so a refresh moves it once. The snapshot is pinned by timestamp, not taken as
# "the latest": the nightly cron appends one every night, so a report re-run months later
# must still read the line it was built from.
FREEZE_CONF = ws.ROOT / "configs" / "eval" / "freeze.conf"


def _freeze_pin(key: str) -> str:
    """One key from configs/eval/freeze.conf, or exit saying what is missing.

    Loaded into the environment rather than parsed here, reusing the loader that already
    reads settings.conf and .env - which also means an env var of the same name wins, the
    escape hatch a scratch run needs.
    """
    if not FREEZE_CONF.exists():
        raise SystemExit(
            f"missing {FREEZE_CONF.relative_to(ws.ROOT)} - it pins the canonical freeze.\n"
            "  Cut one with `make annotation-freeze`, or restore\n"
            "  the file from git. See docs/eval.md."
        )
    value = os.environ.get(key, "").strip()
    if not value:
        raise SystemExit(
            f"{key} is unset or empty in {FREEZE_CONF.relative_to(ws.ROOT)}.\n"
            "  Both FREEZE_DATE and CANONICAL_SNAPSHOT_RUN_AT are required."
        )
    return value


ws.load_dotenv(FREEZE_CONF)  # existing env wins, as with settings.conf
FREEZE_DATE = _freeze_pin("FREEZE_DATE")
CANONICAL_SNAPSHOT_RUN_AT = _freeze_pin("CANONICAL_SNAPSHOT_RUN_AT")
FROZEN_EXPORTS = ws.DATA_DIR / "annotation" / "exports-frozen" / FREEZE_DATE

# The bot output that was actually curated into Argilla, so it joins to the annotations.
CURATED_SUFFIX = "_combined.curated.jsonl"

# Excluded from every report output, decided 2026-07-30: the programme was seeded in
# Argilla (70 panels imported) but never staffed, so it has zero annotations in every
# task. It is omitted rather than carried as an n=0 row, because an all-blank row in a
# report table reads as a measurement rather than as an absence. The gap is recorded in
# the reproducibility bundle and in the data dictionary instead.
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


def add_snapshot_arg(parser) -> None:
    """Register --snapshot-run-at for the scripts that read a log snapshot."""
    parser.add_argument(
        "--snapshot-run-at",
        default=CANONICAL_SNAPSHOT_RUN_AT,
        help="run_at of the log snapshot to read (default: the canonical one).",
    )


def out_dir(explicit: Path | None) -> Path:
    """Resolve the eval report output directory, creating it.

    Thin alias over ws.stage_report_dir so corpus_catalog.py, which cannot import this
    pandas-dependent module, still resolves the same directory from the same clock.
    """
    return ws.stage_report_dir("eval", explicit)


FREEZE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def newest_freeze() -> str | None:
    """The newest dated dir under exports-frozen/, or None if there is none.

    The names are ISO dates, so lexical order is chronological. Anything else there is
    ignored: annotation-freeze refuses such a name, and a stray directory must not be able
    to make a current pin look stale.
    """
    root = FROZEN_EXPORTS.parent
    if not root.is_dir():
        return None
    dated = sorted(
        p.name for p in root.iterdir() if p.is_dir() and FREEZE_DIR_RE.match(p.name)
    )
    return dated[-1] if dated else None


def check_freeze_current() -> None:
    """Refuse to read the canonical freeze while a newer one sits on disk.

    A MISSING freeze already fails loudly (`no such export tree` below). A STALE pin does
    not: cut a new freeze, leave the pin on the old one, and every report silently
    publishes the previous dataset. It stays auditable afterwards - each
    .provenance.json hashes its inputs - but nothing stopped it at the time.

    `make annotation-freeze` writes the pin itself, so this catches what the target
    cannot: a hand-edited freeze.conf, a freeze cut without the target, and - most likely
    - a freeze.conf written but never committed, so another checkout still resolves the
    old date.
    """
    newest = newest_freeze()
    if newest is None or newest == FREEZE_DATE:
        return
    raise SystemExit(
        f"stale freeze pin: {FREEZE_CONF.relative_to(ws.ROOT)} names {FREEZE_DATE}, but "
        f"{newest} is the newest freeze on disk.\n"
        "  A report built from the older tree would publish the previous dataset. Either\n"
        "  move the pin (`make annotation-freeze` writes it - and commit it), or pass\n"
        "  --exports explicitly to read a non-canonical tree on purpose."
    )


def programmes(exports: Path) -> list[str]:
    """Programme slugs present in an export tree, sorted, minus the excluded set.

    Read from the tree rather than configs/annotation/domains/ so a programme with an
    export but no config (or vice versa) surfaces as a mismatch instead of being
    silently dropped. EXCLUDED_PROGRAMMES is the one deliberate omission.

    Also where the freeze pin is checked for staleness, because all three report scripts
    pass through here. Gated on the tree actually BEING the canonical freeze rather than
    on --exports having been omitted: the same bytes must behave the same way however the
    path was spelled, so no wrapper can disable the guard by passing the default.
    """
    if not exports.is_dir():
        raise SystemExit(f"no such export tree: {exports}")
    if exports.resolve() == FROZEN_EXPORTS.resolve():
        check_freeze_current()
    return sorted(
        p.name
        for p in exports.iterdir()
        if p.is_dir() and p.name not in EXCLUDED_PROGRAMMES
    )


# Columns every filter and metric below depends on. Asserted once at read time so a
# rename upstream fails loudly here, instead of being absorbed by each filter's
# "column absent -> pass everything through" guard. That failure mode is the dangerous
# one: a missing `panel_complete` would silently score partial retrieval panels, which
# is the exact defect this pipeline exists to prevent.
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "retrieval": (
        "record_uuid",
        "annotator_id",
        "response_status",
        "calibration",
        "chunk_id",
        "chunk_rank",
        "n_retrieved_chunks",
        "panel_complete",
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
    "retrieval": (
        "response_status",
        "calibration",
        "panel_complete",
        "n_retrieved_chunks",
    ),
    "grounding": ("response_status", "calibration"),
    "generation": ("response_status", "calibration"),
}


def read_task(exports: Path, programme: str, task: str) -> pd.DataFrame:
    """Read one programme's task CSV, asserting the schema the filters rely on.

    Returns an empty frame when the file is absent or empty. That is a real state rather
    than an error: a task can be imported and never annotated, and the callers count it
    as zero instead of failing.
    """
    path = exports / programme / f"{task}.csv"
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    frame = pd.read_csv(path)
    if frame.empty:
        return pd.DataFrame()
    missing = [
        c for c in REQUIRED_COLUMNS[task] + LABELS[task] if c not in frame.columns
    ]
    if missing:
        raise SystemExit(
            f"{path} is missing column(s) the report filters require: {', '.join(missing)}.\n"
            f"The export schema has changed; fix the scripts rather than letting a filter "
            f"silently pass every row through."
        )
    nulls = {
        c: int(frame[c].isna().sum())
        for c in NON_NULL_COLUMNS[task]
        if frame[c].isna().any()
    }
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
    Derived from ITEM_KEYS so the structural fact lives in one place, rather than
    restating ``task == "retrieval"`` at every site that depends on it - panel
    completeness, chunks-per-query, and the calibration grain all turn on this.
    """
    return len(ITEM_KEYS[task]) > 1


def export_inputs(exports: Path, *, include_iaa: bool = True) -> list[Path]:
    """The export files a report derives from, for provenance hashing.

    One definition so the hashed input set is identical across reports and their
    provenance records stay comparable.
    """
    inputs = sorted(exports.rglob("*.csv"))
    if include_iaa:
        inputs += sorted(exports.rglob("iaa/report.json"))
    return inputs


def export_meta(exports: Path, programme: str) -> dict:
    """A programme's ``annotation_export.meta.json``, or {} if absent."""
    path = exports / programme / "annotation_export.meta.json"
    return json.loads(path.read_text()) if path.exists() else {}


def panel_totals(exports: Path, programme: str) -> tuple[int, int]:
    """(n_panels, n_panels_complete) for a programme, from the export's own meta file.

    Taken from ``completeness_summary`` rather than counted off the CSV rows, because
    the rows only cover panels that received at least one submitted response. Counting
    them undercounts the DENOMINATOR — several programmes have panels nobody opened — so
    a row-derived count would hide exactly the coverage gap these columns exist to show.
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
    if (
        frame.empty
        or "calibration" not in frame.columns
        or "record_uuid" not in frame.columns
    ):
        return frame
    calibration = frame["calibration"].astype(bool)
    all_calibration = calibration.groupby(frame["record_uuid"]).transform("all")
    return frame[~all_calibration]


def consolidated_prevalence(
    frame: pd.DataFrame, task: str, label: str
) -> tuple[int, int]:
    """(n_items, n_true) for one label, majority-consolidated per item.

    Follows pragmata's consolidate_labels_by_majority: a strict majority decides the
    item's label; an exact tie falls back to the first row's value in file order. So the
    count is of annotated items, not of responses, and n_true counts items whose
    consolidated label is true - the same numbers eval score would ingest. Named
    `n_items` in the label table and `n_items_annotated` in the operations table (the
    same count at the same grain); see the data dictionary.
    """
    if frame.empty or label not in frame.columns:
        return 0, 0
    keys = list(ITEM_KEYS[task])
    grouped = frame.groupby(keys, sort=False)[label].agg(["sum", "count", "first"])
    positive = grouped["sum"] * 2
    consolidated = (positive > grouped["count"]) | (
        (positive == grouped["count"]) & grouped["first"].astype(bool)
    )
    return len(grouped), int(consolidated.sum())


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
