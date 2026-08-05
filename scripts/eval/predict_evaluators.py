#!/usr/bin/env python3
"""Apply the trained evaluators to unlabelled data - the prediction stage.

The workspace side of `pragmata eval predict-labels`. Stages the unlabelled per-task CSVs,
then runs one evaluator over one population and files its output under a name that says which
population it describes. Inference needs the same environment training does, so `predict` runs
on the GPU box; the staging subcommand is CPU-only. See
[Eval prediction](../../docs/eval-prediction.md).

Two populations answer the published questions, and a third is internal:

- `annotated` - the same rows the human-label metrics were scored on, pooled from the frozen
  canonical export with the labels stripped. Predicting these is what makes evaluator output
  comparable against a human baseline at all.
- `corpus` - the curated corpus, most of which nobody annotated. Predicting these gives
  corpus-scale prevalence estimates, with no baseline to check them against.
- `testsplit` - a run's own held-out split, staged by `evaluator_report.py calibration` rather
  than by `predict-inputs`, and re-predicted only for the per-item probabilities the
  reliability curves need. A manual re-run must name the run the split was staged from.

Prediction has no YAML configs, deliberately: population and evaluator run id are CLI
arguments, because until the final run there are no published numbers for a pin to stand
behind. See configs/eval/README.md.

Usage:
  scripts/eval/predict_evaluators.py predict-inputs --population annotated
  scripts/eval/predict_evaluators.py predict retrieval --population annotated --evaluator-run-id ID
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import eval_common as ec
import workspace as ws

# Staged unlabelled CSVs handed to predict_labels, one directory per population. Under
# data/eval-inputs/ and not data/eval/ for the reason score_human_annotations.py gives:
# data/eval/ is pragmata's tool tree and holds only what pragmata wrote there. The
# per-population leaf keeps the two apart the way the scorer's per-policy dirs do - the
# populations are pooled under different rules and must not collide.
PREDICT_INPUTS = ws.DATA_DIR / "eval-inputs" / "predict"

# Populations `predict-inputs` builds. `testsplit` is a third one that `predict` accepts but
# this subcommand does not build: evaluator_report.py stages it per run from that run's own
# held-out split, which is a property of a training run rather than of the corpus.
STAGED_POPULATIONS = ("annotated", "corpus")
PREDICT_POPULATIONS = (*STAGED_POPULATIONS, "testsplit")

# See train_evaluators.py's own note: set here rather than left to the Makefile so a direct
# run lands where a `make` run does, and at import time because transformers reads it when IT
# is imported.
os.environ.setdefault("HF_HOME", str(ws.ROOT / ".hf"))

# The identity columns each task's rows must carry through prediction, taken from pragmata's
# *_SCORE_SCHEMA (_identity_column_schemas in core/schemas/eval_input.py) rather than chosen
# here. They are the reason staging is not simply "the text columns": predict validates
# against the PREDICT schema, which needs none of them, but `eval score --prediction-id`
# reads predictions.csv against the SCORE schema, which requires record_uuid for every task
# and chunk_id plus chunk_rank for retrieval. Extra columns pass through predict untouched
# (both pragmata's and tlmtc's predict contracts are strict=False, and tlmtc's
# make_prediction_frame concatenates the input frame back onto its output), so anything
# staged here reaches the scorer.
#
# n_retrieved_chunks is not in that schema and is kept anyway: it carries the query's true K,
# and it is what --skip-incomplete-panels compares the labelled chunk count against. Without
# it pragmata cannot verify panel completeness and says so in a warning rather than failing,
# which is the quiet version of the defect this pipeline exists to prevent.
IDENTITY_COLUMNS: dict[str, tuple[str, ...]] = {
    "retrieval": ("record_uuid", "chunk_id", "chunk_rank", "n_retrieved_chunks"),
    "grounding": ("record_uuid",),
    "generation": ("record_uuid",),
}

# Corpus-only identity, carried because the corpus side has identifiers the export does not.
# `query_id` is the querygen spec's own id - the join key retrieval_manifest.csv and
# corpus_catalog.csv are keyed on, and the one thing the annotation exports lack (see the
# data dictionary's note on joining). `doc_id` is per chunk, so retrieval only.
CORPUS_IDENTITY_COLUMNS: dict[str, tuple[str, ...]] = {
    "retrieval": ("query_id", "doc_id"),
    "grounding": ("query_id",),
    "generation": ("query_id",),
}

# The curation bundle whose pins the corpus JSONL is checked against. A match means the
# prediction population is the same corpus the annotations were drawn from; a mismatch means
# it is not, which is a fact about the numbers rather than an error. Either way it is
# recorded - see _corpus_pin_records.
CURATION_PINS = (
    ws.ROOT / "reproducibility" / "2026-07-01-annotation-curation" / "pins.sha256"
)

# The QueryResponsePair fields pragmata's import contract accepts, and nothing else. Both
# QueryResponsePair and Chunk are extra="forbid", so run_bot.py's provenance extras have to
# be projected away before validation - exactly the jq projection scripts/annotation/import.sh
# applies before calling `pragmata annotation import`. Restated here because this is the same
# boundary in Python: the workspace JSONL keeps the extras (the eval manifest reads them) and
# only the schema fields cross over.
PAIR_FIELDS = ("query", "answer", "context_set", "language")
CHUNK_FIELDS = ("chunk_id", "doc_id", "chunk_rank", "text")


def write_staged(csv_path: Path, frame: pd.DataFrame, prov_fields: dict) -> None:
    """Write one staged CSV plus the provenance sidecar the freshness guard reads.

    Public because evaluator_report.py stages the third population (`testsplit`) through it:
    one writer means one sidecar shape, and therefore one freshness rule in `staged_csv` for
    all three.

    ``output_sha256`` is the CSV's own bytes rather than its inputs', which is what lets
    `predict` refuse a CSV that has drifted from the record beside it - see staged_csv. The
    sidecar is computed after the write for that reason, and the whole pair is written
    together so a half-staged population cannot pass the guard.
    """
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(csv_path, index=False, encoding="utf-8")
    prov = ws.provenance(
        script="scripts/eval/predict_evaluators.py",
        output_sha256=ws.sha256_file(csv_path),
        n_rows=len(frame),
        columns=list(frame.columns),
        **prov_fields,
    )
    ec.sidecar_path(csv_path).write_text(
        json.dumps(prov, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _check_text_columns(frame: pd.DataFrame, task: str, source: str) -> None:
    """Refuse a staged frame whose text columns are not all non-blank strings.

    pragmata's predict schema enforces this itself, but several frames deep and after the
    model has been resolved. Failing here names the task, the column and the row count
    instead, and does it before anything expensive. Refusing rather than dropping the rows is
    deliberate: a silently smaller population changes every prevalence estimate computed from
    the output, and nothing downstream would say so.
    """
    for column in ec.TEXT_COLUMNS[task]:
        values = frame[column]
        blank = values.isna() | values.astype("string").str.strip().eq("")
        if blank.any():
            raise SystemExit(
                f"{source}: {int(blank.sum())} of {len(frame)} {task} row(s) have a blank "
                f"{column!r}.\n"
                "  pragmata's prediction contract requires non-blank text in both text "
                "columns. Fix the\n  source rather than dropping the rows: a smaller "
                "population silently changes every rate."
            )


# --- predict-inputs: the annotated population ------------------------------------------


def stage_annotated(exports: Path) -> int:
    """Stage the frozen export as unlabelled per-task CSVs, identity kept, labels dropped.

    Pooled exactly as train_evaluators.combine pools it - programmes derived from the tree via
    ec.programmes (which also refuses a stale freeze pin), submitted responses only, and
    `source_domain` written rather than trusted - so the annotated population here is the same
    population the human-label metrics describe. Anything else would make the comparison the
    two exist for meaningless.

    Two differences from the training staging, both forced:

    - Every label column is dropped. pragmata's predict contract does not merely ignore them,
      it REJECTS them (validate_eval_predict_frame refuses the task's label columns and
      anything named label_*), which is the right call - a prediction input that carries the
      answers invites scoring a model against its own input.
    - Rows are reduced to one per ITEM. The export carries one row per annotator, and with the
      labels gone those rows are exact duplicates of the same text: predicting them would run
      the model two or three times over identical input, and `eval score --prediction-id`
      would then have to collapse them again. ec.ITEM_KEYS is the same grain pragmata
      consolidates to, so the population is unchanged - only the redundancy is.
    """
    programmes = ec.programmes(exports)
    print(f"pooling {len(programmes)} programme(s) from {exports}", file=sys.stderr)

    staged: dict[str, tuple[pd.DataFrame, list[str]]] = {}
    for task in ec.TASKS:
        frames = []
        contributing = []
        for programme in programmes:
            raw = ec.read_task(exports, programme, task)
            frame = ec.submitted(raw)
            if frame.empty:
                reason = "no_data" if raw.empty else "no_rows_after_filter"
                print(
                    f"  {programme}/{task}: contributes nothing ({reason})",
                    file=sys.stderr,
                )
                continue
            frame = frame.copy()
            frame["source_domain"] = programme
            frames.append(frame)
            contributing.append(programme)
        if not frames:
            raise SystemExit(
                f"no programme contributed any {task} rows from {exports}.\n"
                "  Nothing to predict on - check the export tree is populated."
            )

        pooled = pd.concat(frames, ignore_index=True)
        # An explicit column list rather than a drop list: a label column added upstream would
        # then reach the predict contract and be rejected there, which reads as a pragmata bug
        # rather than as an export schema change.
        keep = [*ec.TEXT_COLUMNS[task], *IDENTITY_COLUMNS[task], "source_domain"]
        missing = [c for c in keep if c not in pooled.columns]
        if missing:
            raise SystemExit(
                f"{task}: the pooled export is missing column(s) prediction needs: "
                f"{', '.join(missing)}.\n"
                "  The export schema has changed; fix IDENTITY_COLUMNS rather than staging "
                "a CSV the scorer\n  cannot group."
            )
        unlabeled = pooled[keep].drop_duplicates(
            subset=list(ec.ITEM_KEYS[task]), keep="first"
        )
        _check_text_columns(unlabeled, task, f"{task} (annotated)")
        print(
            f"  {task}: {len(pooled)} response rows -> {len(unlabeled)} items "
            f"from {len(contributing)} programme(s)",
            file=sys.stderr,
        )
        staged[task] = (unlabeled, contributing)

    # Every task pooled before any is written, for the reason combine gives: both failure
    # modes above are mid-loop, and the freshness guard checks each CSV independently, so a
    # half-rewritten population directory would pass it task by task.
    for task, (unlabeled, contributing) in staged.items():
        target = PREDICT_INPUTS / "annotated" / f"{task}.csv"
        write_staged(
            target,
            unlabeled,
            {
                "inputs": [exports / p / f"{task}.csv" for p in programmes],
                "task": task,
                "population": "annotated",
                "programmes": programmes,
                "contributing_programmes": contributing,
                "excluded_programmes": sorted(ec.EXCLUDED_PROGRAMMES),
                "freeze_date": ec.FREEZE_DATE,
                "row_filter": "submitted",
                "grain": f"item ({', '.join(ec.ITEM_KEYS[task])})",
                "labels_dropped": list(ec.LABELS[task]),
            },
        )
        print(f"wrote {target.relative_to(ws.ROOT)}", file=sys.stderr)
    return 0


# --- predict-inputs: the corpus population ---------------------------------------------


def _read_pins(path: Path) -> dict[str, str]:
    """`pins.sha256` as {workspace-relative path: sha256}. Empty if the bundle is absent.

    `sha256sum` format - digest, two spaces, path - so the path is taken from the first
    two-space run and not from a naive split: a path with a space in it would otherwise be
    truncated.
    """
    if not path.is_file():
        return {}
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.strip().partition("  ")
        if digest and separator and name:
            pins[name.strip()] = digest
    return pins


def _corpus_pin_records(paths: list[Path]) -> list[dict]:
    """Each source JSONL's sha256 beside the curation bundle's pin for it, matched or not.

    The comparison outcome is recorded either way, never only on success: "these are the same
    bytes the annotations were drawn from" and "these are not" are both facts the numbers rest
    on, and an absent record would read as the first. ``pin_sha256: null`` distinguishes a
    third case - the bundle names no pin for this file at all, e.g. a programme added since.

    Keyed on the canonical ``data/publikationsbot/<name>`` path rather than on where the file
    was found: a copy pulled into data/transfer/ is meant to be the same bytes, and comparing
    it against the pin is the whole point of doing this on the GPU box too.
    """
    pins = _read_pins(CURATION_PINS)
    records = []
    for path in paths:
        digest = ws.sha256_file(path)
        pinned = pins.get(f"./data/publikationsbot/{path.name}") or pins.get(
            f"data/publikationsbot/{path.name}"
        )
        records.append(
            {
                "path": str(
                    path.relative_to(ws.ROOT) if path.is_relative_to(ws.ROOT) else path
                ),
                "sha256": digest,
                "pin_sha256": pinned,
                "matches_curation_pin": None if pinned is None else pinned == digest,
            }
        )
    return records


def stage_corpus(corpus_dir: Path) -> int:
    """Stage the curated corpus as unlabelled per-task CSVs.

    The text columns are built the way pragmata builds the Argilla fields at annotation
    import, read out of its own `record_builder`: retrieval pairs the query with each CHUNK's
    text, grounding pairs the answer with `context_set`, generation pairs the query with the
    answer. `context_set` is carried through **verbatim** - it is a field of the import record,
    NOT something pragmata assembles out of the chunk texts, which is the one assumption here
    worth checking in the source rather than guessing at.

    `record_uuid` comes from pragmata's own `derive_record_uuid`, so a corpus row and the
    export row for the same pair carry the SAME identity. That is what makes the two
    populations comparable, and it is also what makes corpus retrieval panels groupable at all:
    every chunk of a pair is staged, so a corpus panel is complete by construction and
    `--skip-incomplete-panels` has nothing to drop.

    The population is "every curated record that satisfies pragmata's import contract", which
    is the same set that could have been annotated - a record the contract rejects (a query
    whose retrieval returned no chunks, say) could never have reached Argilla either. Rejects
    are counted and recorded rather than silently dropped, and an all-reject file is fatal.
    """
    # Loaded through pragmata rather than re-derived: the record identity and the field
    # mapping are its decisions, and a workspace copy of either would drift silently. Also
    # pins which pragmata answered into the sidecar, as every other stage does.
    _eval_api, _Task, src_root = ec.pragmata_eval()
    try:
        from pragmata.core.annotation.record_builder import derive_record_uuid
        from pragmata.core.schemas.annotation_import import QueryResponsePair
    except ImportError as exc:
        raise SystemExit(
            f"corpus staging needs pragmata's annotation import schema: {exc}\n"
            "  It imports argilla, which the GPU host's training venv does not install.\n"
            "  Stage the corpus population on the CPU box and transfer the CSVs, or install\n"
            "  argilla there. See docs/eval-prediction.md."
        ) from exc

    paths = sorted(corpus_dir.glob(f"*{ec.CURATED_SUFFIX}"))
    programmes = []
    rows: dict[str, list[dict]] = {task: [] for task in ec.TASKS}
    n_valid = n_invalid = 0
    used_paths = []
    for path in paths:
        programme = path.name.removesuffix(ec.CURATED_SUFFIX)
        if programme in ec.EXCLUDED_PROGRAMMES:
            print(f"  {programme}: excluded", file=sys.stderr)
            continue
        programmes.append(programme)
        used_paths.append(path)
        valid = invalid = 0
        for record in ws.read_jsonl(path):
            payload = {
                **{f: record.get(f) for f in PAIR_FIELDS},
                "chunks": [
                    {f: chunk.get(f) for f in CHUNK_FIELDS}
                    for chunk in (record.get("chunks") or [])
                ],
            }
            try:
                pair = QueryResponsePair.model_validate(payload)
            # Broad: pydantic raises ValidationError, but importing it here just to name it
            # would tie corpus staging to a pydantic version this workspace does not pin.
            except Exception as exc:
                invalid += 1
                if invalid == 1:
                    print(
                        f"  {programme}: first rejected record "
                        f"(query_id={record.get('query_id')!r}): "
                        f"{str(exc).splitlines()[0]}",
                        file=sys.stderr,
                    )
                continue
            valid += 1
            record_uuid = derive_record_uuid(pair)
            query_id = record.get("query_id", "")
            rows["grounding"].append(
                {
                    "answer": pair.answer,
                    "context_set": pair.context_set,
                    "record_uuid": record_uuid,
                    "query_id": query_id,
                    "source_domain": programme,
                }
            )
            rows["generation"].append(
                {
                    "query": pair.query,
                    "answer": pair.answer,
                    "record_uuid": record_uuid,
                    "query_id": query_id,
                    "source_domain": programme,
                }
            )
            for chunk in pair.chunks:
                rows["retrieval"].append(
                    {
                        "query": pair.query,
                        "chunk": chunk.text,
                        "record_uuid": record_uuid,
                        "chunk_id": chunk.chunk_id,
                        "chunk_rank": chunk.chunk_rank,
                        "n_retrieved_chunks": len(pair.chunks),
                        "query_id": query_id,
                        "doc_id": chunk.doc_id,
                        "source_domain": programme,
                    }
                )
        if valid == 0:
            raise SystemExit(
                f"{path}: not one of its {invalid} record(s) satisfies pragmata's import "
                "contract.\n"
                "  That is a source problem, not a filter: check the JSONL is the curated "
                "corpus and not\n  an intermediate batch."
            )
        print(f"  {programme}: {valid} record(s), {invalid} rejected", file=sys.stderr)
        n_valid += valid
        n_invalid += invalid

    if not programmes:
        raise SystemExit(
            f"no *{ec.CURATED_SUFFIX} under {corpus_dir} outside the excluded set."
        )

    pin_records = _corpus_pin_records(used_paths)
    matched = sum(1 for r in pin_records if r["matches_curation_pin"])
    print(
        f"curation pins: {matched} of {len(pin_records)} source file(s) match "
        f"{CURATION_PINS.relative_to(ws.ROOT)}",
        file=sys.stderr,
    )

    staged: dict[str, pd.DataFrame] = {}
    for task in ec.TASKS:
        frame = pd.DataFrame(rows[task])
        keep = [
            *ec.TEXT_COLUMNS[task],
            *IDENTITY_COLUMNS[task],
            *CORPUS_IDENTITY_COLUMNS[task],
            "source_domain",
        ]
        frame = frame[keep]
        # The same pair can appear in two programmes' curated files, and derive_record_uuid is
        # content-addressed, so the duplicate collapses to one identity. Dropped here rather
        # than left to the scorer, which rejects duplicate scoring units outright.
        before = len(frame)
        frame = frame.drop_duplicates(subset=list(ec.ITEM_KEYS[task]), keep="first")
        if before != len(frame):
            print(
                f"  {task}: dropped {before - len(frame)} duplicate item(s)",
                file=sys.stderr,
            )
        _check_text_columns(frame, task, f"{task} (corpus)")
        staged[task] = frame

    for task, frame in staged.items():
        target = PREDICT_INPUTS / "corpus" / f"{task}.csv"
        write_staged(
            target,
            frame,
            {
                "inputs": used_paths,
                "pragmata_src": src_root,
                "task": task,
                "population": "corpus",
                "programmes": programmes,
                "excluded_programmes": sorted(ec.EXCLUDED_PROGRAMMES),
                "corpus_dir": str(
                    corpus_dir.relative_to(ws.ROOT)
                    if corpus_dir.is_relative_to(ws.ROOT)
                    else corpus_dir
                ),
                "corpus_sources": pin_records,
                "curation_pins": str(CURATION_PINS.relative_to(ws.ROOT)),
                "n_records_valid": n_valid,
                "n_records_rejected": n_invalid,
                "grain": f"item ({', '.join(ec.ITEM_KEYS[task])})",
                "record_uuid_source": "pragmata derive_record_uuid",
            },
        )
        print(
            f"wrote {target.relative_to(ws.ROOT)} ({len(frame)} rows)", file=sys.stderr
        )
    return 0


# --- predict ---------------------------------------------------------------------------


def staged_csv(task: str, population: str) -> tuple[Path, dict]:
    """The staged CSV for a (task, population) and its sidecar, checked against the pin.

    The same discipline train_evaluators._training_csv applies, and for the same reason:
    existence is not enough, and the gap is a silent one. ec.require_fresh_staged_csv owns
    that rule and its reasoning; this names the layout, the command that rebuilds each
    population, and the two conditions particular to prediction.

    The population is checked against the sidecar because prediction stages one directory per
    population, and the directory name alone is not evidence of what is in it.

    The freeze check applies to the annotated population only, because it is the only one
    pooled from the export. The corpus population's own provenance is the per-source sha256s
    and the curation-pin comparison in its sidecar, which staging records and this echoes.
    `testsplit` is staged per run rather than by a make target, so its hint says so.
    """
    path = PREDICT_INPUTS / population / f"{task}.csv"
    rebuild = (
        f"  Run `make eval-predict-inputs POPULATION={population}`."
        if population in STAGED_POPULATIONS
        else "  It is staged per run by scripts/eval/evaluator_report.py calibration."
    )
    return path, ec.require_fresh_staged_csv(
        path,
        rebuild=rebuild,
        population=population,
        check_freeze=population == "annotated",
    )


def check_testsplit_run(sidecar: dict, csv_path: Path, evaluator_run_id: str) -> None:
    """Refuse a testsplit CSV staged from a different run than the one predicting it.

    A test split belongs to exactly one training run - it is that run's held-out quarter -
    so applying run B to a CSV staged from run A's parquet would produce a directory that
    looks like B's calibration data and is not. The calibration flow never hits this (it
    stages and predicts inside one process), but the staged file outlives that process,
    and the sidecar names its run precisely so a later caller can be checked against it.
    """
    staged_run = sidecar.get("evaluator_run_id")
    if staged_run != evaluator_run_id:
        raise SystemExit(
            f"{csv_path.relative_to(ws.ROOT)} is run {staged_run!r}'s test split, but the\n"
            f"  prediction was asked for run {evaluator_run_id!r}. A test split belongs to\n"
            f"  the run that held it out - predicting it with another run would produce a\n"
            f"  directory that reads as that run's calibration data and is not.\n"
            "  It is staged per run by scripts/eval/evaluator_report.py calibration."
        )


def _predict(eval_api, **kwargs):
    """Call predict_labels, turning a missing training extra into an instruction.

    tlmtc is imported lazily inside pragmata's own adapter, not at api.eval import time, so
    the absence of the training stack only surfaces here - as a bare ImportError several
    frames deep, which reads as a bug rather than as an unconfigured environment.
    """
    try:
        return eval_api.predict_labels(**kwargs)
    except ImportError as exc:
        raise SystemExit(
            f"evaluator prediction needs a dependency this environment lacks: {exc}\n"
            "  Inference runs the same stack training does (pragmata[eval] -> tlmtc), which\n"
            "  is deliberately NOT in this workspace's uv.lock - it pulls a CUDA torch build,\n"
            "  and the lock freezes the environment behind the published human-label numbers.\n"
            "  The install steps are in docs/eval-training.md."
        ) from exc


def predict(
    task: str,
    population: str,
    *,
    evaluator_run_id: str | None = None,
    use_cpu: bool = False,
    batch_size: int | None = None,
    overwrite: bool = False,
) -> Path:
    """Predict one population with one evaluator; return the population-named output dir.

    **The output layout is this workspace's, not pragmata's, and it has to be.** tlmtc names
    its prediction directory after the EVALUATOR run id - `resolve_prediction_paths` builds
    `prediction_outputs/<run_id>/` and `predict_tlmtc` writes `probabilities.csv` and
    `predictions.csv` into it through `mkdir(exist_ok=True)` and a plain `to_csv`. There is no
    guard of any kind: predicting a second population with the same evaluator OVERWRITES the
    first, silently, and pragmata's `pragmata_predict.meta.json` is rewritten to match, so
    afterwards nothing on disk says which population the numbers describe. Three populations
    per evaluator (annotated, corpus, and each run's own test split) makes that a certainty
    rather than a risk.

    So a successful run's output tree is MOVED to `prediction_outputs/<run_id>-<population>/`.
    That name is still scoreable, which is the constraint the scheme had to satisfy:
    `eval score --prediction-id X` resolves `prediction_outputs/X/pragmata_predict.meta.json`
    by directory name alone (resolve_eval_predict_meta_path), validates the `task` recorded
    inside it, and scores the `predictions.csv` beside it. It never compares the meta's own
    `run_id` field against the directory, so the field keeps naming the evaluator - which is
    the more useful of the two things it could say, and is what the workspace record beside it
    cross-references.

    The staging directory is cleaned before the run rather than after: a leftover
    `prediction_outputs/<run_id>/` can only be an interrupted run, since a completed one
    always moves.
    """
    csv_path, sidecar = staged_csv(task, population)
    run = ec.resolve_evaluator_run(task, evaluator_run_id)
    # Printed whether resolved or given. An implicit "latest" is fine for a scratch run and
    # wrong for a published one, and the difference has to be visible in the run log rather
    # than inferred from the absence of a flag.
    origin = "given" if evaluator_run_id else "resolved as the latest for this task"
    print(f"evaluator run: {run.run_id} ({origin})", file=sys.stderr)

    # Before the GPU check, because the sidecar decides it on its own - and before the
    # collision guard, which would happily accept the directory a mismatch produces: it is
    # named after the predicting run and would hold another run's split.
    if population == "testsplit":
        check_testsplit_run(sidecar, csv_path, run.run_id)

    final_dir = ec.PREDICTION_OUTPUTS / f"{run.run_id}-{population}"
    if final_dir.exists() and not overwrite:
        raise SystemExit(
            f"{final_dir.relative_to(ws.ROOT)} already holds a prediction for this "
            f"(evaluator, population).\n"
            "  Pass --overwrite to replace it, or predict with a different evaluator run."
        )
    staging_dir = ec.PREDICTION_OUTPUTS / run.run_id
    if staging_dir.exists():
        print(
            f"note: removing {staging_dir.relative_to(ws.ROOT)} - a completed run always "
            "moves its\n  output to the population-named directory, so this is an "
            "interrupted run's leftover.",
            file=sys.stderr,
        )
        shutil.rmtree(staging_dir)

    ec.require_gpu(use_cpu=use_cpu)
    eval_api, Task, src_root = ec.pragmata_eval()

    predict_kwargs: dict[str, object] = {}
    if use_cpu:
        predict_kwargs["use_cpu"] = True
    if batch_size is not None:
        predict_kwargs["batch_size"] = batch_size
    print(
        f"predicting {task}/{population} from {csv_path.relative_to(ws.ROOT)} "
        f"(predict_kwargs={json.dumps(predict_kwargs, sort_keys=True)})",
        file=sys.stderr,
    )

    started = time.time()
    result = _predict(
        eval_api,
        unlabeled_data_path=str(csv_path),
        evaluator_run_id=run.run_id,
        task=Task(task),
        base_dir=str(ws.DATA_DIR),
        predict_kwargs=predict_kwargs,
    )

    produced = Path(result.paths.prediction_run_dir)
    # The move below is this workspace's step, so it has to be sure what it is moving. Both
    # artifacts and the meta sidecar, all written by this process: an output that predates the
    # run would mean the layout moved under us and the previous run's numbers are about to be
    # filed under this population's name.
    expected = ["probabilities.csv", "predictions.csv", "pragmata_predict.meta.json"]
    stale = [
        name
        for name in expected
        if not (produced / name).is_file()
        or (produced / name).stat().st_mtime < started
    ]
    if produced != staging_dir or stale:
        raise SystemExit(
            f"predict_labels reported success, but its output tree is not what this run\n"
            f"  produced: {produced}\n"
            f"  expected {staging_dir} holding {', '.join(expected)}\n"
            f"  missing or predating the run: {', '.join(stale) or 'none'}\n"
            "  The population-aware relocation in predict() reads pragmata's layout; if that\n"
            "  layout has moved, fix it there rather than filing whatever is on disk."
        )

    if final_dir.exists():
        shutil.rmtree(final_dir)
    shutil.move(str(produced), str(final_dir))

    # A provenance record inside the final directory, because that directory is what gets
    # pushed off the GPU box and read months later. pragmata's own pragmata_predict.meta.json
    # carries run_id, task and the input path; nothing there names the population, the freeze
    # or corpus pins behind the input, or the commit of this workspace that filed it - and the
    # directory name is this workspace's invention, so it has to be explained from inside. The
    # `.workspace.` infix is load-bearing: data/eval/ is pragmata's tool tree by the ownership
    # rule in docs/eval.md, so a file this workspace wrote there has to say so in its name.
    record = ws.provenance(
        script="scripts/eval/predict_evaluators.py",
        inputs=[csv_path],
        pragmata_src=src_root,
        task=task,
        population=population,
        evaluator_run_id=run.run_id,
        evaluator_run_id_origin=origin,
        prediction_id=final_dir.name,
        prediction_dir=str(final_dir.relative_to(ws.ROOT)),
        input_csv=str(csv_path.relative_to(ws.ROOT)),
        input_csv_sha256=ws.sha256_file(csv_path),
        input_provenance={
            key: sidecar.get(key)
            for key in (
                "freeze_date",
                # The staged split's own run, not this prediction's - the guard above has
                # just established they are the same one, and recording it is what lets a
                # testsplit directory say whose split it holds without the sidecar beside it.
                "evaluator_run_id",
                "corpus_sources",
                "n_records_valid",
                "n_records_rejected",
                "grain",
                "n_rows",
            )
            if sidecar.get(key) is not None
        },
        predict_kwargs=predict_kwargs,
        # Recorded because it is what decides whether this output can be scored at all:
        # grounding trains on three of its five labels, and pragmata's grounding SCORE schema
        # requires all five, so a grounding prediction is short two columns the scorer demands.
        # Better named in the record than discovered from a schema error.
        evaluator_label_names=run.label_names,
    )
    (final_dir / "predict_provenance.workspace.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print(f"SUCCESS: {final_dir}")
    return final_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    inputs_parser = sub.add_parser(
        "predict-inputs",
        help="Stage the unlabelled per-task CSVs for one population",
    )
    inputs_parser.add_argument(
        "--population", choices=list(STAGED_POPULATIONS), required=True
    )
    # --exports only, not ec.add_common_args: staging writes to data/eval-inputs/, not to a
    # dated report dir, so the shared --out-dir would be a flag that does nothing.
    inputs_parser.add_argument(
        "--exports",
        type=Path,
        default=ec.FROZEN_EXPORTS,
        help="Annotation export tree to pool (default: the frozen canonical export).",
    )
    inputs_parser.add_argument(
        "--corpus-dir",
        type=Path,
        default=None,
        help=(
            "Directory holding the curated *_combined.curated.jsonl files "
            "(default: data/publikationsbot/, falling back to data/transfer/publikationsbot/)."
        ),
    )

    predict_parser = sub.add_parser(
        "predict", help="Run one evaluator over one staged population (GPU)"
    )
    predict_parser.add_argument("task", choices=list(ec.TASKS))
    predict_parser.add_argument(
        "--population",
        choices=list(PREDICT_POPULATIONS),
        required=True,
        help=(
            "annotated|corpus are staged by predict-inputs. testsplit is internal: "
            "evaluator_report.py calibration stages a run's own held-out split and predicts "
            "it in one pass, so a manual re-run must pass the --evaluator-run-id that split "
            "was staged from."
        ),
    )
    predict_parser.add_argument(
        "--evaluator-run-id",
        default=None,
        help=(
            "Evaluator training run to apply. Default: the latest for this task, printed and "
            "recorded. Pass it explicitly for anything published - 'latest' is a property of "
            "the box, not of the numbers."
        ),
    )
    predict_parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="tlmtc prediction batch size (default: tlmtc's own 32).",
    )
    predict_parser.add_argument(
        "--use-cpu",
        action="store_true",
        help="Force CPU inference, skipping the GPU check. Slow; for smoke tests.",
    )
    predict_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing prediction for this (evaluator, population).",
    )

    args = parser.parse_args()

    if args.command == "predict-inputs":
        if args.population == "annotated":
            return stage_annotated(ec.resolve_exports(args.exports))
        return stage_corpus(ec.resolve_corpus_dir(args.corpus_dir))

    predict(
        args.task,
        args.population,
        evaluator_run_id=args.evaluator_run_id,
        use_cpu=args.use_cpu,
        batch_size=args.batch_size,
        overwrite=args.overwrite,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
