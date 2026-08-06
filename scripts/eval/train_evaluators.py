#!/usr/bin/env python3
"""Train the synthetic evaluators - the recommended config per task, plus its diagnostics.

The workspace side of `pragmata eval train-evaluator`. Stages the pooled per-task training
CSVs out of the frozen canonical export, then trains one evaluator per task at the
configuration each task's numbers were established under. Runs on the GPU box: the training
extra is not in this workspace's lock, see [Synthetic evaluators](../../docs/eval-synthetic-evaluator.md).

The per-task configuration lives in configs/eval/training/ - a shared `_common.yaml`
deep-merged with one file per task, mirroring configs/annotation/querygen_specs/. Those values
are pins behind published numbers, so they are committed data rather than code, and each one
is documented beside itself, including the levers tested and found not to help.

Usage:
  scripts/eval/train_evaluators.py combine                 # -> data/eval-inputs/training/
  scripts/eval/train_evaluators.py check-sequence-length   # diagnostic, trains nothing
  scripts/eval/train_evaluators.py train <task>            # retrieval: [--threshold-type ...]

Long training runs belong under nohup; grounding takes 2+ hours. See the doc.
"""

from __future__ import annotations

import argparse
import json
import os
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

# The Hugging Face cache, mirroring the Makefile's own `HF_HOME ?= $(CURDIR)/.hf` so a direct
# run of this script lands where a `make` run does. Not merely tidiness: on this shared box
# ~/.cache/huggingface was created root-owned mode 755, so the fallback cannot be written and
# the tokenizer download fails outright. In-tree it inherits the checkout's default ACL and the
# base model is fetched once for everyone. Set here rather than left to the Makefile because
# every other machine-dependent path in this pipeline resolves from the script's own location
# too - that is what makes `make` and a direct invocation behave identically. setdefault, not
# assignment, so an operator's own HF_HOME still wins, exactly as the Makefile's `?=` does; and
# at import time, because transformers reads it when IT is imported, inside the functions below.
os.environ.setdefault("HF_HOME", str(ws.ROOT / ".hf"))

# Per-task training config, mirroring configs/annotation/querygen_specs/: a shared
# underscore-prefixed file deep-merged with one file per unit. The values are pins behind
# published numbers, so they are committed data rather than code - the same reasoning that
# puts the freeze pin in configs/eval/freeze.conf.
TRAINING_CONFIGS = ws.ROOT / "configs" / "eval" / "training"
COMMON_CONFIG = TRAINING_CONFIGS / "_common.yaml"

SEQUENCE_LIMITS = (1024, 1536, 2048, 3072, 4096, 6144, 8192)


# Grounding trains on three of its five labels. The deciding counts are at ITEM grain - each
# record's responses majority-consolidated, which is what tlmtc splits and trains on, not the
# per-response rows of the staged CSV. In the 2026-07-30 export's 447 grounding items:
# support_present is 445 positive / 2 negative (672 / 2 at response-row grain) and source_cited
# is 446 / 1 (669 / 5). Two and one negative items cannot give tlmtc full class support per
# label across a train/val/test split at any ratio, so it refuses the run outright. This is a
# data floor, not a tuning problem: the newer export added grounding rows and not one new
# negative for either label. Re-check on a future export - `check-sequence-length` prints these
# item counts, and `combine` the response-row ones.
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
            "  are in docs/eval-synthetic-evaluator.md."
        ) from exc


def _narrow_grounding_labels(task: str) -> tuple[str, ...] | None:
    """Restrict grounding to its trainable labels. A no-op for the other two tasks.

    Reassigns pragmata's LABEL_COLUMNS_BY_TASK, which reaches only the code that re-reads it
    at CALL time: core/eval/transforms.py, where the label set decides what gets consolidated
    by majority and which columns become tlmtc's `label_*`. It does NOT reach the input
    contract. GROUNDING_TRAIN_SCHEMA is built from the same mapping at module IMPORT time in
    core/schemas/eval_input.py, so it has already frozen all five labels; the read still rejects
    a CSV missing support_present or source_cited. The staged CSV carries every export column, so
    that costs nothing here - but it means the two labels are dropped from training, not from
    the input.

    Shared by `train` and `check-sequence-length` so the diagnostic consolidates over the same
    label set the run will, rather than over all five. Callers must have gone through
    ec.pragmata_eval() first, so that the import below resolves the same pragmata the run uses.
    """
    if task != "grounding":
        return None
    from pragmata.core.schemas import eval_input
    from pragmata.core.schemas.annotation_task import Task

    eval_input.LABEL_COLUMNS_BY_TASK[Task.GROUNDING] = GROUNDING_TRAIN_LABELS
    print(
        f"grounding labels narrowed to: {', '.join(GROUNDING_TRAIN_LABELS)}",
        file=sys.stderr,
    )
    return GROUNDING_TRAIN_LABELS


