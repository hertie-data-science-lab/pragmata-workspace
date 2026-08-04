#!/usr/bin/env python3
"""One row per document in the publikationsbot corpus — the fairness audit's base.

Columns, the author-gender collapse rule and every caveat are defined in
`docs/eval-data-dictionary.md` (`corpus_catalog.csv`).

Reads the live vector store read-only, rolling 13 of its 22 per-chunk metadata keys up to
document grain; joins to `retrieval_manifest.csv` on `doc_id`. DSN handling is imported
from `vectorstore_inventory.py` so the credential-hiding error paths live in one place.
The connection string is never stored here: it is pulled at runtime from the dev
container app's secret via `az`.

Gender is a `gender-guesser` 0.4.0 dictionary lookup on first names, emitted per author
slot (`verf1..verf3`) as a `_raw`/`_collapsed` pair — never as a joined list, because
compacting one skips a hole and shifts every later author left.

Run (needs an active `az login` in the BSt tenant):
    make eval-catalog                                    # write the catalog CSV
    make eval-catalog EVAL_ARGS="--out-dir DIR"          # explicit output directory
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections import Counter
from pathlib import Path

# vectorstore_inventory.py owns the DSN handling: it pulls the secret via `az`, opens a
# read-only connection, and never echoes the credential in an error. Imported rather than
# copied so that handling lives in one place. The workspace lib gives this script the same
# dated output dir and .provenance.json contract as its four siblings. eval_common is NOT
# imported: this script reads the live corpus, not a frozen export, so none of its
# export-shaped helpers apply.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import vectorstore_inventory as vsi
import workspace as ws

# --- DB coordinates, shared with vectorstore_inventory.py ---
MAIN_COLLECTION = vsi.MAIN_COLLECTION
APP_NAME = vsi.APP_NAME
RESOURCE_GROUP = vsi.RESOURCE_GROUP

AUTHOR_FIELDS = ("verf1", "verf2", "verf3")

# gender-guesser's six-way verdict collapsed to the two resolved classes; anything else
# (andy, unknown) stays unresolved rather than being folded into one of these.
FEMALE = {"female", "mostly_female"}
MALE = {"male", "mostly_male"}
# The verdicts that count as resolved — everything else (andy, unknown) does not.
RESOLVED = FEMALE | MALE


def first_name(raw: str | None) -> str | None:
    """Given name from a "Last, First" library string, or None if institutional.

    Drops role suffixes like "(Verf.)"/"(Hrsg.)" and treats a missing "Last, First"
    comma as an institutional author rather than guessing a single token is a surname.
    """
    if not raw:
        return None
    raw = re.sub(r"\(.*?\)", "", raw).strip()
    if "," not in raw:
        return None
    given = raw.split(",", 1)[1].strip()
    return given.split()[0].split("-")[0] if given else None


COLUMNS = [
    "doc_id",
    # Bibliographic identity, verbatim from the store apart from the title's non-filing
    # markers: without a title the audit is a table of opaque doc_ids nobody can check.
    "title",
    "subtitle",
    "doi",
    "catalog_url",
    "pub_year",
    "publisher",
    "place",
    "extent",
    "extent_pages",
    "n_chunks",
    # Author gender — see the module docstring for what these can and cannot mean.
    # Every column here is independent: each _raw holds gender-guesser's own verdict and
    # each _collapsed holds OUR decision about it. Nothing is a restatement of another
    # column, so no consumer has to guess which of two spellings of one number to trust.
    "n_authors",
    "is_institutional",
    "author1_gender_raw",
    "author1_gender_collapsed",
    "author2_gender_raw",
    "author2_gender_collapsed",
    "author3_gender_raw",
    "author3_gender_collapsed",
    "author_gender_collapsed",
]


def clean_title(raw: str | None) -> str:
    """Library title with its non-filing markers removed.

    The catalogue wraps the leading article in ¬…¬ so sorting can skip it
    ("¬The¬ Future of EU Cohesion"). The markers are a sorting instruction, not part of
    the title, and nothing here sorts by title - so they go, and the rest is verbatim.
    """
    return (raw or "").replace("\u00ac", "").strip()


def parse_year(raw: str | None) -> str:
    """Four-digit year from a free-text year field, or empty.

    The field carries things like "2019", "[2019]" and "2019/2020", so a bare int()
    would throw away recoverable rows and a lenient parser would invent precision the
    source does not have. Takes the first plausible year and nothing else.
    """
    if not raw:
        return ""
    match = re.search(r"(1[89]\d{2}|20\d{2})", str(raw))
    return match.group(1) if match else ""


def parse_pages(raw: str | None) -> str:
    """Page count from a German extent string, or empty.

    "231 Seiten", "ca. 100 S.", "XII, 340 Seiten" — takes the largest number found,
    since roman-numeral front matter and volume numbers otherwise win over the page
    count. An extent with no letters at all ("2013", "04/2010") is a year or an
    issue/year, not a page count, and returns blank - the only two such rows in the
    corpus were exactly that. The raw string is kept in `extent` so
    this is auditable.
    """
    if not raw or not re.search(r"[A-Za-z]", str(raw)):
        return ""
    numbers = [int(n) for n in re.findall(r"\d+", str(raw))]
    return str(max(numbers)) if numbers else ""


def classify_slot(detector, name: str | None) -> tuple[str, str]:
    """(raw six-way verdict, collapsed verdict) for ONE author slot.

    Per slot rather than per document so the columns stay aligned to verf1..verf3. A
    compacted list would shift every later author left whenever an earlier name fails to
    parse, making "author 2" unanswerable. Three states, kept distinguishable:

      no name recorded in this slot   ("", "")
      name recorded, unparseable      ("", "institutional")
      name recorded, parseable        (verdict, female | male | unknown)
    """
    if not name:
        return "", ""
    given = first_name(name)
    if given is None:
        return "", "institutional"
    verdict = detector.get_gender(given)
    collapsed = (
        "female" if verdict in FEMALE else "male" if verdict in MALE else "unknown"
    )
    return verdict, collapsed


def majority(collapsed: list[str]) -> str:
    """Majority gender across resolved authors; 'mixed' on a tie, 'unknown' if none."""
    resolved = [g for g in collapsed if g != "unknown"]
    if not resolved:
        return "unknown"
    counts = Counter(resolved)
    top = counts.most_common()
    if len(top) > 1 and top[0][1] == top[1][1]:
        return "mixed"
    return top[0][0]


def fetch_documents(cur) -> list[dict]:
    """One row per doc_id in the main collection, with its chunk count.

    Aggregated in the DB rather than in Python: the collection holds ~545k chunk rows
    and only the ~doc-level rollup is wanted. Metadata is taken with max(), which is
    an arbitrary-but-deterministic pick if a document's chunks ever disagree.
    """
    cur.execute(
        """
        SELECT e.cmetadata->>'doc_id'        AS doc_id,
               count(*)                      AS n_chunks,
               max(e.cmetadata->>'hst')      AS hst,
               max(e.cmetadata->>'hst_zu')   AS hst_zu,
               max(e.cmetadata->>'doi')      AS doi,
               max(e.cmetadata->>'url_bibliothekskatalog') AS catalog_url,
               max(e.cmetadata->>'jahr')     AS jahr,
               max(e.cmetadata->>'verl')     AS verl,
               max(e.cmetadata->>'ort')      AS ort,
               max(e.cmetadata->>'umf')      AS umf,
               max(e.cmetadata->>'pers1')    AS pers1,
               max(e.cmetadata->>'verf1')    AS verf1,
               max(e.cmetadata->>'verf2')    AS verf2,
               max(e.cmetadata->>'verf3')    AS verf3
        FROM public.langchain_pg_embedding e
        JOIN public.langchain_pg_collection c ON c.uuid = e.collection_id
        WHERE c.name = %s AND e.cmetadata->>'doc_id' IS NOT NULL
        GROUP BY e.cmetadata->>'doc_id'
        ORDER BY e.cmetadata->>'doc_id';
        """,
        (MAIN_COLLECTION,),
    )
    keys = [d[0] for d in cur.description]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def collection_pin(cur, documents: list[dict]) -> dict:
    """Input pin for the vector store, so corpus drift is detectable after the fact.

    The corpus is a live Postgres collection with no version of its own, so unlike the
    frozen export this catalog cannot be pinned by file hash. Two values stand in: the
    collection's total row count (which also counts chunks carrying no doc_id, invisible
    to the catalog itself) and a checksum over the per-document chunk counts the catalog
    rests on. Either changing means the corpus moved.
    """
    cur.execute(
        """
        SELECT count(*)
        FROM public.langchain_pg_embedding e
        JOIN public.langchain_pg_collection c ON c.uuid = e.collection_id
        WHERE c.name = %s;
        """,
        (MAIN_COLLECTION,),
    )
    content = "\n".join(
        f"{doc['doc_id']}:{doc['n_chunks']}"
        for doc in sorted(documents, key=lambda d: d["doc_id"])
    )
    return {
        "collection_rows": int(cur.fetchone()[0]),
        "n_documents": len(documents),
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
    }


def build_rows(documents: list[dict]) -> list[dict]:
    import gender_guesser.detector as gender

    detector = gender.Detector(case_sensitive=False)
    rows = []
    for doc in documents:
        recorded = [doc.get(field) for field in AUTHOR_FIELDS]
        # pers1 is the display form; it stands in only when no verf* field is present,
        # so a document with structured authors is never double-counted.
        if not any(recorded):
            recorded = [doc.get("pers1"), None, None]
        slots = [classify_slot(detector, name) for name in recorded]
        n_recorded = sum(1 for name in recorded if name)
        # Authors whose name PARSED, at any verdict. The document-level majority rests on
        # these: a name the dictionary does not know is still a person (-> "unknown"),
        # whereas no parseable name at all is institutional.
        parsed = [collapsed for raw, collapsed in slots if raw]

        rows.append(
            {
                "doc_id": doc["doc_id"],
                "title": clean_title(doc.get("hst")),
                "subtitle": clean_title(doc.get("hst_zu")),
                "doi": (doc.get("doi") or "").strip(),
                "catalog_url": (doc.get("catalog_url") or "").strip(),
                "pub_year": parse_year(doc.get("jahr")),
                "publisher": (doc.get("verl") or "").strip(),
                "place": (doc.get("ort") or "").strip(),
                "extent": (doc.get("umf") or "").strip(),
                "extent_pages": parse_pages(doc.get("umf")),
                "n_chunks": doc["n_chunks"],
                "n_authors": n_recorded,
                "is_institutional": n_recorded > 0 and not parsed,
                "author1_gender_raw": slots[0][0],
                "author1_gender_collapsed": slots[0][1],
                "author2_gender_raw": slots[1][0],
                "author2_gender_collapsed": slots[1][1],
                "author3_gender_raw": slots[2][0],
                "author3_gender_collapsed": slots[2][1],
                "author_gender_collapsed": (
                    majority(parsed)
                    if parsed
                    else ("institutional" if n_recorded else "unknown")
                ),
            }
        )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory (default: reports/eval/<today>/).",
    )
    args = ap.parse_args()

    target = ws.stage_report_dir("eval", args.out_dir) / "corpus_catalog.csv"

    conn = vsi.connect(vsi.fetch_dsn())
    try:
        cursor = conn.cursor()
        documents = fetch_documents(cursor)
        source_pin = collection_pin(cursor, documents)
    finally:
        conn.close()
    rows = build_rows(documents)

    # Counted here rather than read off a column: n_authors_resolved was dropped as a
    # pure restatement of the per-slot verdicts, and this summary is its one use.
    resolved = sum(
        1
        for r in rows
        if any(r[f"author{i}_gender_raw"] in RESOLVED for i in (1, 2, 3))
    )
    institutional = sum(1 for r in rows if r["is_institutional"])
    n_chunks_total = sum(r["n_chunks"] for r in rows)
    ws.write_csv(
        target,
        rows,
        columns=COLUMNS,
        prov=ws.provenance(
            script="scripts/eval/corpus_catalog.py",
            source={
                "collection": MAIN_COLLECTION,
                "app": APP_NAME,
                "resource_group": RESOURCE_GROUP,
                **source_pin,
            },
            n_chunks_total=n_chunks_total,
            n_documents_with_resolved_author=resolved,
            n_documents_institutional=institutional,
            gender_coverage=round(resolved / len(rows), 4) if rows else None,
            grain="one row per doc_id",
        ),
    )

    print(
        f"wrote {target} ({len(rows)} documents, {n_chunks_total} chunks)",
        file=sys.stderr,
    )
    # Guarded: an empty result is possible (a stale MAIN_COLLECTION name) and the CSV
    # and .provenance.json are already written by this point, so the run should end with a usable
    # message rather than a ZeroDivisionError traceback over a valid artifact.
    share = f" ({resolved / len(rows):.1%})" if rows else ""
    print(
        f"gender resolved for {resolved}/{len(rows)} documents{share}; "
        f"{institutional} institutional-only",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
