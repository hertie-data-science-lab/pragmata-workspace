#!/usr/bin/env python3
"""What the retriever returned for each query — the join key for the fairness audit.

One row per (query, retrieved chunk), carrying the per-query metadata the querygen
spec attached. Joins to `corpus_catalog.csv` on `doc_id` to ask whether retrieval
over-represents any part of the corpus, and to the annotation exports on `query_id`.

Source defaults to `*_combined.curated.jsonl` — the curated set that was actually
imported into Argilla, so it joins to the annotated queries. `--source combined` or
`--source pooled` widen it to the pre-curation sets, which contain queries no
annotator ever saw; use those to describe the retriever, not the annotations.

`programme` is the directory/file slug (e.g. `nachhaltige-soziale-marktwirtschaft`),
consistent with every other CSV here. The record's own `domain` field carries the
human-readable name (`Nachhaltige soziale Marktwirtschaft`) and is emitted too.

Usage:
  scripts/eval/retrieval_manifest.py
  scripts/eval/retrieval_manifest.py --source combined
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import eval_common as ec  # noqa: E402
import workspace as ws  # noqa: E402

SOURCES = {
    "curated": ec.CURATED_SUFFIX,
    "combined": "_combined.jsonl",
    "pooled": "_pooled.jsonl",
}

# Per-query metadata carried straight through from the querygen spec, so the audit can
# break retrieval down by any of them without a second join.
QUERY_META = (
    "domain", "language", "role", "topic", "intent",
    "task", "difficulty", "format", "spec_stem", "retried",
)

COLUMNS = [
    "query_id",
    "programme",
    "doc_id",
    "chunk_id",
    "rank",
    "n_retrieved_chunks",
    *QUERY_META,
]


def rows_for(path: Path, programme: str) -> tuple[list[dict], int, int]:
    """(rows, n_queries, n_queries_without_chunks) for one source file.

    A query with no chunks is retained as a single row with empty doc_id/chunk_id: the
    retriever returning nothing is a real outcome and dropping it would quietly shrink
    the denominator of any per-query rate computed from this file.
    """
    rows: list[dict] = []
    n_queries = n_empty = 0
    for record in ws.read_jsonl(path):
        n_queries += 1
        base = {
            "query_id": record.get("query_id", ""),
            "programme": programme,
            **{key: record.get(key, "") for key in QUERY_META},
        }
        chunks = record.get("chunks") or []
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
    ap.add_argument("--source", choices=sorted(SOURCES), default="curated",
                    help="Which bot output to read (default: curated, what reached Argilla).")
    args = ap.parse_args()

    suffix = SOURCES[args.source]
    paths = sorted(ws.OUT_DIR.glob(f"*{suffix}"))
    if not paths:
        raise SystemExit(f"no {suffix} files under {ws.OUT_DIR}")

    rows: list[dict] = []
    totals = {"queries": 0, "empty": 0}
    for path in paths:
        programme = path.name.removesuffix(suffix)
        file_rows, n_queries, n_empty = rows_for(path, programme)
        rows.extend(file_rows)
        totals["queries"] += n_queries
        totals["empty"] += n_empty
        print(f"  {programme}: {n_queries} queries, {len(file_rows)} rows", file=sys.stderr)

    target = ec.out_dir(args.out_dir) / "retrieval_manifest.csv"
    ws.write_csv(
        target,
        rows,
        columns=COLUMNS,
        prov=ws.provenance(
            script="scripts/eval/retrieval_manifest.py",
            inputs=paths,
            source=args.source,
            source_suffix=suffix,
            n_queries=totals["queries"],
            n_queries_without_chunks=totals["empty"],
            grain="query x retrieved chunk",
            caveats=[
                "One row per retrieved chunk. A query whose retrieval returned nothing "
                "keeps one row with empty doc_id and n_retrieved_chunks=0, so per-query "
                "denominators stay correct.",
                "A doc_id appears once per retrieved chunk, so counting doc frequency "
                "means counting rows, and counting distinct documents means "
                "deduplicating on (query_id, doc_id).",
                f"source={args.source}: curated is what reached Argilla and joins to the "
                "annotations; combined/pooled are pre-curation and include queries no "
                "annotator saw.",
                "Joining to the annotation exports: they carry no query_id, only "
                "record_uuid, so join on chunk_id (verified: all 590 annotated chunks "
                "appear here) or on the query text (all 464 distinct annotated queries "
                "map to a query_id). Join to corpus_catalog.csv on doc_id.",
            ],
        ),
    )
    print(
        f"wrote {target} ({len(rows)} rows, {totals['queries']} queries, "
        f"{totals['empty']} with no chunks)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
