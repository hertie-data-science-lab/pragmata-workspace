#!/usr/bin/env python3
"""What the retriever returned for each query — the join key for the fairness audit.

Columns and caveats are defined in `docs/data-dictionary.md`
(`retrieval_manifest.csv`).

Reads `*_combined.curated.jsonl` — the curated corpus, a superset of what was imported
into Argilla and annotated — and carries the querygen spec's per-query metadata through
unchanged. `panel_started` and `n_chunks_annotated` are counted against the frozen export
and are the only two columns here derived from annotation state, so a rate over this file
can pick its own denominator.

Usage:
  scripts/eval/retrieval_manifest.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import eval_common as ec
import workspace as ws

# Per-query metadata carried straight through from the querygen spec, so the audit can
# break retrieval down by any of them without a second join. Keyed by output column;
# the spec's `task` is renamed to end the collision with the eval task vocabulary.
QUERY_META = {
    "language": "language",
    "role": "role",
    "topic": "topic",
    "intent": "intent",
    "query_task": "task",
    "difficulty": "difficulty",
    "format": "format",
    "spec_stem": "spec_stem",
    "retried": "retried",
}

COLUMNS = [
    "query_id",
    "programme",
    "doc_id",
    "chunk_id",
    "rank",
    # The retriever's own relevance value, where the run that produced the data captured
    # it. `rank` is ordinal and cannot say whether rank 5 was a close call; the bot
    # response is not retained, so a run that dropped the score cannot be back-filled.
    "score",
    "n_retrieved_chunks",
    # Annotation coverage, at QUERY grain and repeated across the query's rows: whether
    # this query's retrieval panel was STARTED (>=1 chunk got a submitted response - not
    # that the panel is complete), and how many of its chunks were annotated.
    "panel_started",
    "n_chunks_annotated",
    *QUERY_META,
]


def annotated_chunks(exports: Path) -> dict[str, set[str]]:
    """query text -> the chunk_ids with >=1 submitted retrieval response.

    Keyed on the query TEXT, not chunk_id alone: a chunk can be retrieved by more than
    one query, so a chunk-only match would credit annotation to a panel nobody opened.
    The exports carry no query_id, which is why the text is the join key.
    """
    coverage: dict[str, set[str]] = {}
    for programme in ec.programmes(exports):
        frame = ec.submitted(ec.read_task(exports, programme, "retrieval"))
        if frame.empty:
            continue
        for query, chunk_id in zip(frame["query"], frame["chunk_id"]):
            coverage.setdefault(str(query), set()).add(str(chunk_id))
    return coverage


def rows_for(
    path: Path, programme: str, coverage: dict[str, set[str]]
) -> tuple[list[dict], int, int]:
    """(rows, n_queries, n_queries_without_chunks) for one source file.

    A query with no chunks is retained as a single row with empty doc_id/chunk_id: the
    retriever returning nothing is a real outcome and dropping it would quietly shrink
    the denominator of any per-query rate computed from this file.
    """
    rows: list[dict] = []
    n_queries = n_empty = 0
    for record in ws.read_jsonl(path):
        n_queries += 1
        chunks = record.get("chunks") or []
        done = coverage.get(str(record.get("query", "")), set())
        n_annotated = sum(1 for c in chunks if str(c.get("chunk_id", "")) in done)
        base = {
            "query_id": record.get("query_id", ""),
            "programme": programme,
            "panel_started": n_annotated > 0,
            "n_chunks_annotated": n_annotated,
            **{column: record.get(key, "") for column, key in QUERY_META.items()},
        }
        if not chunks:
            n_empty += 1
            rows.append({**base, "n_retrieved_chunks": 0})
            continue
        for chunk in chunks:
            rows.append(
                {
                    **base,
                    "doc_id": chunk.get("doc_id", ""),
                    "chunk_id": chunk.get("chunk_id", ""),
                    "rank": chunk.get("chunk_rank", ""),
                    # "" rather than 0 when absent: a missing score and a score of zero
                    # are different claims about what the retriever returned.
                    "score": "" if chunk.get("score") is None else chunk["score"],
                    "n_retrieved_chunks": len(chunks),
                }
            )
    return rows, n_queries, n_empty


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ec.add_common_args(ap)
    args = ap.parse_args()

    suffix = ec.CURATED_SUFFIX
    paths = sorted(ws.OUT_DIR.glob(f"*{suffix}"))
    if not paths:
        raise SystemExit(f"no {suffix} files under {ws.OUT_DIR}")

    coverage = annotated_chunks(args.exports)
    rows: list[dict] = []
    totals = {"queries": 0, "empty": 0, "started": 0}
    for path in paths:
        programme = path.name.removesuffix(suffix)
        if programme in ec.EXCLUDED_PROGRAMMES:
            continue
        file_rows, n_queries, n_empty = rows_for(path, programme, coverage)
        rows.extend(file_rows)
        totals["queries"] += n_queries
        totals["empty"] += n_empty
        n_started = len({r["query_id"] for r in file_rows if r["panel_started"]})
        totals["started"] += n_started
        print(
            f"  {programme}: {n_queries} queries ({n_started} panel-started), "
            f"{len(file_rows)} rows",
            file=sys.stderr,
        )

    target = ws.stage_report_dir("eval", args.out_dir) / "retrieval_manifest.csv"
    ws.write_csv(
        target,
        rows,
        columns=COLUMNS,
        prov=ws.provenance(
            script="scripts/eval/retrieval_manifest.py",
            inputs=paths + ec.export_inputs(args.exports, include_iaa=False),
            source_suffix=suffix,
            exports_tree=str(args.exports),
            excluded_programmes=sorted(ec.EXCLUDED_PROGRAMMES),
            n_queries=totals["queries"],
            n_queries_panel_started=totals["started"],
            n_queries_without_chunks=totals["empty"],
            grain="query x retrieved chunk",
        ),
    )
    print(
        f"wrote {target} ({len(rows)} rows, {totals['queries']} queries, "
        f"{totals['started']} panel-started, {totals['empty']} with no chunks)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
