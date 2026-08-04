#!/usr/bin/env python3
"""Train the synthetic evaluators - the recommended config per task, plus its diagnostics.

The workspace side of `pragmata eval train-evaluator`. Stages the pooled per-task training
CSVs out of the frozen canonical export, then trains one evaluator per task at the
configuration each task's numbers were established under. Runs on the GPU box: the training
extra is not in this workspace's lock, see [Eval training](../../docs/eval-training.md).

Every parameter below is a pin behind a published number rather than an operator knob, so it
lives here in code and not in configs/settings.conf. The comments say what was tried and
rejected as well as what was kept - re-deriving a dead end costs a GPU day.

Usage:
  scripts/eval/train_evaluators.py combine                 # -> data/eval-inputs/training/
  scripts/eval/train_evaluators.py check-sequence-length   # diagnostic, trains nothing
  scripts/eval/train_evaluators.py train-retrieval [--threshold-type label|global]
  scripts/eval/train_evaluators.py train-grounding
  scripts/eval/train_evaluators.py train-generation

Long training runs belong under nohup; grounding takes 2+ hours. See the doc.
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
import workspace as ws

# Staged pooled CSVs handed to train_evaluator, alongside the scoring stage's own staging
# root. Under data/eval-inputs/ and not data/eval/ for the reason score_human_annotations.py
# gives: data/eval/ is pragmata's tool tree and holds only what pragmata wrote there. The
# `training/` leaf keeps these apart from the scorer's per-policy dirs - the two are pooled
# under different filters and must not collide.
TRAINING_INPUTS = ws.DATA_DIR / "eval-inputs" / "training"

# The tokenizer the sequence-length diagnostic measures against. Deliberately the same
# default train_evaluator uses when no checkpoint is passed, so the numbers describe the run
# that will actually happen.
DEFAULT_CHECKPOINT = "jhu-clsp/mmBERT-base"

# The field pairs each task feeds the tokenizer, matching what pragmata's build_tlmtc_frame
# concatenates. Prose calls the `answer` column a *response* (see the data dictionary); the
# column name itself is `answer` and that is what is read here.
TASK_FIELDS: dict[str, tuple[str, str]] = {
    "retrieval": ("query", "chunk"),
    "grounding": ("answer", "context_set"),
    "generation": ("query", "answer"),
}

SEQUENCE_LIMITS = (1024, 1536, 2048, 3072, 4096, 6144, 8192)

# Shared across all three tasks. validation/test at 0.25 each leaves half for training and
# is what every published number used; the seed makes a split reproducible.
COMMON_TRAIN_KWARGS = {
    "use_cpu": False,
    "validation_size": 0.25,
    "test_size": 0.25,
    "verbosity": "quiet",
    "early_stopping_patience": 15,
    "train_epochs": 40,
    "random_seed": 42,
}

# Grounding trains on three of its five labels. support_present (672 positive / 2 negative)
# and source_cited (669 / 5) in the 2026-07-30 export have too few negatives for any split
# ratio to give tlmtc full class support per label across train/val/test, so it refuses the
# run outright. This is a data floor, not a tuning problem: the newer export added grounding
# rows and not one new negative for either label. Re-check on a future export - the counts
# are printed by `combine`.
#
# The dropped pair is exactly what grounding_presence_rate and citation_presence_rate rest
# on in score_human_annotations.py, so the trained evaluator covers strictly less than the
# human-label metrics do. That asymmetry is intended and worth remembering when comparing
# the two.
GROUNDING_TRAIN_LABELS = (
    "unsupported_claim_present",
    "contradicted_claim_present",
    "fabricated_source",
)


def _pragmata_eval():
    """Import the eval pin's pragmata API, or exit saying how to get one.

    Shadows the installed annotation pragmata - a frozen demo commit with no eval module -
    by putting the pin's src first on sys.path, the in-process equivalent of the PYTHONPATH
    that score_human_annotations.py hands its subprocess. Imported through here rather than
    at module scope so `--help` and `combine` cost nothing.
    """
    pin = ws.eval_pragmata()
    src = str(pin.src)
    if sys.path[0] != src:
        sys.path.insert(0, src)
    try:
        import pragmata.api.eval as eval_api
        from pragmata.core.schemas.annotation_task import Task
    except ImportError as exc:
        raise SystemExit(f"cannot import the eval API from {pin.src}: {exc}") from exc
    return eval_api, Task


def _train(eval_api, **kwargs):
    """Call train_evaluator, turning a missing training extra into an instruction.

    tlmtc is imported lazily inside pragmata's own adapter, not at api.eval import time, so
    the absence of the training stack only surfaces here - as a bare ImportError several
    frames deep, which reads as a bug rather than as an unconfigured environment.
    """
    try:
        return eval_api.train_evaluator(**kwargs)
    except ImportError as exc:
        raise SystemExit(
            f"evaluator training needs a dependency this environment lacks: {exc}\n"
            "  The `eval` extra (pragmata[eval] -> tlmtc[train]) is deliberately NOT in this\n"
            "  workspace's uv.lock - it pulls a CUDA torch build, and the lock freezes the\n"
            "  environment behind the published human-label numbers.\n"
            "  Training runs on the GPU host against its own environment; the install steps\n"
            "  are in docs/eval-training.md."
        ) from exc


def _training_csv(task: str) -> Path:
    """The staged pooled CSV for a task, or exit telling the caller to build it."""
    path = TRAINING_INPUTS / f"{task}.csv"
    if not path.exists():
        raise SystemExit(
            f"no training CSV at {path.relative_to(ws.ROOT)}.\n"
            "  Run `make eval-train-inputs` first - it pools the frozen export per task."
        )
    return path


def combine(exports: Path) -> int:
    """Pool the frozen per-programme export CSVs into one training CSV per task.

    Programmes come from the export tree rather than a hardcoded list, via
    ec.programmes(), which also drops EXCLUDED_PROGRAMMES and checks the freeze pin is not
    stale. That matters here beyond tidiness: an earlier version of this pipeline carried a
    hardcoded list naming `monitor`, which was never a domain - it was a throwaway export
    directory log.py used to write, picked up by anything globbing exports/*/ and silently
    double-counting the last domain in the loop with stale values (fixed upstream in
    09e2a9a). Deriving the list removes the possibility.

    Rows are filtered to submitted responses. Exports run with include_discarded=true and a
    discarded response is an abstention carrying no labels, so pooling raw rows trains on
    null labels.

    `source_domain` is always written, never trusted from the input: it is not an export
    column at all, and a stray pre-existing value would mis-attribute rows in any per-domain
    breakdown of the results.
    """
    programmes = ec.programmes(exports)
    print(f"pooling {len(programmes)} programme(s) from {exports}", file=sys.stderr)
    TRAINING_INPUTS.mkdir(parents=True, exist_ok=True)

    for task in ec.TASKS:
        frames = []
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
            print(f"  {programme}/{task}: {len(frame)} rows", file=sys.stderr)
            frames.append(frame)

        if not frames:
            raise SystemExit(
                f"no programme contributed any {task} rows from {exports}.\n"
                "  Nothing to train on - check the export tree is populated."
            )

        pooled = pd.concat(frames, ignore_index=True)
        target = TRAINING_INPUTS / f"{task}.csv"
        pooled.to_csv(target, index=False, encoding="utf-8")

        # A provenance sidecar even though these are staged inputs rather than deliverables:
        # a training run's own record names this CSV, so the CSV has to name the export rows
        # and the code that built it.
        prov = ws.provenance(
            script="scripts/eval/train_evaluators.py",
            inputs=[exports / p / f"{task}.csv" for p in programmes],
            task=task,
            programmes=programmes,
            excluded_programmes=sorted(ec.EXCLUDED_PROGRAMMES),
            freeze_date=ec.FREEZE_DATE,
            row_filter="submitted",
        )
        target.with_suffix(".csv.provenance.json").write_text(
            json.dumps(prov, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        print(
            f"wrote {target.relative_to(ws.ROOT)} "
            f"({len(pooled)} rows, {pooled['source_domain'].nunique()} domains)",
            file=sys.stderr,
        )
        # Label prevalence per task, because it is what decides whether a label is
        # trainable at all - grounding's two dropped labels are visible here.
        for label in ec.LABELS[task]:
            counts = pooled[label].value_counts(dropna=False).to_dict()
            n_true = int(counts.get(True, 0))
            n_false = int(counts.get(False, 0))
            print(
                f"    {label}: {n_true} positive / {n_false} negative", file=sys.stderr
            )
    return 0


def check_sequence_length() -> int:
    """Report how much of each task's input the default sequence_length truncates.

    Trains nothing. Worth re-running whenever the export moves materially: truncation is
    silent, and grounding was found to be 100% truncated at the 1024 default, with a median
    need of ~4171 tokens - the model never saw a complete grounding input.
    """
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(DEFAULT_CHECKPOINT)
    for task, (first, second) in TASK_FIELDS.items():
        frame = pd.read_csv(_training_csv(task))
        # The field names are bound as defaults rather than closed over: the lambda is
        # consumed immediately by apply(), but a late-binding closure over the loop
        # variables would silently measure the wrong columns if that ever stopped being true.
        lengths = frame.apply(
            lambda row, a=first, b=second: len(
                tokenizer.encode(f"{row.get(a, '')} {row.get(b, '')}")
            ),
            axis=1,
        )
        print(f"--- {task} ({first} + {second}, {len(frame)} rows) ---")
        print(f"  median {int(lengths.median())} tokens, max {int(lengths.max())}")
        for limit in SEQUENCE_LIMITS:
            print(f"  seq_len={limit}: {(lengths > limit).mean():.1%} still truncated")
        print()
    return 0


def train_retrieval(threshold_type: str) -> int:
    """Retrieval - the strongest result of the three, and the only one to trust outright.

    mmBERT (pragmata's own default, so no checkpoint override) + hyperparameter tuning +
    threshold optimization with best_model_metric pinned explicitly. Pinning that metric is
    load-bearing: letting threshold_optimization silently switch checkpoint selection from
    AUC to F1 is what made the same setting degenerate on the other two tasks.

    threshold_type selects label-specific or global thresholds. Both are legitimate -
    global reached roc_auc_macro 0.769, label-specific 0.752 with a better f1_macro (0.720
    vs 0.704). Global is the default on the primary metric.

    A checkpoint override is deliberately absent everywhere in this file. mmBERT-base beat
    answerdotai/ModernBERT-base on every task tested, most sharply on grounding, where it
    was part of what made training possible at all. Do not pass one without meaning to.
    """
    eval_api, Task = _pragmata_eval()
    result = _train(
        eval_api,
        base_dir=str(ws.DATA_DIR),
        labeled_data_path=str(_training_csv("retrieval")),
        task=Task.RETRIEVAL,
        train_kwargs={
            **COMMON_TRAIN_KWARGS,
            "hyperparameter_tuning": True,
            "threshold_optimization": True,
            "threshold_type": threshold_type,
            "best_model_metric": "roc_auc_macro",
        },
    )
    print(f"SUCCESS: {result.paths.run_dir}")
    return 0


def train_grounding() -> int:
    """Grounding - trainable only with the two unusable labels dropped and a long sequence.

    sequence_length=6144 against a median need of ~4171 tokens; the 1024 default truncated
    every single row. batch_size=1 is required at that length - batch_size=4 exhausted a
    40GB A100's memory in testing - which is what makes this the slow run, 2+ hours. Do not
    raise it without checking headroom first.

    Both extra levers were tested on this exact config and neither helped:
      - hyperparameter_tuning: macro AUC flat within noise, f1_macro worse.
      - threshold_optimization: the degenerate "predict positive almost everywhere" pattern
        (pred_prevalence 0.87-1.0 against a true 0.28), not a real precision/recall trade.
    Their absence below is deliberate.

    Only unsupported_claim_present has enough test-set support (31 positives) to trust.
    contradicted_claim_present and fabricated_source land 2-4 test positives, where one
    flipped prediction moves AUC by 0.2-0.3, so treat them as directional until a second
    seed or more annotation confirms them.
    """
    eval_api, Task = _pragmata_eval()

    # Narrow the label set before train_evaluator reads the schema. Done through the schema
    # module rather than a train_kwarg because pragmata exposes no per-run label override;
    # keep it adjacent to the import so nothing else observes the unpatched value.
    from pragmata.core.schemas import eval_input

    eval_input.LABEL_COLUMNS_BY_TASK[Task.GROUNDING] = GROUNDING_TRAIN_LABELS
    print(f"grounding labels: {', '.join(GROUNDING_TRAIN_LABELS)}", file=sys.stderr)

    result = _train(
        eval_api,
        base_dir=str(ws.DATA_DIR),
        labeled_data_path=str(_training_csv("grounding")),
        task=Task.GROUNDING,
        sequence_length=6144,
        train_kwargs={
            **COMMON_TRAIN_KWARGS,
            "hyperparameter_tuning": False,
            "threshold_optimization": False,
            "batch_size": 1,
        },
    )
    print(f"SUCCESS: {result.paths.run_dir}")
    return 0


def train_generation() -> int:
    """Generation - the best config found, and still not a trustworthy evaluator.

    Kept for reproducibility, not recommended for production use. sequence_length=3072
    covers 100% of rows against the default's 84% and gave a small real gain, but
    majority-class collapse persists on proper_action and response_on_topic: their F1 reads
    0.94 and 0.88 while AUC sits at 0.55-0.57, i.e. the model is still largely predicting
    the majority class. Every lever tried - mmBERT, HPO, threshold optimization,
    oversampling, sequence length - moved it at most slightly.

    The cause is upstream of any model choice: minority-class scarcity (16 negative examples
    for response_on_topic in the whole training set) compounded by annotator agreement at or
    near zero for these labels. Neither is fixable here.

    Two more dead ends, so nobody re-derives them:
      - hyperparameter_tuning: f1_macro improved, AUC unchanged - a precision/recall
        rebalance, not better discrimination.
      - threshold_optimization: actively worse, via the checkpoint-selection switch above.
      - Oversampling minority rows by duplicating CSV lines: zero effect. pragmata
        deduplicates rows before training and silently undoes it.
    """
    eval_api, Task = _pragmata_eval()
    result = _train(
        eval_api,
        base_dir=str(ws.DATA_DIR),
        labeled_data_path=str(_training_csv("generation")),
        task=Task.GENERATION,
        sequence_length=3072,
        train_kwargs={
            **COMMON_TRAIN_KWARGS,
            "hyperparameter_tuning": False,
            "threshold_optimization": False,
            "batch_size": 4,
        },
    )
    print(f"SUCCESS: {result.paths.run_dir}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    combine_parser = sub.add_parser(
        "combine", help="Pool the frozen export into one training CSV per task"
    )
    ec.add_common_args(combine_parser)

    sub.add_parser(
        "check-sequence-length",
        help="Diagnostic: how much of each task's input the default truncates",
    )

    retrieval_parser = sub.add_parser(
        "train-retrieval", help="Train the retrieval evaluator"
    )
    retrieval_parser.add_argument(
        "--threshold-type",
        choices=["label", "global"],
        default="global",
        help="Threshold optimization mode (default: global, best AUC of the three).",
    )

    sub.add_parser(
        "train-grounding", help="Train the grounding evaluator (slow, 2+ hours)"
    )
    sub.add_parser(
        "train-generation", help="Train the generation evaluator (see docstring)"
    )

    args = parser.parse_args()

    if args.command == "combine":
        return combine(args.exports)
    if args.command == "check-sequence-length":
        return check_sequence_length()
    if args.command == "train-retrieval":
        return train_retrieval(args.threshold_type)
    if args.command == "train-grounding":
        return train_grounding()
    return train_generation()


if __name__ == "__main__":
    raise SystemExit(main())
