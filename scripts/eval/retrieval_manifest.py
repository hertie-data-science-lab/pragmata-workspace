#!/usr/bin/env python3
"""What the retriever returned for each query — the join key for the fairness audit.

One row per (query, retrieved chunk), carrying the per-query metadata the querygen
spec attached. Joins to `corpus_catalog.csv` on `doc_id` to ask whether retrieval
over-represents any part of the corpus.

Reads `*_combined.curated.jsonl` — the CURATED CORPUS, which is a superset of what was
imported into Argilla and annotated. It is not post-removal: the curation recorded in
reproducibility/ selected a subset for import, so most curated queries were never
annotated. The `annotated` and `n_annotated_chunks` columns say which ones were, counted
against the frozen export, so a rate over this file can pick its own denominator.

`programme` is the directory/file slug (e.g. `nachhaltige-soziale-marktwirtschaft`),
consistent with every other CSV here. The record's own `domain` field carries the
human-readable name (`Nachhaltige soziale Marktwirtschaft`) and is emitted too.

The querygen spec's own `task` field (a description of what the query asks for) is
emitted as `query_task`: `task` in every other eval CSV means retrieval / grounding /
generation, and the two collided.

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
    "domain": "domain",
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
    "n_retrieved_chunks",
    # Annotation coverage, at QUERY grain and repeated across the query's rows: whether
    # this query's retrieval panel was annotated at all, and how many of its chunks were.
    "annotated",
    "n_annotated_chunks",
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
            "annotated": n_annotated > 0,
            "n_annotated_chunks": n_annotated,
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
    totals = {"queries": 0, "empty": 0, "annotated": 0}
    for path in paths:
        programme = path.name.removesuffix(suffix)
        if programme in ec.EXCLUDED_PROGRAMMES:
            continue
        file_rows, n_queries, n_empty = rows_for(path, programme, coverage)
        rows.extend(file_rows)
        totals["queries"] += n_queries
        totals["empty"] += n_empty
        n_annotated = len({r["query_id"] for r in file_rows if r["annotated"]})
        totals["annotated"] += n_annotated
        print(
            f"  {programme}: {n_queries} queries ({n_annotated} annotated), "
            f"{len(file_rows)} rows",
            file=sys.stderr,
        )

    target = ec.out_dir(args.out_dir) / "retrieval_manifest.csv"
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
            n_queries_annotated=totals["annotated"],
            n_queries_without_chunks=totals["empty"],
            grain="query x retrieved chunk",
        ),
    )
    print(
        f"wrote {target} ({len(rows)} rows, {totals['queries']} queries, "
        f"{totals['annotated']} annotated, {totals['empty']} with no chunks)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
