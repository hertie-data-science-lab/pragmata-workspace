#!/usr/bin/env python3
"""Per-item prediction CSVs - the readable deliverable copy of what an evaluator predicted.

Columns and caveats are defined in `docs/data-dictionary.md`
(`predictions_vs_human.csv`, `predictions.csv`). Two deliverables, both at ITEM grain:

- `predictions_vs_human.<task>.csv`, the `annotated` population only: each item's
  majority-consolidated HUMAN label beside the evaluator's predicted label, the predicted
  probability, and a per-label agreement flag. It is the item-level companion of the aggregate
  twin comparison - `eval_metric_estimates.csv` against
  `synthetic_metric_estimates.annotated.csv` says whether two rates agree, and cannot say
  which items they disagree on.
- `predictions.<task>.<population>.csv`, for any predicted population: the predicted labels
  and their probabilities merged into one file. The prediction run directory keeps the split
  `predictions.csv` / `probabilities.csv` pair tlmtc wrote, which is the artefact
  `eval score --prediction-id` reads; this is the readable copy, with the text columns dropped
  and the identity columns kept so it joins to the rest of the bundle.

**Nothing here is aggregated.** No rate, no denominator, no interval: population metrics come
from `pragmata eval score` through `score_synthetic_predictions.py` and from nowhere else, for
the reason docs/synthetic-evaluators.md gives under "What is not done" - a rate computed here
would be a number the score CLI would not produce, from a code path nothing else uses. These
files are per-item passthrough, so the aggregate they support is the reader's own.

**Nothing here re-implements consolidation either.** The human side comes from
`predict_evaluators.pooled_annotated_items`, the one path that builds the annotated population,
which reduces responses to items with pragmata's own `consolidate_labels_by_majority`. That is
the same function eval train and score ingestion run, so the human labels in this file are the
ones the human metrics rest on rather than a workspace majority vote that resembles them.

Usage:
  scripts/eval/export_predictions.py vs-human
  scripts/eval/export_predictions.py predictions --population all-generated
  scripts/eval/export_predictions.py vs-human --prediction-id <dir> --out-dir /tmp/scratch
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import eval_common as ec
import predict_evaluators as pe
import score_synthetic_predictions as sp
import workspace as ws

# The prediction directory's two CSVs. Same columns, same rows, different values: predictions
# carries the 0/1 label tlmtc's threshold produced, probabilities the float behind it.
PREDICTIONS_CSV = "predictions.csv"
PROBABILITIES_CSV = "probabilities.csv"

# Column prefixes. The PREDICTED label keeps its bare name, as it has in every artefact from
# the export CSVs to predictions.csv, so a reader who knows the label vocabulary needs no
# translation; the columns that are new here are the ones that get prefixed. `human_` and
# `agree_` exist only in predictions_vs_human.
HUMAN_PREFIX = "human_"
PROBABILITY_PREFIX = "prob_"
AGREEMENT_PREFIX = "agree_"

# Six decimals, as every other float column in these deliverables (evaluator_metrics,
# evaluator_calibration). Formatted rather than left to csv's repr so a re-run on another
# platform writes the same bytes.
PROBABILITY_FORMAT = "{:.6f}"


def _rel(path: Path) -> str:
    """A path as the operator sees it - workspace-relative where it can be.

    The same guard ws._input_record applies: --out-dir and --exports may point outside the
    tree, and relative_to alone would raise on those rather than print them.
    """
    return str(path.relative_to(ws.ROOT) if path.is_relative_to(ws.ROOT) else path)


def identity_columns(frame: pd.DataFrame, task: str, source: str) -> list[str]:
    """The identity columns to carry into a deliverable, in a fixed order.

    One rule for both outputs, and the same rule the staging side guarantees: predict staging
    refuses to write a CSV missing any of `pe.IDENTITY_COLUMNS[task]` or `source_domain`, and
    tlmtc concatenates the input frame back onto its output, so every one of them is on
    `predictions.csv` by construction. Required rather than carried-if-present for exactly that
    reason - an absent one means the frame did not come through this pipeline's staging, and
    guessing which columns identify a row is how a deliverable ends up joinable to nothing.

    The all-generated identity (`query_id`, and `doc_id` for retrieval) is carried when
    present, and is absent by nature on the annotated population: the annotation exports do not
    carry `query_id` at all - see the data dictionary's note on joining.
    """
    required = [*pe.IDENTITY_COLUMNS[task], "source_domain"]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise SystemExit(
            f"{source}: missing identity column(s) {', '.join(missing)}.\n"
            "  Prediction staging writes all of them and tlmtc passes them through, so a "
            "prediction that\n  lacks one was not staged by "
            "scripts/eval/predict_evaluators.py predict-inputs. Without them the\n"
            "  rows identify nothing and the CSV joins to nothing - re-stage and re-predict."
        )
    optional = [
        column
        for column in pe.ALL_GENERATED_IDENTITY_COLUMNS[task]
        if column in frame.columns and column not in required
    ]
    return [*pe.IDENTITY_COLUMNS[task], *optional, "source_domain"]


def _require_unique(frame: pd.DataFrame, keys: list[str], source: str) -> None:
    """Refuse a frame with two rows for one item.

    Prediction inputs are staged at item grain, so a duplicate key means either the staging
    collapsed nothing or the file was concatenated twice. Either way the merges below would
    fan out silently and every downstream count would be wrong.
    """
    duplicated = frame.duplicated(subset=keys, keep=False)
    if duplicated.any():
        raise SystemExit(
            f"{source}: {int(duplicated.sum())} of {len(frame)} row(s) share an item key "
            f"({', '.join(keys)}).\n"
            "  The population is staged one row per item, so a joined output would fan out. "
            "Re-stage the\n  population and re-predict rather than deduplicating here."
        )


def _require_binary(
    frame: pd.DataFrame, columns: list[str], source: str
) -> pd.DataFrame:
    """Return the frame with the named label columns as int64, refusing anything not 0/1.

    Predicted labels are tlmtc's thresholded output and consolidated human labels are
    pragmata's, so both are 0/1 already. Checked anyway because the agreement flag is an
    equality test between them: a null on either side would compare unequal and be published
    as a disagreement, and a float 0.0/1.0 read back from CSV would compare unequal to an
    int on some pandas versions and not others.
    """
    for column in columns:
        values = frame[column]
        if values.isna().any():
            raise SystemExit(
                f"{source}: {int(values.isna().sum())} row(s) have no {column!r}.\n"
                "  A blank label cannot be compared or counted. Fix the source rather than "
                "filling it in:\n  a null read as a disagreement is a fabricated finding."
            )
        # {0, 1} covers True/False too - they compare and hash equal, which is exactly why a
        # bool column passes this check and casts cleanly below.
        unexpected = sorted(set(values.unique()) - {0, 1})
        if unexpected:
            raise SystemExit(
                f"{source}: {column!r} holds value(s) that are not 0 or 1: "
                f"{', '.join(repr(v) for v in unexpected[:5])}.\n"
                "  These columns are binary labels on both sides of the comparison; anything "
                "else means the\n  file is not what this script thinks it is."
            )
    return frame.astype({column: "int64" for column in columns})


def _read_prediction_csv(
    path: Path, task: str, wanted_labels: list[str] | None = None
) -> tuple[pd.DataFrame, list[str]]:
    """(frame, label columns) for one of a prediction directory's two CSVs.

    Read through `usecols`: an all-generated retrieval `probabilities.csv` carries every
    chunk's text and runs to tens of MB, none of which reaches either deliverable. The label
    columns are discovered from the header first, because which of the task's labels a run
    predicted is a property of the run - grounding trains three of its five - and is not
    knowable before reading it.
    """
    if not path.is_file():
        raise SystemExit(
            f"no {path.name} at {_rel(path)}.\n"
            "  Every prediction directory holds predictions.csv and probabilities.csv; one "
            "that does not is\n  incomplete - re-pull the predictions tree, or re-predict."
        )
    present = ec.csv_columns(path)
    labels = [
        label
        for label in ec.LABELS[task]
        if label in present and (wanted_labels is None or label in wanted_labels)
    ]
    keys = list(ec.ITEM_KEYS[task])
    missing_keys = [key for key in keys if key not in present]
    if missing_keys:
        raise SystemExit(
            f"{_rel(path)}: missing the {task} item key(s) {', '.join(missing_keys)}.\n"
            f"  Items are identified by ({', '.join(keys)}) and there is no positional "
            "fallback: pairing\n  predictions to labels by row order would publish a silent "
            "mis-join. A testsplit prediction\n  legitimately has no item keys - it is not a "
            "population this script can export."
        )
    identity = [
        column
        for column in (
            *pe.IDENTITY_COLUMNS[task],
            *pe.ALL_GENERATED_IDENTITY_COLUMNS[task],
        )
        if column in present
    ]
    usecols = [*keys, *identity, "source_domain", *labels]
    frame = pd.read_csv(path, usecols=lambda c, keep=set(usecols): c in keep)
    return frame, labels


def predicted_items(
    prediction_dir: Path, task: str
) -> tuple[pd.DataFrame, list[str], list[str]]:
    """(item frame, predicted label columns, identity columns) for one prediction directory.

    `predictions.csv` joined to `probabilities.csv` on the item keys, the labels of the second
    prefixed `prob_`. Joined rather than zipped by position for the reason
    evaluator_report.calibration gives about its own join: tlmtc does preserve row order today,
    but a file that had been reordered or partially written would pair each probability with
    another item's label - silently, and the result would read as a badly calibrated model
    rather than as a broken join.

    Which labels a run predicted is read off `predictions.csv` rather than taken from
    ec.LABELS, and the two files must agree on them: grounding trains three of its five labels,
    so a column-for-column expectation would refuse every grounding prediction.
    """
    keys = list(ec.ITEM_KEYS[task])
    predictions, labels = _read_prediction_csv(prediction_dir / PREDICTIONS_CSV, task)
    if not labels:
        raise SystemExit(
            f"{_rel(prediction_dir / PREDICTIONS_CSV)} carries none of the {task} label columns "
            f"({', '.join(ec.LABELS[task])}).\n"
            "  There is nothing to export. tlmtc appends one column per label it predicted; a "
            "file with none\n  is not a prediction for this task."
        )
    probabilities, probability_labels = _read_prediction_csv(
        prediction_dir / PROBABILITIES_CSV, task, wanted_labels=labels
    )
    absent = [label for label in labels if label not in probability_labels]
    if absent:
        raise SystemExit(
            f"{prediction_dir.name}: {PREDICTIONS_CSV} predicts "
            f"{', '.join(absent)} but {PROBABILITIES_CSV} carries no such column(s).\n"
            "  The two are written by one tlmtc pass over one frame and must name the same "
            "labels. Re-predict\n  rather than exporting a label with no probability behind it."
        )

    _require_unique(predictions, keys, _rel(prediction_dir / PREDICTIONS_CSV))
    _require_unique(probabilities, keys, _rel(prediction_dir / PROBABILITIES_CSV))
    if len(predictions) != len(probabilities):
        raise SystemExit(
            f"{prediction_dir.name}: {PREDICTIONS_CSV} has {len(predictions)} row(s) and "
            f"{PROBABILITIES_CSV} has {len(probabilities)}.\n"
            "  One tlmtc pass writes both over the same frame, so they cannot describe "
            "different row sets.\n  Re-predict; a partially written file is the likeliest "
            "cause."
        )

    source = _rel(prediction_dir / PREDICTIONS_CSV)
    identity = identity_columns(predictions, task, source)
    predictions = _require_binary(predictions, labels, source)
    merged = predictions[[*identity, *labels]].merge(
        probabilities[[*keys, *labels]].rename(
            columns={label: PROBABILITY_PREFIX + label for label in labels}
        ),
        on=keys,
        how="inner",
    )
    if len(merged) != len(predictions):
        raise SystemExit(
            f"{prediction_dir.name}: {len(merged)} of {len(predictions)} item(s) matched "
            f"between {PREDICTIONS_CSV} and {PROBABILITIES_CSV} on "
            f"({', '.join(keys)}).\n"
            "  The two files hold the same row count but not the same items, so nothing here "
            "can be paired\n  up. Re-predict."
        )
    return merged, labels, identity


def _formatted(frame: pd.DataFrame, labels: list[str]) -> pd.DataFrame:
    """Probabilities rendered to a fixed number of decimals, in place on a copy."""
    frame = frame.copy()
    for label in labels:
        column = PROBABILITY_PREFIX + label
        frame[column] = frame[column].astype(float).map(PROBABILITY_FORMAT.format)
    return frame


def _found(predictions: dict[str, dict], task: str, population: str) -> dict | None:
    """One task's discovered prediction, or None having said why there is none."""
    found = predictions.get(task)
    if found is None:
        print(
            f"  {task}: no prediction for population {population!r} - not exported",
            file=sys.stderr,
        )
    return found


