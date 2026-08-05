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
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import workspace as ws

ws.load_env()  # configs/settings.conf + .env; existing env wins

TASKS = ("retrieval", "grounding", "generation")

# pragmata's own eval tool tree. Named here rather than per script because three stages now
# read it - training writes train_outputs/, prediction writes prediction_outputs/, scoring
# writes scores/ - and the ownership rule in docs/eval.md is that this tree holds only what
# pragmata put there. Workspace-produced inputs live under data/eval-inputs/.
EVAL_TOOL_ROOT = ws.DATA_DIR / "eval"
TRAIN_OUTPUTS = EVAL_TOOL_ROOT / "train_outputs"
PREDICTION_OUTPUTS = EVAL_TOOL_ROOT / "prediction_outputs"

# The (text, text_pair) column pair per task, mirroring pragmata's TEXT_COLUMNS_BY_TASK
# (core/schemas/eval_input.py). Duplicated for the same reason LABELS is: the staging
# subcommands build CSVs from exports and JSONL and must not need pragmata just to name
# columns. tlmtc sees these renamed to text/text_pair, which is why a prediction CSV read
# back through `eval score --prediction-id` has to be un-renamed again (pragmata's
# _restore_pragmata_text_columns does it).
TEXT_COLUMNS: dict[str, tuple[str, str]] = {
    "retrieval": ("query", "chunk"),
    "grounding": ("answer", "context_set"),
    "generation": ("query", "answer"),
}

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
# (core/eval/transforms.py). `record_uuid` is the QUERY (for retrieval, its whole panel),
# so a retrieval item is one (record_uuid, chunk_id) pair and a query's panel fans out
# into one row per chunk; a grounding or generation item is one query.
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


