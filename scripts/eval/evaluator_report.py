#!/usr/bin/env python3
"""How good the synthetic evaluators are - the two deliverable CSVs about the models.

Columns and caveats are defined in `docs/data-dictionary.md` (`evaluator_metrics.csv`,
`evaluator_calibration.csv`). These describe the EVALUATORS, not the populations they judge:
the curated-corpus estimates are `eval_metric_estimates.csv` (human labels) and
`synthetic_metric_estimates.csv` (the evaluators applied to a population). Read those two
beside this one - an all-items rate from a model with an AUC near chance is not a measurement.

Both are computed on each run's OWN held-out test split, the split tlmtc cut and reported on,
so they are the same population the training run's own metrics describe.

| Subcommand | Reads | Needs |
|---|---|---|
| `metrics` | `evaluation/label_metrics.json` + `data/test.parquet` per run | nothing but the run dirs |
| `calibration` | per-item probabilities, so it re-predicts each test split | the GPU environment |

Usage:
  scripts/eval/evaluator_report.py metrics
  scripts/eval/evaluator_report.py calibration --run-id retrieval=<id>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import eval_common as ec
import predict_evaluators as pe
import workspace as ws

METRICS_COLUMNS = [
    "task",
    "label",
    # The evaluator training run id, not a human-readable configuration name. Opaque on
    # purpose: it is the join key to that run's own records - train_provenance.workspace.json
    # beside the checkpoints, and the prediction directories named after it - and a friendly
    # label would name a configuration rather than the run that produced these numbers.
    "training",
    "roc_auc",
    "accuracy",
    "f1",
    "precision",
    "recall",
    "n",
]

CALIBRATION_COLUMNS = ["task", "label", "prob_bin", "mean_pred", "frac_true", "n"]

# Ten fixed-width bins over [0, 1]. Fixed rather than quantile bins because the point is
# whether a stated probability means what it says - which is a claim about the value, not
# about its rank - and because fixed edges make two runs' rows line up. The top bin is closed
# so a probability of exactly 1.0 has somewhere to go.
N_BINS = 10

# How far the reconstructed confusion counts may sit from whole numbers before the derivation
# is treated as invalid rather than as rounding. The inputs are float32 metrics read back from
# JSON, so exact integers are not expected; anything past this means the algebra below does
# not describe how these numbers were produced.
INTEGER_TOLERANCE = 0.01

# The row-index column carried through prediction so per-item probabilities can be joined back
# to the test split's own labels. Not `index`: tlmtc and pandas both use that name, and a
# collision would be silent. Not a label_* name either - the predict contract rejects those.
ROW_INDEX_COLUMN = "workspace_row_index"


def _run_ids(pairs: list[str]) -> dict[str, str]:
    """`--run-id task=id` occurrences as a {task: run_id} mapping."""
    resolved: dict[str, str] = {}
    for pair in pairs:
        task, _, run_id = pair.partition("=")
        if task not in ec.TASKS or not run_id:
            raise SystemExit(
                f"--run-id expects <task>=<run_id> with task in "
                f"{', '.join(ec.TASKS)}; got {pair!r}"
            )
        resolved[task] = run_id
    return resolved


def _test_split(run_dir: Path) -> pd.DataFrame:
    """A run's held-out test split, as tlmtc prepared it.

    `data/test.parquet` inside the run directory: text, text_pair and one `label_*` column per
    trained label, already majority-consolidated and split. It is the population every number
    in both CSVs describes, which is why the row count comes from here rather than from the
    staged input CSV - the input is the whole pooled export, of which this is roughly a fifth.
    """
    path = run_dir / "data" / "test.parquet"
    if not path.is_file():
        raise SystemExit(
            f"no test split at {path}.\n"
            "  Every tlmtc run writes data/{{train,val,test}}.parquet; a run directory "
            "without them is\n  incomplete - re-pull the checkpoints, or re-train."
        )
    return pd.read_parquet(path)


def _label_metrics(run_dir: Path) -> dict[str, dict]:
    """A run's per-label test metrics, as tlmtc reported them."""
    path = run_dir / "evaluation" / "label_metrics.json"
    if not path.is_file():
        raise SystemExit(
            f"no per-label metrics at {path}.\n"
            "  tlmtc writes evaluation/label_metrics.json for every completed run."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _derived_accuracy(stats: dict, n: int, task: str, label: str) -> tuple[str, str]:
    """(accuracy, note) for one label, reconstructed from the metrics tlmtc does persist.

    **Accuracy is not in `label_metrics.json`**, so it is derived rather than read. The
    reconstruction is exact, not an approximation, because the persisted quantities pin the
    whole 2x2 table on a population of known size:

        P  = true_prevalence * n          positives in the test split
        TP = recall * P                   recall = TP / P
        PP = pred_prevalence * n          rows the evaluator called positive
        FP = PP - TP
        FN = P - TP
        TN = n - TP - FP - FN
        accuracy = (TP + TN) / n

    FP comes from `pred_prevalence` rather than from precision (`TP / precision - TP`), for
    two reasons. Division by a rounded ratio is the shakier arithmetic; and precision is 0.0
    both when the evaluator called nothing positive (0/0, reported as 0) and when everything
    it called positive was wrong (a genuine zero) - two states this table must not conflate,
    which `pred_prevalence` tells apart directly.

    Every count must come out a whole number, since they are counts of test rows, and each is
    checked against INTEGER_TOLERANCE. A miss means the metrics were not computed the way
    this algebra assumes - a different denominator, a different split - and publishing a
    plausible-looking accuracy derived from the wrong model of them is worse than failing, so
    it aborts naming the label and the residual.

    The one blank is `pred_prevalence == 0`: the evaluator never predicts the label positive.
    Accuracy IS still determined there (TP = FP = 0, so accuracy = 1 - true_prevalence), but
    it is left blank deliberately: an accuracy filled in for a label the model never predicts
    reads as performance, when what it measures is the prevalence of the negative class. The
    blank plus the f1/precision/recall zeros beside it say what happened.
    """
    prevalence = float(stats["true_prevalence"])
    positives = prevalence * n

    def _check(value: float, name: str) -> float:
        nearest = round(value)
        if abs(value - nearest) > INTEGER_TOLERANCE:
            raise SystemExit(
                f"{task}/{label}: the accuracy derivation does not reconstruct whole test "
                f"rows - {name} came out {value:.4f}, {abs(value - nearest):.4f} from the "
                f"nearest integer (tolerance {INTEGER_TOLERANCE}).\n"
                f"  n={n} true_prevalence={prevalence!r} recall={stats['recall']!r} "
                f"precision={stats['precision']!r}\n"
                "  These metrics were not computed over the population this assumes. Fix "
                "_derived_accuracy\n  rather than publishing an accuracy derived from the "
                "wrong model of them."
            )
        return float(nearest)

    positives = _check(positives, "true_prevalence * n")
    true_positives = _check(float(stats["recall"]) * positives, "recall * positives")

    predicted_positives = _check(
        float(stats["pred_prevalence"]) * n, "pred_prevalence * n"
    )
    if predicted_positives == 0:
        return "", "degenerate: the evaluator never predicts this label positive"

    false_positives = _check(predicted_positives - true_positives, "false positives")
    false_negatives = _check(positives - true_positives, "false negatives")
    true_negatives = _check(
        n - true_positives - false_positives - false_negatives, "true negatives"
    )
    return f"{(true_positives + true_negatives) / n:.6f}", ""


def metrics(run_ids: dict[str, str], out_dir: Path | None) -> int:
    """Write evaluator_metrics.csv - one row per task x label x training run.

    Reads only what a completed run left on disk, so it needs neither a GPU nor pragmata.
    """
    rows: list[dict] = []
    inputs: list[Path] = []
    resolved: dict[str, str] = {}
    notes: dict[str, str] = {}
    not_trained: dict[str, list[str]] = {}
    for task in ec.TASKS:
        run = ec.resolve_evaluator_run(task, run_ids.get(task))
        origin = "given" if task in run_ids else "latest for this task"
        print(f"{task}: run {run.run_id} ({origin})", file=sys.stderr)
        resolved[task] = run.run_id
        per_label = _label_metrics(run.run_dir)
        n = len(_test_split(run.run_dir))
        inputs += [
            run.run_dir / "evaluation" / "label_metrics.json",
            run.run_dir / "data" / "test.parquet",
        ]
        # Iterated over the run's OWN labels rather than over ec.LABELS[task]: grounding trains
        # on three of its five, and a row for a label the run never had would be a blank line
        # that reads as a measurement. Which labels are missing is a property of the run and is
        # said in the docs and the sidecar, not implied by empty cells.
        for label, stats in per_label.items():
            accuracy, note = _derived_accuracy(stats, n, task, label)
            if note:
                notes[f"{task}/{label}"] = note
            rows.append(
                {
                    "task": task,
                    "label": label,
                    "training": run.run_id,
                    "roc_auc": f"{float(stats['roc_auc']):.6f}",
                    "accuracy": accuracy,
                    "f1": f"{float(stats['f1']):.6f}",
                    "precision": f"{float(stats['precision']):.6f}",
                    "recall": f"{float(stats['recall']):.6f}",
                    "n": n,
                }
            )
        not_trained[task] = [lab for lab in ec.LABELS[task] if lab not in per_label]
        if not_trained[task]:
            print(
                f"  {task}: no rows for {', '.join(not_trained[task])} - not trained "
                "(see docs/synthetic-evaluators.md)",
                file=sys.stderr,
            )

    target = ws.deliverable_path("eval", "evaluator_metrics.csv", out_dir)
    ws.write_csv(
        target,
        rows,
        columns=METRICS_COLUMNS,
        prov=ws.provenance(
            script="scripts/eval/evaluator_report.py",
            inputs=inputs,
            grain="task x label x training run",
            evaluator_runs=resolved,
            population="each run's own held-out test split (data/test.parquet)",
            metrics_source="tlmtc evaluation/label_metrics.json",
            # Spelled out in the record as well as in the code, because a derived column that
            # is not in any input is the one thing a reader of the CSV cannot check.
            accuracy_derivation=(
                "not persisted by tlmtc; reconstructed from the same run's "
                "true_prevalence, recall and pred_prevalence over n test rows: "
                "P=true_prevalence*n, TP=recall*P, PP=pred_prevalence*n, FP=PP-TP, "
                "FN=P-TP, TN=n-TP-FP-FN, accuracy=(TP+TN)/n. Every count is checked "
                f"to be whole within {INTEGER_TOLERANCE} test rows or the run aborts."
            ),
            accuracy_blank_reason=(
                "pred_prevalence == 0 (the evaluator never predicts this label "
                "positive). Accuracy is determined there (1-true_prevalence) but left "
                "blank rather than filled in, which would read as performance."
            ),
            accuracy_blank_labels=sorted(notes),
            # Recorded rather than implied by absent rows: "this evaluator does not cover the
            # label" and "the label was not measured" read the same way in a CSV.
            labels_not_trained=not_trained,
        ),
    )
    print(f"wrote {target} ({len(rows)} rows)", file=sys.stderr)
    return 0


def _stage_test_split(task: str, run) -> Path:
    """Stage one run's test split as an unlabelled CSV, and return the path.

    The parquet is tlmtc-shaped - `text`, `text_pair`, `label_*` - and the prediction contract
    is pragmata-shaped and refuses labels, so three things change:

    - `text`/`text_pair` become the task's own column names, which `build_tlmtc_frame` renames
      straight back. Round-tripping rather than passing the tlmtc names through is not
      pedantry: `build_tlmtc_frame` REFUSES an input that already contains its output columns
      (it derives them, and a pre-existing one would mean the caller had done half the job).
    - every `label_*` column is dropped. It is the held-out truth, and the predict contract
      rejects it outright - correctly, since a model must not see the answers.
    - a row index is added, because dropping the labels also drops the only way to line the
      probabilities back up against them. tlmtc preserves input columns through prediction, so
      this column comes back on `probabilities.csv` and the join is exact rather than positional.

    `split_group` goes too where retrieval has it: it is the splitter's grouping key, spent
    once at training time, and carrying it into a prediction input would only invite it being
    read as identity.

    Written under data/eval-inputs/predict/testsplit/ with the same provenance sidecar the
    other populations get, so `predict` applies one freshness rule to all three.
    """
    frame = _test_split(run.run_dir)
    text_column, text_pair_column = ec.TEXT_COLUMNS[task]
    dropped = [c for c in frame.columns if c.startswith("label_")] + [
        c for c in ("split_group",) if c in frame.columns
    ]
    unlabeled = frame.drop(columns=dropped).rename(
        columns={"text": text_column, "text_pair": text_pair_column}
    )
    unlabeled.insert(0, ROW_INDEX_COLUMN, range(len(unlabeled)))

    target = pe.PREDICT_INPUTS / "testsplit" / f"{task}.csv"
    pe.write_staged(
        target,
        unlabeled,
        {
            "inputs": [run.run_dir / "data" / "test.parquet"],
            "task": task,
            "population": "testsplit",
            "evaluator_run_id": run.run_id,
            "grain": "one row per test-split row",
            "labels_dropped": dropped,
            "row_index_column": ROW_INDEX_COLUMN,
        },
    )
    print(
        f"  staged {target.relative_to(ws.ROOT)} ({len(unlabeled)} rows)",
        file=sys.stderr,
    )
    return target


def _bin_label(index: int) -> str:
    """`[0.0,0.1)` ... `[0.9,1.0]` - the closed top bin is the only asymmetry."""
    low = index / N_BINS
    high = (index + 1) / N_BINS
    closer = "]" if index == N_BINS - 1 else ")"
    return f"[{low:.1f},{high:.1f}{closer}"


def calibration(
    run_ids: dict[str, str],
    out_dir: Path | None,
    *,
    use_cpu: bool,
    batch_size: int | None,
) -> int:
    """Write evaluator_calibration.csv - reliability per task x label x probability bin.

    Needs per-item probabilities, which no training artifact holds: tlmtc persists aggregate
    and per-label metrics, not the per-row scores behind them. So each run is re-applied to its
    own test split through the same prediction plumbing everything else uses - staged input,
    freshness sidecar, population-named output directory - with `testsplit` as the population.
    That reuse is the point: the probabilities behind this CSV are produced the way the
    published predictions are, not by a private code path that could drift from them.

    The one place it departs from `make eval-predict` is the collision guard, which it passes
    ``overwrite=True``. That guard exists so a published prediction cannot be silently replaced
    by a different population's; the `testsplit` prediction is neither published nor a different
    population - this subcommand stages its input itself, deterministically, from the same
    parquet every time - so refusing a re-run would only be friction on regenerating the
    deliverable.
    """
    rows: list[dict] = []
    inputs: list[Path] = []
    resolved: dict[str, str] = {}
    for task in ec.TASKS:
        run = ec.resolve_evaluator_run(task, run_ids.get(task))
        origin = "given" if task in run_ids else "latest for this task"
        print(f"{task}: run {run.run_id} ({origin})", file=sys.stderr)
        resolved[task] = run.run_id
        # Up front, because it decides which columns of probabilities.csv are labels at all -
        # and because the alternative is spending a GPU pass to then bin nothing.
        if not run.label_names:
            raise SystemExit(
                f"{task}: run {run.run_id} names no label_names in its "
                "train_run_meta.json, so\n"
                "  which columns of probabilities.csv are labels cannot be established. "
                "tlmtc writes that\n  file for every completed run - re-pull the checkpoints."
            )

        _stage_test_split(task, run)
        prediction_dir = pe.predict(
            task,
            "testsplit",
            evaluator_run_id=run.run_id,
            use_cpu=use_cpu,
            batch_size=batch_size,
            overwrite=True,
        )
        probabilities = pd.read_csv(prediction_dir / "probabilities.csv")
        truth = _test_split(run.run_dir)
        inputs += [
            prediction_dir / "probabilities.csv",
            run.run_dir / "data" / "test.parquet",
        ]

        # Joined on the passthrough index, not zipped by position. tlmtc does preserve row
        # order today, but a probabilities file that had been reordered or partially written
        # would then pair each probability with the wrong row's label - silently, and the
        # resulting curve would look like a miscalibrated model rather than a broken join.
        if ROW_INDEX_COLUMN not in probabilities.columns:
            raise SystemExit(
                f"{prediction_dir / 'probabilities.csv'} carries no {ROW_INDEX_COLUMN} "
                "column, so its rows\n"
                "  cannot be matched to the test split's labels. It was predicted from a "
                "differently staged\n  input - re-run this subcommand, which stages it."
            )
        joined = probabilities.set_index(ROW_INDEX_COLUMN).sort_index()
        if len(joined) != len(truth) or list(joined.index) != list(range(len(truth))):
            raise SystemExit(
                f"{task}: the prediction covers {len(joined)} row(s) of a {len(truth)}-row "
                "test split.\n"
                "  Every test row must be predicted exactly once, or the bins describe a "
                "different population\n  than the one they claim."
            )

        for label in run.label_names:
            if label not in joined.columns:
                raise SystemExit(
                    f"{task}: probabilities.csv has no {label!r} column, though the run's "
                    "metadata names it."
                )
            probability = joined[label].to_numpy(dtype=float)
            actual = truth[f"label_{label}"].to_numpy(dtype=float)
            # floor(p * 10), with 1.0 folded into the top bin rather than a bin of its own.
            index = (probability * N_BINS).astype(int).clip(0, N_BINS - 1)
            for bin_index in range(N_BINS):
                mask = index == bin_index
                count = int(mask.sum())
                if not count:
                    # Empty bins are skipped rather than written as zeros: a row of zeros
                    # reads as "the model was right 0% of the time here", which is a claim,
                    # where an absent row is the absence of evidence it actually is.
                    continue
                rows.append(
                    {
                        "task": task,
                        "label": label,
                        "prob_bin": _bin_label(bin_index),
                        "mean_pred": f"{probability[mask].mean():.6f}",
                        "frac_true": f"{actual[mask].mean():.6f}",
                        "n": count,
                    }
                )
        print(
            f"  {task}: {len(truth)} test rows binned over "
            f"{len(run.label_names)} label(s)",
            file=sys.stderr,
        )

    target = ws.deliverable_path("eval", "evaluator_calibration.csv", out_dir)
    ws.write_csv(
        target,
        rows,
        columns=CALIBRATION_COLUMNS,
        prov=ws.provenance(
            script="scripts/eval/evaluator_report.py",
            inputs=inputs,
            grain="task x label x probability bin",
            evaluator_runs=resolved,
            population="each run's own held-out test split (data/test.parquet)",
            bins=f"{N_BINS} fixed-width bins over [0,1]; the top bin is closed",
            empty_bins="skipped, not written as zero-count rows",
            join=f"probabilities joined to labels on the {ROW_INDEX_COLUMN} passthrough column",
            probabilities_source=(
                "re-predicted through scripts/eval/predict_evaluators.py with "
                "population=testsplit; tlmtc persists no per-item scores"
            ),
        ),
    )
    print(f"wrote {target} ({len(rows)} rows)", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def add_shared(target) -> None:
        target.add_argument(
            "--run-id",
            action="append",
            default=[],
            metavar="TASK=ID",
            help=(
                "Evaluator run to report for one task, e.g. retrieval=<id>. Repeatable. "
                "Default: the latest run for each task, printed and recorded."
            ),
        )
        target.add_argument(
            "--out-dir",
            type=Path,
            default=None,
            help=(
                "Run root for the deliverable set; each CSV lands in its taxonomy "
                "subdirectory (default: reports/eval/<today>/)."
            ),
        )

    metrics_parser = sub.add_parser(
        "metrics", help="evaluator_metrics.csv - per-label test metrics (no GPU)"
    )
    add_shared(metrics_parser)

    calibration_parser = sub.add_parser(
        "calibration",
        help="evaluator_calibration.csv - reliability per probability bin (GPU)",
    )
    add_shared(calibration_parser)
    calibration_parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="tlmtc prediction batch size (default: tlmtc's own 32).",
    )
    calibration_parser.add_argument(
        "--use-cpu",
        action="store_true",
        help="Force CPU inference, skipping the GPU check. Slow; for smoke tests.",
    )

    args = parser.parse_args()
    run_ids = _run_ids(args.run_id)
    if args.command == "metrics":
        return metrics(run_ids, args.out_dir)
    return calibration(
        run_ids,
        args.out_dir,
        use_cpu=args.use_cpu,
        batch_size=args.batch_size,
    )


if __name__ == "__main__":
    raise SystemExit(main())
