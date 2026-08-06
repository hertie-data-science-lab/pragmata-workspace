#!/usr/bin/env python3
"""Corpus metrics from the synthetic evaluators' predictions - the twin of the human scorer.

Columns and caveats are defined in `docs/eval-data-dictionary.md`
(`synthetic_metric_estimates.csv`). Its twin, `score_human_annotations.py`, scores the human
labels; the two share the metric vocabulary and the CLI they call, so the numbers are
comparable by construction rather than by coincidence.

Runs `pragmata eval score --prediction-id <dir>` once per task over a prediction directory
`predict_evaluators.py` filed, and collects every per-metric estimate into one tidy CSV. The
`alpha_*` columns of the human CSV are absent by definition: a prediction has one label per
item and no annotator disagreement to measure. What replaces them is *not* in the numbers -
it is the evaluator's own quality, which lives in `evaluator_metrics.csv`.

Two populations, and the caveat differs:

- `annotated` - the rows the human metrics were scored on, so each metric can be read beside
  its `eval_metric_estimates.csv` counterpart.
- `corpus` - corpus scale, no human baseline at all. Read only with the evaluator's own test
  metrics in hand: for grounding and generation those are weak enough that the corpus numbers
  are directional at best (see docs/eval-training.md).

Usage:
  scripts/eval/score_synthetic_predictions.py                       # the annotated population
  scripts/eval/score_synthetic_predictions.py --population corpus
  scripts/eval/score_synthetic_predictions.py --prediction-id <dir> # pin them explicitly
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import eval_common as ec
import score_human_annotations as human
import workspace as ws

# The human CSV's columns minus the four `alpha_*` ones, plus the three that identify which
# model produced the numbers. Taken from the human script rather than restated so the two
# files stay column-for-column comparable: a column added there appears here too, in the same
# position, and a rename cannot drift between them.
#
# `source_labels` stays. It is a property of the METRIC - which labels its formula reads - not
# of the labelling process, so it means the same thing on a predicted population, and it is
# what says whether a metric rests on a label this evaluator was even trained for.
ALPHA_COLUMNS = (
    "alpha_min",
    "alpha_min_label",
    "alpha_n_items",
    "alpha_min_degenerate",
)
COLUMNS = [c for c in human.COLUMNS if c not in ALPHA_COLUMNS] + [
    "evaluator_run_id",
    "prediction_id",
    "population",
]


def policy_name(all_panels: bool) -> str:
    """Short slug naming the filter combination, in the human scorer's two-part shape.

    `pred-` rather than the human `calib-`/`prod-` first part, because the calibration
    distinction does not exist here: predictions carry no annotator, so nothing was
    double-annotated and there is no calibration population to keep or drop. The second part
    is the same retrieval panel-completeness choice, which does still apply.
    """
    return "pred-" + ("allpanels" if all_panels else "complete")


def discover_predictions(population: str, explicit: list[str]) -> dict[str, dict]:
    """task -> the prediction directory to score, and the workspace record that describes it.

    Prediction directories are named `<evaluator_run_id>-<population>` by
    predict_evaluators.predict, but the name is not parsed to find them: each one carries a
    `predict_provenance.workspace.json` naming its task, population and evaluator, so
    discovery reads the record rather than the filename. That matters because the evaluator run
    id is opaque and a hand-copied directory could disagree with its own contents.

    Ambiguity is refused rather than resolved by recency. Two evaluators predicting the same
    (task, population) is the normal state after a re-train, and picking one silently is how a
    published number ends up describing a model nobody chose - so it names both and asks for
    `--prediction-id`.
    """
    if not ec.PREDICTION_OUTPUTS.is_dir():
        raise SystemExit(
            f"no {ec.PREDICTION_OUTPUTS.relative_to(ws.ROOT)} - nothing has been predicted "
            "yet.\n  Run `make eval-predict TASK=<task> POPULATION="
            f"{population}` on the GPU box first, or pull the tree\n  "
            "(make transfer-pull PREFIX=predictions)."
        )

    candidates: dict[str, list[tuple[Path, dict]]] = {task: [] for task in ec.TASKS}
    for record_path in sorted(
        ec.PREDICTION_OUTPUTS.glob("*/predict_provenance.workspace.json")
    ):
        record = json.loads(record_path.read_text(encoding="utf-8"))
        prediction_dir = record_path.parent
        if explicit and prediction_dir.name not in explicit:
            continue
        if not explicit and record.get("population") != population:
            continue
        task = record.get("task")
        if task in candidates:
            candidates[task].append((prediction_dir, record))

    missing = [
        name
        for name in explicit
        if not any(d.name == name for pairs in candidates.values() for d, _ in pairs)
    ]
    if missing:
        raise SystemExit(
            f"no prediction directory named {', '.join(missing)} under "
            f"{ec.PREDICTION_OUTPUTS.relative_to(ws.ROOT)},\n"
            "  or it carries no predict_provenance.workspace.json. `make eval-predict` writes "
            "one into every\n  directory it files; a tree that has none was not produced by it."
        )

    resolved: dict[str, dict] = {}
    for task, pairs in candidates.items():
        if not pairs:
            continue
        if len(pairs) > 1:
            names = ", ".join(sorted(d.name for d, _ in pairs))
            raise SystemExit(
                f"{len(pairs)} prediction directories hold {task}/{population}: {names}.\n"
                "  Which evaluator the published number describes cannot be picked by "
                "recency - pass\n  --prediction-id <dir> for each task you mean to score."
            )
        prediction_dir, record = pairs[0]
        # An explicit --prediction-id skips the population filter above, so that a directory
        # can be scored by name. It must still BE this population: the output filename and the
        # score-run directory are both named for --population, and a row labelled with its own
        # record's population inside a file named for another is the kind of mismatch nobody
        # spots until the numbers are in a table.
        if record.get("population") != population:
            raise SystemExit(
                f"{prediction_dir.name} holds population "
                f"{record.get('population')!r}, but --population says {population!r}.\n"
                "  The output file is named for --population, so scoring them together would "
                "mislabel the file."
            )
        resolved[task] = {"dir": prediction_dir, "record": record}
    if not resolved:
        raise SystemExit(
            f"no prediction directory for population {population!r} under "
            f"{ec.PREDICTION_OUTPUTS.relative_to(ws.ROOT)}."
        )
    return resolved


def unscoreable_labels(prediction_dir: Path, task: str) -> list[str]:
    """The task's label columns the prediction does not carry, which scoring requires.

    This is grounding, concretely. It trains on three of its five labels - `support_present`
    and `source_cited` have too few negative items for any split to give tlmtc class support,
    see docs/eval-training.md - so its predictions.csv has three label columns. pragmata's
    GROUNDING_SCORE_SCHEMA requires all five, built from LABEL_COLUMNS_BY_TASK at module
    IMPORT time, so the narrowing that makes training possible cannot reach it and the score
    CLI rejects the frame outright.

    Checked from the header here rather than left to that rejection, because the rejection
    arrives as a pandera contract error that reads like a bug in staging. The right output is
    an explicit n=0 row per affected metric saying the evaluator does not cover the label -
    which is a finding about the evaluator, and one the eval report already documents.
    """
    # One line, not read_text(): a corpus predictions.csv carries every chunk's text and runs
    # to tens of MB, and only the header is wanted. Label columns are appended by tlmtc and are
    # bare identifiers, so splitting the header on commas is safe where splitting a data row
    # would not be.
    with (prediction_dir / "predictions.csv").open(encoding="utf-8") as handle:
        header = handle.readline()
    present = {column.strip().strip('"') for column in header.split(",")}
    return [label for label in ec.LABELS[task] if label not in present]


def run_score(pin, prediction_id: str, task: str, score_id: str, args) -> Path:
    """Invoke `pragmata eval score --prediction-id` on a prediction dir; return the report path.

    The subprocess mechanics - PYTHONPATH shadow, base_dir, the stale-report mtime guard -
    live in ec.run_score_cli, shared with the human scorer. `--prediction-id` in place of
    `--path` is the substance: pragmata resolves it to the run's `predictions.csv`, records
    `source.kind=model_prediction` on the report, and - because of that kind - renames tlmtc's
    generic `text`/`text_pair` columns back to the task's own before validating. Passing
    `--path predictions.csv` instead would skip exactly that step and fail the score contract.
    """
    return ec.run_score_cli(
        pin,
        ["--prediction-id", prediction_id],
        task,
        score_id,
        args,
        context=f"{score_id}/{task} (prediction {prediction_id})",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--population",
        choices=["annotated", "corpus"],
        default="annotated",
        help="Which predicted population to score (default: annotated).",
    )
    ap.add_argument(
        "--prediction-id",
        action="append",
        default=[],
        metavar="DIR",
        help=(
            "Prediction directory name under data/eval/prediction_outputs/ to score. "
            "Repeatable, one per task. Default: discovered from each directory's own "
            "workspace record for the chosen population."
        ),
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory (default: reports/eval/<today>/).",
    )
    ap.add_argument(
        "--all-panels",
        action="store_true",
        help="Score every retrieval panel, not just complete ones.",
    )
    ap.add_argument(
        "--ci", type=float, default=0.95, help="Confidence level (default 0.95)."
    )
    ap.add_argument(
        "--n-resamples",
        type=int,
        default=1000,
        help="Bootstrap iterations for the continuous retrieval metrics.",
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Bootstrap RNG seed; fixed so intervals are reproducible.",
    )
    ap.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Score even if the pragmata pin is dirty or names no commit at all.",
    )
    args = ap.parse_args()

    pin = ws.eval_pragmata()
    ec.require_clean_eval_pin(pin, allow_dirty=args.allow_dirty)

    policy = policy_name(args.all_panels)
    predictions = discover_predictions(args.population, args.prediction_id)
    rows: list[dict] = []
    inputs: list[Path] = []
    per_task_record: dict[str, dict] = {}

    for task in ec.TASKS:
        found = predictions.get(task)
        if found is None:
            print(f"  {task}: no prediction for this population", file=sys.stderr)
            continue
        prediction_dir, record = found["dir"], found["record"]
        prediction_id = prediction_dir.name
        identity = {
            "evaluator_run_id": record.get("evaluator_run_id", ""),
            "prediction_id": prediction_id,
            "population": record.get("population", args.population),
        }
        per_task_record[task] = record
        inputs.append(prediction_dir / "predictions.csv")

        absent = unscoreable_labels(prediction_dir, task)
        if absent:
            print(
                f"  {task}: not scored - the evaluator predicts no "
                f"{', '.join(absent)}, which pragmata's score contract requires",
                file=sys.stderr,
            )
            rows.extend(
                {**row, **identity}
                for row in human.empty_rows(task, policy, "evaluator_labels_incomplete")
            )
            continue

        # One score dir per (population, policy): it holds one JSON per task, exactly this
        # loop, and must not collide with the human scorer's own per-policy dirs.
        score_id = f"synthetic-{args.population}-{policy}"
        report_path = run_score(pin, prediction_id, task, score_id, args)
        report = json.loads(report_path.read_text())
        # alphas={} deliberately: attach_alpha then fills `source_labels` - which labels the
        # metric reads - and leaves the alpha fields untouched, and this CSV has no columns for
        # them anyway. Reusing the human row builder is what keeps the two files comparable.
        rows.extend(
            {**row, **identity}
            for row in human.rows_from_report(report, task, policy, {})
        )
        print(
            f"  {task}: scored n={report.get('n_examples')} from {prediction_id}",
            file=sys.stderr,
        )

    target = (
        ws.stage_report_dir("eval", args.out_dir)
        / f"synthetic_metric_estimates.{args.population}.csv"
    )
    ws.write_csv(
        target,
        rows,
        columns=COLUMNS,
        prov=ws.provenance(
            script="scripts/eval/score_synthetic_predictions.py",
            inputs=inputs,
            pragmata_src=pin.src,
            population=args.population,
            policy=policy,
            grain="task x metric, over one predicted population",
            predictions={
                task: {
                    "prediction_id": record.get("prediction_id"),
                    "evaluator_run_id": record.get("evaluator_run_id"),
                    "evaluator_label_names": record.get("evaluator_label_names"),
                    "input_csv": record.get("input_csv"),
                    "input_csv_sha256": record.get("input_csv_sha256"),
                    "input_provenance": record.get("input_provenance"),
                }
                for task, record in per_task_record.items()
            },
            filters={
                "retrieval_panels": (
                    "all (--allow-incomplete-panels)"
                    if args.all_panels
                    else "incomplete skipped by pragmata (--skip-incomplete-panels)"
                ),
            },
            ci_level=args.ci,
            n_resamples=args.n_resamples,
            seed=args.seed,
        ),
    )
    print(
        f"wrote {target} ({len(rows)} rows, population={args.population}, policy={policy})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