def _write_all(pending: list[tuple[Path, pd.DataFrame, list[str], dict]]) -> int:
    """Write every prepared deliverable, or none.

    The preparation loops above validate all three tasks before this is called, for the reason
    train_evaluators.combine gives about its staging directory: every refusal in this script is
    mid-loop, so writing as it goes would leave one task's CSV from this run beside another's
    from the last one. A mixed-vintage set is worse than no output, because each file carries
    its own plausible `.provenance.json` and nothing says the three do not belong together.
    """
    for target, frame, columns, prov in pending:
        ws.write_csv(
            target, frame[columns].to_dict("records"), columns=columns, prov=prov
        )
        print(f"wrote {_rel(target)} ({len(frame)} items)", file=sys.stderr)
    return 0


def _record_fields(record: dict) -> dict:
    """The prediction run's own workspace record, echoed into the deliverable's provenance.

    The same fields score_synthetic_predictions.py carries for the same reason: this CSV's
    numbers are the prediction run's, so its identity - the evaluator, the staged input and
    the freeze or source hashes behind it - has to travel with the deliverable rather than
    only with the run directory it was copied off.
    """
    return {
        "prediction_id": record.get("prediction_id"),
        "evaluator_run_id": record.get("evaluator_run_id"),
        "evaluator_label_names": record.get("evaluator_label_names"),
        "input_csv": record.get("input_csv"),
        "input_csv_sha256": record.get("input_csv_sha256"),
        "input_provenance": record.get("input_provenance"),
    }