def _training_csv(task: str) -> tuple[Path, dict]:
    """The staged pooled CSV for a task and its provenance sidecar, checked against the pin.

    Existence is not enough, and that gap is a silent one: `combine` pools whatever freeze the
    pin named when it ran, and nothing downstream re-reads the CSV's origin afterwards.
    ec.require_fresh_staged_csv owns that rule and its reasoning; this names the layout and
    the target that rebuilds it. The freeze check is unconditional, because every training
    input is pooled from the frozen export - a moved pin always makes this CSV the previous
    dataset.

    Applied to the diagnostic as well as to training: `check-sequence-length` exists to say
    what the next run will tokenise, and a stale CSV makes that answer wrong in the same way.
    """
    path = TRAINING_INPUTS / f"{task}.csv"
    return path, ec.require_fresh_staged_csv(
        path,
        rebuild="  Run `make eval-train-inputs` - it pools the frozen export per task.",
        check_freeze=True,
    )


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

    Pools every task before writing any of them. Both failure modes here are mid-loop -
    ec.read_task raises on a schema change, and a task nobody annotated raises below - so
    writing inside the pooling loop would leave data/eval-inputs/training/ half rewritten from
    this export and half left over from the last one. A mixed-vintage staging directory is
    worse than no output at all, because the freshness guard on the other side checks each CSV
    against the pin independently and all three would still pass.
    """
    programmes = ec.programmes(exports)
    print(f"pooling {len(programmes)} programme(s) from {exports}", file=sys.stderr)

    pooled_by_task: dict[str, pd.DataFrame] = {}
    contributors_by_task: dict[str, list[str]] = {}
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
            print(f"  {programme}/{task}: {len(frame)} rows", file=sys.stderr)
            frames.append(frame)
            contributing.append(programme)

        if not frames:
            raise SystemExit(
                f"no programme contributed any {task} rows from {exports}.\n"
                "  Nothing to train on - check the export tree is populated."
            )

        pooled_by_task[task] = pd.concat(frames, ignore_index=True)
        contributors_by_task[task] = contributing

    TRAINING_INPUTS.mkdir(parents=True, exist_ok=True)
    for task, pooled in pooled_by_task.items():
        target = TRAINING_INPUTS / f"{task}.csv"
        pooled.to_csv(target, index=False, encoding="utf-8")

        # A provenance sidecar even though these are staged inputs rather than deliverables:
        # a training run's own record names this CSV, so the CSV has to name the export rows
        # and the code that built it. output_sha256 is the CSV's own bytes rather than its
        # inputs', which is what lets `train` refuse a CSV that has drifted from the record
        # beside it - see _training_csv. contributing_programmes is narrower than programmes
        # for a related reason: `inputs` names every per-programme CSV that was looked for
        # (a missing one is recorded as such, not dropped), and an export that exists but
        # holds no submitted rows for this task is not distinguishable there at all - so
        # the programmes that actually supplied rows are listed separately.
        prov = ws.provenance(
            script="scripts/eval/train_evaluators.py",
            inputs=[exports / p / f"{task}.csv" for p in programmes],
            task=task,
            programmes=programmes,
            contributing_programmes=contributors_by_task[task],
            excluded_programmes=sorted(ec.EXCLUDED_PROGRAMMES),
            freeze_date=ec.FREEZE_DATE,
            row_filter="submitted",
            output_sha256=ws.sha256_file(target),
        )
        ec.sidecar_path(target).write_text(
            json.dumps(prov, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        print(
            f"wrote {target.relative_to(ws.ROOT)} "
            f"({len(pooled)} rows, {pooled['source_domain'].nunique()} domains)",
            file=sys.stderr,
        )
        # Label prevalence per task - grounding's two dropped labels are visible here. At
        # RESPONSE-ROW grain, which is not quite the grain that decides trainability:
        # consolidating to items is a pragmata transform, and this subcommand deliberately
        # imports nothing from pragmata, so check_sequence_length owns the item counts.
        for label in ec.LABELS[task]:
            counts = pooled[label].value_counts(dropna=False).to_dict()
            n_true = int(counts.get(True, 0))
            n_false = int(counts.get(False, 0))
            print(
                f"    {label}: {n_true} positive / {n_false} negative", file=sys.stderr
            )
        print(
            "    ^ response rows. Training consolidates each item's responses to one row by "
            "majority first;\n      `make eval-train-seqlen` prints the item counts, which "
            "are the actual data floor.",
            file=sys.stderr,
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

    Also prints per-label positive/negative counts at that same item grain. It is the one
    place that can: the counts `combine` prints are response rows, and the gap between the two
    is what decides whether tlmtc can give a label full class support across the split - the
    reason grounding trains on three labels rather than five.
    """
    from transformers import AutoTokenizer

    # Task is pragmata's own enum; the binding keeps its class name deliberately.
    _eval_api, Task, _src_root = ec.pragmata_eval()
    from pragmata.core.eval.imports import import_eval_train_frame
    from pragmata.core.eval.transforms import (
        build_tlmtc_frame,
        consolidate_labels_by_majority,
    )

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
        csv_path, _sidecar = _training_csv(task)
        frame = import_eval_train_frame(path=csv_path, task=task_enum)

        # Counted over ALL of the task's labels, and so before the narrowing below: grounding's
        # two dropped labels are exactly the ones whose item counts have to be re-read on a
        # future export, and after narrowing they are no longer consolidated at all - the
        # column survives, but holding the representative row's raw value rather than the
        # majority. pragmata's own consolidation, so these are the numbers tlmtc would split.
        consolidated = consolidate_labels_by_majority(frame, task=task_enum)

        # Narrow exactly as `train` does, so what gets tokenised below is what the run
        # tokenises: the label set decides which row represents an item when its annotators
        # disagree, and therefore which text is measured.
        narrowed = _narrow_grounding_labels(task)
        tlmtc_frame = build_tlmtc_frame(frame, task=task_enum, mode="train")

        tokenizer = AutoTokenizer.from_pretrained(checkpoint)
        lengths = tlmtc_frame.apply(
            lambda row, tok=tokenizer: len(
                tok.encode(str(row["text"]), str(row["text_pair"]))
            ),
            axis=1,
        )

        print(f"--- {task} ({len(tlmtc_frame)} items, {checkpoint}) ---")
        for label in ec.LABELS[task]:
            n_true = int(consolidated[label].astype("int64").sum())
            dropped = (
                "" if narrowed is None or label in narrowed else "  <- not trained"
            )
            print(
                f"  {label}: {n_true} positive / {len(consolidated) - n_true} negative"
                f" (items){dropped}"
            )
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
    - Grounding's label narrowing reassigns a pragmata module-level mapping. EvalTrainSettings
      forbids extra keys, so it could not live in the YAML even as an ignored field. What that
      reassignment does and does not reach is in _narrow_grounding_labels.

    Checks run cheapest-first: the staged CSV before the GPU, because a missing or stale input
    is the likelier mistake and reporting it as a GPU problem sends the reader to the wrong
    machine.
    """
    eval_api, Task, src_root = ec.pragmata_eval()
    config = _task_config(task)

    if threshold_type is not None:
        # Rejected rather than applied where it cannot do anything. threshold_type is read by
        # tlmtc only when threshold_optimization is on, and grounding and generation pin that
        # off deliberately - so the override was accepted, announced, and silently discarded,
        # which is the worst of the three outcomes for anyone reading a run log afterwards.
        if not config.get("train_kwargs", {}).get("threshold_optimization"):
            raise SystemExit(
                f"--threshold-type {threshold_type} would do nothing for {task}: "
                f"configs/eval/training/{task}.yaml\n"
                "  pins threshold_optimization: false, and threshold_type is only read when "
                "it is on.\n"
                "  That pin is a finding, not an oversight - the optimiser degenerates on this "
                "task; see\n"
                "  the config's own comments. --threshold-type applies to retrieval."
            )
        config.setdefault("train_kwargs", {})["threshold_type"] = threshold_type
        print(f"threshold_type overridden to {threshold_type}", file=sys.stderr)

    csv_path, sidecar = _training_csv(task)

    # The whole resolved configuration, once, before anything expensive. pragmata forwards
    # train_kwargs to tlmtc verbatim without validating it, so a key tlmtc does not read is
    # accepted in silence; echoing the merged result at least puts it in the run log next to
    # the metrics it did not affect.
    print(
        f"resolved config: {json.dumps(config, sort_keys=True, ensure_ascii=False)}",
        file=sys.stderr,
    )

    ec.require_gpu(use_cpu=bool(config.get("train_kwargs", {}).get("use_cpu")))
    narrowed = _narrow_grounding_labels(task)

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

    # A provenance record inside the run directory, because that directory is what gets pushed
    # off the GPU box and read months later. Neither sidecar already in there covers the input
    # side: pragmata's pragmata_train.meta.json carries run_id, task and a timestamp, and
    # tlmtc's train_run_meta.json carries the model-side settings it received - but nothing
    # names which CSV, which freeze, or which commit of this workspace produced them, and the
    # split seed and epoch ceiling are absent from both as well. The `.workspace.` infix is
    # load-bearing: data/eval/ is pragmata's tool tree by the ownership rule in docs/eval-human-annotation.md,
    # so a file this workspace wrote there has to say so in its name.
    record = ws.provenance(
        script="scripts/eval/train_evaluators.py",
        inputs=[csv_path],
        pragmata_src=src_root,
        task=task,
        run_id=result.paths.run_id,
        training_csv=str(csv_path.relative_to(ws.ROOT)),
        training_csv_sha256=ws.sha256_file(csv_path),
        freeze_date=sidecar.get("freeze_date"),
        config=config,
        grounding_train_labels=list(narrowed) if narrowed else None,
    )
    (Path(result.paths.run_dir) / "train_provenance.workspace.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
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
            "Override threshold_optimization's mode for one run. Retrieval only: the other "
            "two tasks pin threshold_optimization off, where this would change nothing, and "
            "passing it there is refused rather than ignored. Default: the task's own pin "
            "(global for retrieval)."
        ),
    )

    args = parser.parse_args()

    if args.command == "combine":
        return combine(ec.resolve_exports(args.exports))
    if args.command == "check-sequence-length":
        return check_sequence_length()
    return train(args.task, args.threshold_type)


if __name__ == "__main__":
    raise SystemExit(main())
