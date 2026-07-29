#!/usr/bin/env python3
"""How much data survives each scoring policy — the decision table, not a result.

Two filters have to be settled before any corpus metric is reportable, and neither
has an obviously right answer:

  1. **Calibration in or out.** Calibration items are deliberately-selected overlap
     items. Including them inflates n but mixes a non-representative population into
     a corpus rate. Applied at QUERY grain — a retrieval record is one chunk, and
     panels are routinely mixed, so a row-grain filter would break panels (see
     ec.drop_calibration_queries).
  2. **Retrieval panels: complete-only or all.** `pragmata eval score` applies no
     completeness policy — it groups by record_uuid and averages whatever chunks are
     present — so incomplete panels produce plausible-looking @K numbers over partial
     chunk sets. Filtering to complete panels is correct but can leave almost no data.

This prints every cell of that 2x2 per programme x task so the trade is explicit,
plus two diagnostics: mean chunks-per-query (how partial the retained panels are) and
the count of units whose majority consolidation hits an exact tie.

It deliberately reports **counts, not metrics** — score_human.py owns the numbers.

Usage:
  scripts/eval/policy_grid.py                  # frozen canonical export
  scripts/eval/policy_grid.py --exports DIR    # a different export tree
  scripts/eval/policy_grid.py --markdown       # also print the markdown table
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import eval_common as ec  # noqa: E402
import workspace as ws  # noqa: E402

COLUMNS = [
    "programme",
    "task",
    "n_rows_total",
    "n_rows_submitted",
    "n_rows_production",
    "n_rows_calibration",
    # Distinct annotation units with >=1 submitted response - the consolidated-unit
    # counterpart of n_rows_submitted (chunks for retrieval, queries otherwise).
    "n_units_annotated",
    "n_annotators",
    # The 2x2: queries surviving each combination of the two filters.
    "n_queries_all_incl_calib",
    "n_queries_all_prod_only",
    "n_queries_complete_incl_calib",
    "n_queries_complete_prod_only",
    # Diagnostics.
    "n_panels",
    "n_panels_with_responses",
    "n_panels_complete",
    "mean_chunks_per_query_all",
    "mean_chunks_per_query_complete",
    "n_units_multi_annotator",
    "n_units_tied_label",
    "n_units_multi_annotator_prod",
    "n_units_tied_label_prod",
]


def mean_chunks(frame, task: str) -> str:
    """Mean labelled chunks per query — how partial the retained panels are.

    Only meaningful for retrieval; the other tasks are one row per query by
    construction, so an empty cell is the honest answer rather than 1.0.
    """
    if not ec.has_subrows(task) or frame.empty or "record_uuid" not in frame.columns:
        return ""
    return f"{frame.groupby('record_uuid')['chunk_id'].nunique().mean():.2f}"


def row_for(exports: Path, programme: str, task: str) -> dict:
    raw = ec.read_task(exports, programme, task)
    sub = ec.submitted(raw)
    prod = ec.drop_calibration_queries(sub)
    calib = ec.keep_calibration_rows(sub)

    # The panel filter only exists for retrieval; for the other tasks "complete" is
    # the same frame, so the 2x2 collapses to its left column.
    if ec.has_subrows(task):
        complete_all, complete_prod = ec.complete_panels(sub), ec.complete_panels(prod)
    else:
        complete_all, complete_prod = sub, prod

    n_tied, n_multi = ec.tied_label_units(sub, task)
    # Same count after the query-grain calibration filter - the frame that gets scored.
    # Overlap is a calibration mechanism, so this removes most tie ambiguity; a mixed
    # retrieval panel still keeps its double-annotated calibration chunks.
    n_tied_prod, n_multi_prod = ec.tied_label_units(prod, task)
    # Queries are the unit every corpus metric averages over; for retrieval they are
    # also the panels, so the same two counts serve both column pairs.
    n_queries_all = ec.n_queries(sub)
    n_queries_complete = ec.n_queries(complete_all)
    # Panel totals come from the export sidecar, not from the rows: rows only cover
    # panels that got a response, so counting them undercounts the denominator and
    # reports 0 for a programme nobody annotated.
    n_panels_total, n_panels_done = ec.panel_totals(exports, programme)

    return {
        "programme": programme,
        "task": task,
        "n_rows_total": len(raw),
        "n_rows_submitted": len(sub),
        "n_rows_production": len(prod),
        "n_rows_calibration": len(calib),
        "n_units_annotated": int(sub.groupby([k for k in ec.UNIT_KEYS[task] if k in sub.columns]).ngroups) if not sub.empty else 0,
        "n_annotators": int(sub["annotator_id"].nunique()) if not sub.empty else 0,
        "n_queries_all_incl_calib": n_queries_all,
        "n_queries_all_prod_only": ec.n_queries(prod),
        "n_queries_complete_incl_calib": n_queries_complete,
        "n_queries_complete_prod_only": ec.n_queries(complete_prod),
        # Blank on the other two tasks: they have no panel notion, and a number here
        # would invite reading one in.
        "n_panels": n_panels_total if ec.has_subrows(task) else "",
        "n_panels_with_responses": n_queries_all if ec.has_subrows(task) else "",
        "n_panels_complete": n_panels_done if ec.has_subrows(task) else "",
        "mean_chunks_per_query_all": mean_chunks(sub, task),
        "mean_chunks_per_query_complete": mean_chunks(complete_all, task),
        "n_units_multi_annotator": n_multi,
        "n_units_tied_label": n_tied,
        "n_units_multi_annotator_prod": n_multi_prod,
        "n_units_tied_label_prod": n_tied_prod,
    }


def print_markdown(rows: list[dict]) -> None:
    """The 2x2 as a markdown table, for pasting into the working doc."""
    print("\n### Queries surviving each policy\n")
    print("| Programme | Task | all+calib | all, prod | complete+calib | complete, prod | mean chunks/q |")
    print("|---|---|---:|---:|---:|---:|---:|")
    for r in rows:
        print(
            f"| {r['programme']} | {r['task']} | {r['n_queries_all_incl_calib']} | "
            f"{r['n_queries_all_prod_only']} | {r['n_queries_complete_incl_calib']} | "
            f"{r['n_queries_complete_prod_only']} | {r['mean_chunks_per_query_all'] or '-'} |"
        )
    print("\n### Majority-consolidation ties\n")
    print("| Programme | Task | multi-ann. | tied | share | multi-ann. (prod) | tied (prod) |")
    print("|---|---|---:|---:|---:|---:|---:|")
    total_multi = total_tied = total_multi_prod = total_tied_prod = 0
    for r in rows:
        multi, tied = r["n_units_multi_annotator"], r["n_units_tied_label"]
        mp, tp = r["n_units_multi_annotator_prod"], r["n_units_tied_label_prod"]
        total_multi += multi
        total_tied += tied
        total_multi_prod += mp
        total_tied_prod += tp
        share = f"{tied / multi:.1%}" if multi else "-"
        print(f"| {r['programme']} | {r['task']} | {multi} | {tied} | {share} | {mp} | {tp} |")
    print(
        f"| **TOTAL** | | **{total_multi}** | **{total_tied}** | | "
        f"**{total_multi_prod}** | **{total_tied_prod}** |"
    )
    print(
        "\n'prod' columns are after the QUERY-grain calibration filter, i.e. the frame "
        "that gets scored. Grounding/generation drop to zero ties because their "
        "overlap is entirely on calibration queries. Retrieval keeps some: a mixed "
        "panel retains its double-annotated calibration chunks, which is deliberate — "
        "removing them would break the @K denominator."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ec.add_common_args(ap)
    ap.add_argument("--markdown", action="store_true", help="Also print markdown tables to stdout.")
    args = ap.parse_args()

    rows = [
        row_for(args.exports, programme, task)
        for programme in ec.programmes(args.exports)
        for task in ec.TASKS
    ]

    target = ec.out_dir(args.out_dir) / "policy_grid.csv"
    inputs = ec.export_inputs(args.exports, include_iaa=False)
    ws.write_csv(
        target,
        rows,
        columns=COLUMNS,
        prov=ws.provenance(
            script="scripts/eval/policy_grid.py",
            inputs=inputs,
            exports_tree=str(args.exports),
            note=(
                "Counts only — no metrics. 'complete' applies the STRICT panel_complete "
                "filter and is retrieval-only; for grounding/generation the complete "
                "columns repeat the all columns by construction."
            ),
        ),
    )
    print(f"wrote {target} ({len(rows)} rows)", file=sys.stderr)

    if args.markdown:
        print_markdown(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