def export_vs_human(args) -> int:
    """Write predictions_vs_human.<task>.csv for the annotated population.

    The two sides have to be the same items, and that is checked twice rather than assumed:
    once on the row count, once on the join. The annotated population is pooled from the frozen
    export through the same function that staged the prediction input, so a mismatch is never a
    difference of opinion about the grain - it means the prediction was made from a differently
    staged (or differently frozen) input, and the comparison would silently be against another
    dataset. Refusing names both counts.
    """
    exports = ec.resolve_exports(args.exports)
    predictions = sp.discover_predictions("annotated", args.prediction_id)
    # Before pooling, which reads the whole export: nothing to compare against is the cheaper
    # failure and the likelier one on a box the predictions were never copied to.
    pooled = pe.pooled_annotated_items(exports)
    programmes = ec.programmes(exports)
    # Only for the record: consolidation has already resolved it through ec.consolidated_items,
    # and the deliverable has to name which commit of pragmata decided its human labels.
    _eval_api, _Task, src_root = ec.pragmata_eval()

    out_dir = ws.stage_report_dir("eval", args.out_dir)
    pending: list[tuple[Path, pd.DataFrame, list[str], dict]] = []
    for task in ec.TASKS:
        found = _found(predictions, task, "annotated")
        if found is None:
            continue
        prediction_dir, record = found["dir"], found["record"]
        keys = list(ec.ITEM_KEYS[task])
        predicted, labels, identity = predicted_items(prediction_dir, task)
        human, contributing = pooled[task]

        if len(predicted) != len(human):
            raise SystemExit(
                f"{task}: {prediction_dir.name} covers {len(predicted)} item(s), but the "
                f"frozen export pools to {len(human)}.\n"
                "  The annotated population is one row per item on both sides, built by one "
                "function, so the\n  two must line up exactly. This prediction was made from "
                "a differently staged or differently\n  frozen input - re-stage "
                "(`make eval-predict-inputs POPULATION=annotated`) and re-predict rather\n"
                "  than comparing an evaluator against a population it never saw."
            )

        human_labels = {label: HUMAN_PREFIX + label for label in ec.LABELS[task]}
        merged = predicted.merge(
            human[[*keys, *ec.LABELS[task]]].rename(columns=human_labels),
            on=keys,
            how="inner",
        )
        if len(merged) != len(human):
            raise SystemExit(
                f"{task}: {len(merged)} of {len(human)} item(s) matched between "
                f"{prediction_dir.name} and the frozen export on ({', '.join(keys)}).\n"
                "  The row counts agree but the items do not, so the labels being compared "
                "belong to different\n  records. Re-stage and re-predict."
            )
        merged = _require_binary(
            merged,
            list(human_labels.values()),
            f"{task} human labels pooled from {_rel(exports)}",
        )
        for label in labels:
            # 1/0 rather than True/False: the column exists to be summed and averaged per
            # label, which is the whole point of a per-item flag, and every label column
            # beside it is already 0/1.
            merged[AGREEMENT_PREFIX + label] = (
                merged[HUMAN_PREFIX + label] == merged[label]
            ).astype("int64")

        # Grouped by label rather than by kind, so an item's four numbers for one label read
        # across. A label the evaluator does not predict keeps its human column and gains no
        # others: the columns are named by side, so its absence says "this evaluator does not
        # cover the label" without a blank cell that would read as a measurement.
        columns = list(identity)
        for label in ec.LABELS[task]:
            columns.append(HUMAN_PREFIX + label)
            if label in labels:
                columns += [
                    label,
                    PROBABILITY_PREFIX + label,
                    AGREEMENT_PREFIX + label,
                ]
        not_predicted = [label for label in ec.LABELS[task] if label not in labels]

        print(
            f"  {task}: {len(merged)} item(s), {len(labels)} predicted label(s)"
            + (f", {len(not_predicted)} human-only" if not_predicted else ""),
            file=sys.stderr,
        )
        pending.append(
            (
                out_dir / f"predictions_vs_human.{task}.csv",
                _formatted(merged, labels),
                columns,
                ws.provenance(
                    script="scripts/eval/export_predictions.py",
                    inputs=[
                        prediction_dir / PREDICTIONS_CSV,
                        prediction_dir / PROBABILITIES_CSV,
                        *(exports / p / f"{task}.csv" for p in programmes),
                    ],
                    pragmata_src=src_root,
                    task=task,
                    population="annotated",
                    grain=f"item ({', '.join(keys)})",
                    n_items=len(merged),
                    freeze_date=ec.FREEZE_DATE,
                    programmes=programmes,
                    contributing_programmes=contributing,
                    excluded_programmes=sorted(ec.EXCLUDED_PROGRAMMES),
                    row_filter="submitted",
                    human_labels=(
                        "majority-consolidated per item by pragmata's "
                        "consolidate_labels_by_majority, through "
                        "predict_evaluators.pooled_annotated_items - the same function and "
                        "the same pooling that staged the prediction input"
                    ),
                    join=(
                        f"predictions and probabilities joined on ({', '.join(keys)}), then "
                        "joined to the pooled export on the same keys; the row count and the "
                        "match count are both checked against the export's item count"
                    ),
                    labels_predicted=labels,
                    labels_not_predicted=not_predicted,
                    aggregates="none - this file is per-item passthrough",
                    **_record_fields(record),
                ),
            )
        )

    if not pending:
        raise SystemExit(
            "no task had an annotated prediction to export.\n"
            "  Predict the annotated population first (see docs/synthetic-evaluators.md), or "
            "pull and copy\n  the predictions tree in."
        )
    return _write_all(pending)


