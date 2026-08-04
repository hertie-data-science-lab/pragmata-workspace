#!/usr/bin/env python3
"""Train the synthetic evaluators - the recommended config per task, plus its diagnostics.

The workspace side of `pragmata eval train-evaluator`. Stages the pooled per-task training
CSVs out of the frozen canonical export, then trains one evaluator per task at the
configuration each task's numbers were established under. Runs on the GPU box: the training
extra is not in this workspace's lock, see [Eval training](../../docs/eval-training.md).

The per-task configuration lives in configs/eval/training/ - a shared `_common.yaml`
deep-merged with one file per task, mirroring configs/annotation/querygen_specs/. Those values
are pins behind published numbers, so they are committed data rather than code, and each one
is documented beside itself, including the levers tested and found not to help.

Usage:
  scripts/eval/train_evaluators.py combine                 # -> data/eval-inputs/training/
  scripts/eval/train_evaluators.py check-sequence-length   # diagnostic, trains nothing
  scripts/eval/train_evaluators.py train <task>            # [--threshold-type label|global]

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

# Per-task training config, mirroring configs/annotation/querygen_specs/: a shared
# underscore-prefixed file deep-merged with one file per unit. The values are pins behind
# published numbers, so they are committed data rather than code - the same reasoning that
# puts the freeze pin in configs/eval/freeze.conf.
TRAINING_CONFIGS = ws.ROOT / "configs" / "eval" / "training"
COMMON_CONFIG = TRAINING_CONFIGS / "_common.yaml"

SEQUENCE_LIMITS = (1024, 1536, 2048, 3072, 4096, 6144, 8192)


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


def _task_config(task: str) -> dict:
    """_common.yaml deep-merged with <task>.yaml, task values winning.

    Merged with pragmata's own deep_merge, so the result is identical to what its layered
    config resolution would produce - the same reasoning as scripts/annotation/merge_yaml.py,
    which composes querygen's _runtime.yaml with each spec. Merged here rather than written to
    a temp file and passed as config_path, because the caller has to supply base_dir and
    labeled_data_path as overrides anyway and train_evaluator deep-merges overrides too.
    """
    import yaml
    from pragmata.core.settings.settings_base import deep_merge

    task_config = TRAINING_CONFIGS / f"{task}.yaml"
    merged: dict = {}
    for path in (COMMON_CONFIG, task_config):
        if not path.exists():
            raise SystemExit(
                f"missing training config {path.relative_to(ws.ROOT)}.\n"
                "  Each task needs one, deep-merged over _common.yaml. Restore it from git."
            )
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise SystemExit(f"{path}: YAML root must be a mapping")
        merged = deep_merge(merged, data)

    declared = merged.pop("task", None)
    if declared is not None and declared != task:
        raise SystemExit(
            f"{task_config.relative_to(ws.ROOT)} declares task: {declared}, "
            f"but was loaded for {task}."
        )
    return merged


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
    """Report how much of each task's input the configured sequence_length truncates.

    Trains nothing. Worth re-running whenever the export moves materially: truncation is
    silent, and grounding was found to be 100% truncated at the 1024 default, with a median
    need of ~4,100 tokens - the model never saw a complete grounding input.

    Measures what training will actually tokenize, by running the rows through pragmata's own
    import and transform rather than re-deriving them here. That matters twice over: which
    columns become `text`/`text_pair` is pragmata's decision (TEXT_COLUMNS_BY_TASK), and
    build_tlmtc_frame consolidates each item's responses by majority first - so the row count
    it yields is the grain the model sees, not the per-response grain of the staged CSV. An
    earlier version restated the column pairs and measured pre-consolidation rows, which made
    both the percentages and the row counts describe a dataset that never reaches tlmtc.
    """
    from transformers import AutoTokenizer

    # Task is pragmata's own enum; the binding keeps its class name deliberately.
    _eval_api, Task = _pragmata_eval()
    from pragmata.core.eval.imports import import_eval_train_frame
    from pragmata.core.eval.transforms import build_tlmtc_frame

    for task in ec.TASKS:
        # The checkpoint comes from the merged config, so the diagnostic always measures the
        # tokenizer the run will use rather than a copy of pragmata's default.
        config = _task_config(task)
        checkpoint = config.get("checkpoint")
        if not checkpoint:
            raise SystemExit(
                f"no `checkpoint` in {COMMON_CONFIG.relative_to(ws.ROOT)} - the "
                "diagnostic measures the configured tokenizer and will not guess one."
            )
        configured = config.get("sequence_length")

        task_enum = Task(task)
        frame = import_eval_train_frame(path=_training_csv(task), task=task_enum)
        tlmtc_frame = build_tlmtc_frame(frame, task=task_enum, mode="train")

        tokenizer = AutoTokenizer.from_pretrained(checkpoint)
        lengths = tlmtc_frame.apply(
            lambda row, tok=tokenizer: len(
                tok.encode(str(row["text"]), str(row["text_pair"]))
            ),
            axis=1,
        )

        print(f"--- {task} ({len(tlmtc_frame)} items, {checkpoint}) ---")
        print(f"  median {int(lengths.median())} tokens, max {int(lengths.max())}")
        for limit in SEQUENCE_LIMITS:
            marker = "  <- configured" if limit == configured else ""
            print(
                f"  seq_len={limit}: {(lengths > limit).mean():.1%} still truncated{marker}"
            )
        print()
    return 0


def train(task: str, threshold_type: str | None = None) -> int:
    """Train one task's evaluator at its committed configuration.

    The three tasks differ only in configuration, so they share this one path. What each
    value is and why it was chosen - including the levers tested and found not to help - is
    documented beside the values in configs/eval/training/, not restated here.

    Two things cannot be configuration and so are handled in code:

    - ``base_dir`` and ``labeled_data_path`` are machine-dependent, so they must not be
      committed to a config file; they are passed as overrides, which pragmata deep-merges
      over the config layer.
    - Grounding's label narrowing reassigns a pragmata module-level mapping before the schema
      is read. EvalTrainSettings forbids extra keys, so it could not live in the YAML even as
      an ignored field.
    """
    eval_api, Task = _pragmata_eval()
    config = _task_config(task)

    if threshold_type is not None:
        config.setdefault("train_kwargs", {})["threshold_type"] = threshold_type
        print(f"threshold_type overridden to {threshold_type}", file=sys.stderr)

    if task == "grounding":
        # Must happen before train_evaluator reads the schema; see GROUNDING_TRAIN_LABELS.
        from pragmata.core.schemas import eval_input

        eval_input.LABEL_COLUMNS_BY_TASK[Task.GROUNDING] = GROUNDING_TRAIN_LABELS
        print(
            f"grounding labels narrowed to: {', '.join(GROUNDING_TRAIN_LABELS)}",
            file=sys.stderr,
        )

    csv_path = _training_csv(task)
    print(
        f"training {task} from {csv_path.relative_to(ws.ROOT)} "
        f"(seq_len={config.get('sequence_length', 1024)})",
        file=sys.stderr,
    )
    result = _train(
        eval_api,
        base_dir=str(ws.DATA_DIR),
        labeled_data_path=str(csv_path),
        task=Task(task),
        **config,
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

    train_parser = sub.add_parser(
        "train",
        help="Train one task's evaluator (grounding is slow, 2+ hours)",
    )
    train_parser.add_argument("task", choices=list(ec.TASKS))
    train_parser.add_argument(
        "--threshold-type",
        choices=["label", "global"],
        default=None,
        help=(
            "Override threshold_optimization's mode for this run. Default: whatever the "
            "task's config pins (global for retrieval; the other two do not use it)."
        ),
    )

    args = parser.parse_args()

    if args.command == "combine":
        return combine(args.exports)
    if args.command == "check-sequence-length":
        return check_sequence_length()
    return train(args.task, args.threshold_type)


if __name__ == "__main__":
    raise SystemExit(main())