def add_common_args(parser) -> None:
    """Register the arguments every eval report script shares."""
    parser.add_argument(
        "--exports",
        type=Path,
        default=FROZEN_EXPORTS,
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
    if newest == FREEZE_DATE:
        return
    raise SystemExit(
        f"stale freeze pin: {FREEZE_CONF.relative_to(ws.ROOT)} names {FREEZE_DATE}, but "
        f"{newest} is the newest freeze on disk.\n"
        "  A report built from the older tree would publish the previous dataset. Either\n"
        "  move the pin (`make annotation-freeze` writes it - and commit it), or pass\n"
        "  --exports explicitly to read a non-canonical tree on purpose."
    )


def resolve_exports(exports: Path) -> Path:
    """The export tree to read, allowing for the GPU box holding it somewhere else.

    `transfer-pull` writes only under data/transfer/ - it refuses any destination that would
    escape it - so on the GPU host the canonical freeze arrives at
    data/transfer/exports-frozen/<date>/, not at the data/annotation/exports-frozen/<date>/
    that this module defaults to. Both are the same freeze: the date comes from the committed
    pin either way, so there is no question of picking up different data, only of where it
    physically sits.

    Falls back rather than guessing: an explicit --exports is honoured untouched, and the
    fallback says which tree it settled on, because "which bytes was this built from" is the
    one thing a training or prediction input must not leave implicit.
    """
    if exports.is_dir():
        return exports
    # Only the DEFAULT falls back. An explicitly named tree that is absent is an error: the
    # caller asked for particular bytes, and quietly using different ones instead is worse
    # than failing.
    if exports.resolve() != FROZEN_EXPORTS.resolve():
        raise SystemExit(f"no export tree at {exports}")
    staged = ws.DATA_DIR / "transfer" / "exports-frozen" / FREEZE_DATE
    if staged.is_dir():
        # --exports may be relative, so relative_to alone would raise: same guard as
        # ws.provenance uses on its hashed inputs.
        shown = exports.resolve()
        shown = shown.relative_to(ws.ROOT) if shown.is_relative_to(ws.ROOT) else shown
        print(
            f"note: {shown} is absent; using the pulled tree at "
            f"{staged.relative_to(ws.ROOT)}",
            file=sys.stderr,
        )
        return staged
    raise SystemExit(
        f"no export tree at {exports}.\n"
        f"  Nor a pulled copy at {staged}.\n"
        f"  On the GPU host: make transfer-pull PREFIX=exports-frozen/{FREEZE_DATE}"
    )


def resolve_corpus_dir(explicit: Path | None = None) -> Path:
    """The directory holding the curated per-programme corpus JSONL, wherever this box has it.

    The same two-place rule ``resolve_exports`` applies to the export tree, for the same
    reason: the curated corpus is produced on the CPU box under ``data/publikationsbot/`` and
    reaches the GPU box through the Blob, where ``transfer-pull`` can only land it at
    ``data/transfer/publikationsbot/``. An explicit path is honoured untouched.
    """
    if explicit is not None:
        if not explicit.is_dir():
            raise SystemExit(f"no corpus directory at {explicit}")
        return explicit
    if any(ws.OUT_DIR.glob(f"*{CURATED_SUFFIX}")):
        return ws.OUT_DIR
    staged = ws.DATA_DIR / "transfer" / "publikationsbot"
    if any(staged.glob(f"*{CURATED_SUFFIX}")):
        print(
            f"note: no *{CURATED_SUFFIX} under {ws.OUT_DIR.relative_to(ws.ROOT)}; using the "
            f"pulled copy at {staged.relative_to(ws.ROOT)}",
            file=sys.stderr,
        )
        return staged
    raise SystemExit(
        f"no *{CURATED_SUFFIX} under {ws.OUT_DIR.relative_to(ws.ROOT)}.\n"
        f"  Nor a pulled copy at {staged.relative_to(ws.ROOT)}.\n"
        "  The curated corpus is produced on the CPU box; on the GPU host pull it first:\n"
        "    make transfer-pull PREFIX=publikationsbot"
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
# rename upstream fails loudly here rather than one filter at a time. The dangerous one
# is `n_retrieved_chunks`: it carries the query's true K, and it is what pragmata's
# --skip-incomplete-panels compares the distinct labelled chunk_ids against, so losing it
# would silently score partial retrieval panels — the exact defect this pipeline exists
# to prevent. (`panel_complete` is deliberately NOT here: the export writes the flag, but
# nothing downstream reads it — pragmata derives completeness itself.)
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "retrieval": (
        "record_uuid",
        "annotator_id",
        "response_status",
        "calibration",
        "chunk_id",
        "chunk_rank",
        "n_retrieved_chunks",
    ),
    "grounding": ("record_uuid", "annotator_id", "response_status", "calibration"),
    "generation": ("record_uuid", "annotator_id", "response_status", "calibration"),
}

# Columns the filters read as booleans or counts, which must also be non-null. A blank
# cell makes pandas type the column `object`, and a bare `.astype(bool)` then maps NaN to
# TRUE, because bool(nan) is truthy - silently, with no error. The consequence inverts
# the filter: a blank `calibration` marks a production query as calibration and drops it
# from the corpus. A blank `n_retrieved_chunks` is the same shape of problem one layer
# down - pragmata cannot compare a panel's labelled chunks against an unknown K, so the
# panel escapes the completeness check. Labels are deliberately NOT here: a discarded
# response legitimately has none.
NON_NULL_COLUMNS: dict[str, tuple[str, ...]] = {
    "retrieval": (
        "response_status",
        "calibration",
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


def submitted(frame: pd.DataFrame) -> pd.DataFrame:
    """Keep only submitted responses.

    Applied explicitly rather than trusting that today's exports happen to carry no
    discarded rows: exports run with include_discarded=true, a discarded response is
    an abstention with no labels, and a null label would silently poison a mean.
    """
    if frame.empty:
        return frame
    return frame[frame["response_status"] == "submitted"]


def keep_calibration_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Keep calibration ROWS only — the population every IAA alpha describes."""
    if frame.empty:
        return frame.iloc[0:0]
    return frame[frame["calibration"].astype(bool)]


def drop_calibration_queries(frame: pd.DataFrame) -> pd.DataFrame:
    """Drop queries that are ENTIRELY calibration. The correct filter for scoring.

    ``calibration`` is a per-record flag, and a retrieval record is one *chunk* of a
    query's panel — ``record_uuid`` names the query, ``chunk_id`` the chunk within it —
    so calibration marks which chunks were given extra annotators for overlap. Panels
    are routinely mixed: every programme has 16-30 retrieval panels holding both, e.g. a
    demokratie panel with K=7 where ranks 1-6 are production and rank 7 is
    double-annotated calibration.

    Filtering calibration at ROW grain therefore deletes individual chunks out of
    otherwise-complete panels and silently breaks the @K denominators — a mixed panel
    would score precision over 6 of 7 chunks. Grouping on ``record_uuid`` instead keeps
    every panel whole and only removes queries that are wholly calibration (0-13 per
    programme), which are the genuine calibration items.

    Grounding and generation carry one record per query, so their records are never
    mixed and this reduces to the plain row filter — one rule covers all three tasks.
    """
    if frame.empty:
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
    if frame.empty:
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


# --- the pragmata eval side: which module answers, which pin, which GPU, which run -------
#
# Shared by the three model-stage scripts (train_evaluators, predict_evaluators,
# score_synthetic_predictions) plus evaluator_report. They live here rather than in one of
# them because all four have to make the same choice the same way, and the choice is about
# the environment rather than about a task's data.


def pragmata_eval():
    """The pragmata eval API, from wherever this environment has it.

    Two environments reach this, and they supply eval differently:

    - The CPU VM has ONE venv running both stages, where the installed pragmata is the
      annotation pin - a frozen commit with no eval module at all. Eval therefore comes from
      the PRAGMATA_EVAL_SRC checkout, shadowed onto sys.path; the in-process equivalent of the
      PYTHONPATH score_human_annotations.py hands its subprocess.
    - The GPU host trains and predicts inside a container against its own venv, where
      pragmata[eval] is installed outright from configs/eval/train-requirements.txt. There is
      only one pragmata there, so there is nothing to shadow, and PRAGMATA_EVAL_SRC generally
      does not even exist inside the container - only the workspace is mounted.

    So try the plain import first and shadow only if it has no eval module. Which one answered
    is printed rather than inferred, and returned as well, so a run's own provenance record can
    pin it: silently training or predicting against a different commit than the one a run claims
    is the failure this ordering could otherwise hide.

    Callers import it through here rather than at module scope so `--help` and the staging
    subcommands cost nothing.
    """
    try:
        import pragmata.api.eval as eval_api
        from pragmata.core.schemas.annotation_task import Task
    except ImportError:
        # The failed attempt left the ANNOTATION pin's `pragmata` package cached in
        # sys.modules. Adding the eval src to sys.path cannot dislodge it - the retry would
        # resolve against the stale package object and fail identically - so drop the
        # partially-imported tree first.
        for name in [
            n for n in sys.modules if n == "pragmata" or n.startswith("pragmata.")
        ]:
            del sys.modules[name]
        pin = ws.eval_pragmata()
        src = str(pin.src)
        if sys.path[0] != src:
            sys.path.insert(0, src)
        try:
            import pragmata.api.eval as eval_api
            from pragmata.core.schemas.annotation_task import Task
        except ImportError as exc:
            raise SystemExit(
                f"cannot import the eval API from {pin.src}: {exc}\n"
                "  On the GPU host, install pragmata[eval] from\n"
                "  configs/eval/train-requirements.txt instead - see docs/eval-training.md."
            ) from exc

    package_dir = Path(eval_api.__file__).parent.parent
    print(f"pragmata eval API: {package_dir}", file=sys.stderr)
    # Its parent is <checkout>/src when shadowed and site-packages when installed - the same
    # value score_human_annotations.py records from the pin, and the shape ws.provenance's
    # `pragmata_src` expects, which asks git which checkout that tree belongs to. The
    # installed case belongs to none (the workspace's own repo does not count), so the sha
    # comes out null rather than borrowed from elsewhere.
    return eval_api, Task, package_dir.parent


def require_gpu(*, use_cpu: bool = False) -> None:
    """Refuse to start a run whose interpreter cannot see a GPU.

    Checked up front rather than left to fail later, because every way of getting this wrong
    surfaces a long way from its cause:

    - Inside the container the `make` default is `.venv/bin/python`, which is the HOST's venv
      on the mounted workspace. Its torch is a cu130 build the driver cannot use, so
      is_available() is False even though nvidia-smi shows four A100s. Pass
      PY=<training venv>/bin/python.
    - On the host itself, GPU compute is disabled by site policy (CUDA_VISIBLE_DEVICES is
      blanked), so no interpreter there can train or predict regardless of its torch.

    Left to itself, the first symptom is a CUDA error raised inside tlmtc - for training,
    potentially after a long tokenisation pass. ``use_cpu`` is honoured as a deliberate escape
    hatch: nothing in configs/eval/training/ sets it, and prediction takes it from a flag.
    """
    if use_cpu:
        return
    import torch

    if torch.cuda.is_available():
        return
    raise SystemExit(
        f"this interpreter cannot see a GPU: torch {torch.__version__}, "
        f"cuda.is_available() False.\n"
        f"  CUDA_VISIBLE_DEVICES={os.environ.get('CUDA_VISIBLE_DEVICES', '<unset>')!r}\n"
        "  Inside the training container, point make at the training venv:\n"
        "    make eval-train TASK=<task> PY=~/train-venv/bin/python\n"
        "  On the GPU host outside a container, compute is disabled by site policy - launch a\n"
        "  container first. See docs/eval-training.md."
    )


def require_clean_eval_pin(pin, *, allow_dirty: bool) -> dict:
    """git_describe() of the eval pragmata pin, refusing one that pins nothing.

    No SHA is worse than a dirty one: a dirty tree at least names the commit it drifted
    from, whereas a pin git cannot describe at all pins nothing. Both refuse here, and
    ``allow_dirty`` is the one deliberate way past either.

    Shared by the two scoring scripts, which publish numbers that have to be re-derivable
    from a commit of pragmata as well as of this workspace.
    """
    described = ws.git_describe(pin.repo)
    if described["sha"] is None and not allow_dirty:
        raise SystemExit(
            f"the pragmata pin at {pin.src} is not inside a git checkout of its own - the\n"
            f"numbers could not be reproduced from any SHA. Point PRAGMATA_EVAL_SRC at a\n"
            f"checkout's src/, or pass --allow-dirty to score anyway."
        )
    if described["dirty"] and not allow_dirty:
        raise SystemExit(
            f"pragmata pin at {pin.repo} has uncommitted changes - the numbers would not be\n"
            f"reproducible from its SHA. Commit/stash there, or pass --allow-dirty."
        )
    return described


def run_score_cli(
    pin, input_args: list[str], task: str, score_id: str, args, context: str
) -> Path:
    """Invoke `pragmata eval score` and return its report JSON path, freshness-checked.

    Shared by both scoring scripts - the human one passes ``["--path", <csv>]``, the
    synthetic one ``["--prediction-id", <id>]`` - so the load-bearing parts live exactly
    once: the PYTHONPATH shadow (the workspace venv's CLI with the eval pin's src
    shadowing the installed annotation pragmata, a frozen demo commit with no eval
    module), the base_dir choice (pragmata's tool-root PARENT, so data/, matching the
    existing data/annotation/ tree), and the mtime guard.

    The guard: the report path below is reconstructed from pragmata's output layout
    rather than reported by the CLI, and score_id is a slug reused run after run. If
    that layout ever moves while the CLI still exits 0, the guessed path would resolve
    to the PREVIOUS run's file and its stale numbers would be published under this
    run's provenance. A file older than the subprocess cannot be its output, so the
    clock is read before the run and the report must postdate it.
    """
    import subprocess
    import time

    env = dict(os.environ)
    env["PYTHONPATH"] = str(pin.src)
    command = [
        str(pin.bin),
        "eval",
        "score",
        *input_args,
        "--task",
        task,
        "--base-dir",
        str(ws.DATA_DIR),
        "--score-id",
        score_id,
        "--ci",
        str(args.ci),
        "--n-resamples",
        str(args.n_resamples),
        "--seed",
        str(args.seed),
        # Panel completeness is pragmata's job (#305): skip records n_panels_skipped
        # on the report; --all-panels accepts the bias instead.
        "--allow-incomplete-panels" if args.all_panels else "--skip-incomplete-panels",
    ]
    started = time.time()
    # check=False: the returncode is handled explicitly, to surface the CLI's own
    # error tail rather than a bare CalledProcessError.
    result = subprocess.run(
        command, env=env, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-6:]
        raise SystemExit(f"eval score failed for {context}:\n  " + "\n  ".join(tail))
    report_path = ws.DATA_DIR / "eval" / "scores" / score_id / f"{task}_scores.json"
    if not report_path.exists() or report_path.stat().st_mtime < started:
        why = "no such file" if not report_path.exists() else "it predates the run"
        raise SystemExit(
            f"eval score reported success for {context}, but the report it should\n"
            f"  have written was not written by it ({why}):\n"
            f"    {report_path}\n"
            "  That path is reconstructed from pragmata's output layout, not reported by\n"
            "  the CLI. If the layout has moved, fix run_score_cli() rather than\n"
            "  publishing whatever file happens to sit there."
        )
    return report_path


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _evaluator_run(run_id: str, meta: dict) -> SimpleNamespace:
    """One evaluator run's identity, from BOTH sidecars a completed run leaves behind.

    The two say different things and callers need both. pragmata's
    ``pragmata_train.meta.json`` carries run_id, task and a timestamp - which is what run
    selection turns on. tlmtc's ``train_run_meta.json`` carries the model side, of which
    ``label_names`` is the load-bearing field downstream: grounding trains on three of its five
    labels, so what a prediction can possibly contain (and therefore whether it is scoreable at
    all) is decided there rather than by the task. Absent rather than empty when the file is
    missing, so a caller can tell "no labels" from "cannot say".
    """
    run_dir = TRAIN_OUTPUTS / run_id
    tlmtc_meta_path = run_dir / "train_run_meta.json"
    tlmtc_meta = _json(tlmtc_meta_path) if tlmtc_meta_path.is_file() else {}
    return SimpleNamespace(
        run_id=run_id,
        run_dir=run_dir,
        meta=meta,
        tlmtc_meta=tlmtc_meta,
        label_names=tlmtc_meta.get("label_names"),
    )


def resolve_evaluator_run(task: str, run_id: str | None = None) -> SimpleNamespace:
    """The evaluator training run to use for a task: run_id, run_dir, both metas, label_names.

    Reads ``pragmata_train.meta.json`` directly rather than through pragmata, so the
    CPU-only consumers (evaluator_report's metrics table) need no eval module at all. It is
    the same file and the same rule pragmata's ``resolve_eval_train_run_id`` applies - latest
    by ``created_at`` with the run id as a deterministic tie-break, and an explicit run id
    checked against the task it was trained for. ``predict_labels`` re-resolves it anyway, so
    this is a pre-flight rather than a substitute: it lets a run print and record which
    evaluator it settled on BEFORE loading a model.
    """
    if run_id is not None:
        meta_path = TRAIN_OUTPUTS / run_id / "pragmata_train.meta.json"
        if not meta_path.is_file():
            raise SystemExit(
                f"no evaluator run {run_id!r}: {meta_path} does not exist.\n"
                "  Trained runs live under data/eval/train_outputs/<run_id>/; on the CPU box\n"
                "  pull them first (make transfer-pull PREFIX=checkpoints)."
            )
        meta = _json(meta_path)
        if meta.get("task") != task:
            raise SystemExit(
                f"evaluator {run_id!r} was trained for task={meta.get('task')!r}, not {task!r}."
            )
        return _evaluator_run(run_id, meta)

    candidates = []
    for meta_path in sorted(TRAIN_OUTPUTS.glob("*/pragmata_train.meta.json")):
        meta = _json(meta_path)
        if meta.get("task") == task:
            candidates.append((meta.get("created_at", ""), meta_path.parent.name, meta))
    if not candidates:
        raise SystemExit(
            f"no trained evaluator for task={task!r} under "
            f"{TRAIN_OUTPUTS.relative_to(ws.ROOT)}.\n"
            "  Train one (make eval-train TASK=<task>) or pull the checkpoints."
        )
    _created_at, resolved, meta = max(candidates)
    return _evaluator_run(resolved, meta)