def export_population(args) -> int:
    """Write predictions.<task>.<population>.csv - predicted labels and probabilities merged."""
    if args.population == "testsplit":
        raise SystemExit(
            "testsplit is not an exportable population: it is staged from a run's own "
            "held-out split,\n"
            "  which carries a row index rather than record identity, so its rows cannot be "
            "named or joined.\n"
            "  What it exists for is already a deliverable - evaluator_calibration.csv, from "
            "`make eval-model-calibration`."
        )
    predictions = sp.discover_predictions(args.population, args.prediction_id)
    out_dir = ws.stage_report_dir("eval", args.out_dir)
    pending: list[tuple[Path, pd.DataFrame, list[str], dict]] = []
    for task in ec.TASKS:
        found = _found(predictions, task, args.population)
        if found is None:
            continue
        prediction_dir, record = found["dir"], found["record"]
        merged, labels, identity = predicted_items(prediction_dir, task)
        columns = list(identity)
        for label in labels:
            columns += [label, PROBABILITY_PREFIX + label]
        not_predicted = [label for label in ec.LABELS[task] if label not in labels]
        print(
            f"  {task}: {len(merged)} item(s), {len(labels)} label(s)", file=sys.stderr
        )

        pending.append(
            (
                out_dir / f"predictions.{task}.{args.population}.csv",
                _formatted(merged, labels),
                columns,
                ws.provenance(
                    script="scripts/eval/export_predictions.py",
                    inputs=[
                        prediction_dir / PREDICTIONS_CSV,
                        prediction_dir / PROBABILITIES_CSV,
                    ],
                    task=task,
                    population=args.population,
                    grain=f"item ({', '.join(ec.ITEM_KEYS[task])})",
                    n_items=len(merged),
                    join=(
                        "predictions and probabilities joined on "
                        f"({', '.join(ec.ITEM_KEYS[task])}), checked to match every row "
                        "exactly once"
                    ),
                    labels_predicted=labels,
                    labels_not_predicted=not_predicted,
                    text_columns=(
                        "dropped - they are the prediction input's, unchanged, and the run "
                        "directory keeps them"
                    ),
                    aggregates="none - this file is per-item passthrough",
                    **_record_fields(record),
                ),
            )
        )

    if not pending:
        raise SystemExit(
            f"no task had a {args.population!r} prediction to export.\n"
            "  Predict that population first (see docs/synthetic-evaluators.md), or pull and "
            "copy the\n  predictions tree in."
        )
    return _write_all(pending)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def add_shared(target) -> None:
        target.add_argument(
            "--prediction-id",
            action="append",
            default=[],
            metavar="DIR",
            help=(
                "Prediction directory name under data/eval/prediction_outputs/ to export. "
                "Repeatable, one per task. Default: discovered from each directory's own "
                "workspace record for the population. Pass it explicitly for anything "
                "published - discovery refuses ambiguity, but a single candidate is still a "
                "property of the box rather than of the numbers."
            ),
        )
        target.add_argument(
            "--out-dir",
            type=Path,
            default=None,
            help="Output directory (default: reports/eval/<today>/).",
        )

    vs_human = sub.add_parser(
        "vs-human",
        help=(
            "predictions_vs_human.<task>.csv - consolidated human label beside the "
            "prediction, annotated population only"
        ),
    )
    add_shared(vs_human)
    vs_human.add_argument(
        "--exports",
        type=Path,
        default=ec.FROZEN_EXPORTS,
        help="Annotation export tree to pool the human labels from (default: the frozen canonical export).",
    )

    population = sub.add_parser(
        "predictions",
        help=(
            "predictions.<task>.<population>.csv - predicted labels and probabilities in one "
            "file"
        ),
    )
    add_shared(population)
    population.add_argument(
        "--population",
        default="annotated",
        help=(
            "Which predicted population to export: annotated or all-generated "
            "(default: annotated). testsplit is refused - it carries no record identity."
        ),
    )

    args = parser.parse_args()
    if args.command == "vs-human":
        return export_vs_human(args)
    return export_population(args)


if __name__ == "__main__":
    raise SystemExit(main())
