#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["psycopg2-binary", "gender-guesser"]
# ///
"""One row per document in the publikationsbot corpus — the fairness audit's base.

Joins to `retrieval_manifest.csv` on `doc_id` to ask whether retrieval
over-represents any part of the corpus (by year, publisher, extent, or author gender).

Extends the query pattern in `vectorstore_inventory.py` from aggregate counts to
per-document rows, importing its DSN handling so the credential-hiding error paths live
in one place. The connection string is never stored here: it is pulled at runtime from
the dev container app's secret via `az`, and the connection is read-only. The author
name parser is duplicated rather than imported, because there it is a nested closure.

## Author gender: what the columns mean, and what they cannot mean

There is no gender field in the corpus. The only signal is a first name run through
`gender-guesser`, a dictionary lookup. Every author recorded on a document is
classified, then three columns are derived from that:

  `first_author_gender`      classification of verf1 (pers1 as fallback)
  `author_gender`            majority across resolved authors; ties -> "mixed"
  `female_present`           True if any resolved author classified female

`*_raw` columns keep `gender-guesser`'s own six-way verdict (female, mostly_female,
andy, unknown, mostly_male, male) rather than collapsing it, so coverage is visible
instead of `andy` and `unknown` being merged into one bucket. `n_authors` and
`n_authors_resolved` say how much of each row the claim rests on.

Three limits that belong in any published figure:

  1. The metadata records at most three authors (`verf1..verf3`), so "majority" is over
     RECORDED authors, not all authors.
  2. A dictionary of first names is weaker on non-Western names, and it is not a
     measure of how anyone identifies.
  3. Institutional authors have no personal name at all and are flagged
     `is_institutional`, not counted as unknown people.

Run (needs an active `az login` in the BSt tenant):
    ./corpus_catalog.py                     # write the catalog CSV
    ./corpus_catalog.py --out PATH          # explicit output path
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

# vectorstore_inventory.py owns the DSN handling: it pulls the secret via `az` and opens
# a read-only connection, taking care never to echo the credential in an error. Imported
# rather than copied so that handling lives in one place. `uv run --script` isolates
# installed packages, not local imports, so the dependency-free workspace lib is
# importable here too, giving this script the same dated output dir and provenance
# sidecar contract as its four siblings. eval_common is NOT imported: it needs pandas,
# which this script has no reason to install.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import vectorstore_inventory as vsi  # noqa: E402
import workspace as ws  # noqa: E402

# --- dev container app coordinates (shared with vectorstore_inventory.py) ---
MAIN_COLLECTION = vsi.MAIN_COLLECTION
APP_NAME = vsi.APP_NAME
RESOURCE_GROUP = vsi.RESOURCE_GROUP

AUTHOR_FIELDS = ("verf1", "verf2", "verf3")

FEMALE = {"female", "mostly_female"}
MALE = {"male", "mostly_male"}

COLUMNS = [
    "doc_id",
    "pub_year",
    "publisher",
    "place",
    "extent",
    "extent_pages",
    "n_chunks",
    # Author gender — see the module docstring for what these can and cannot mean.
    "n_authors",
    "n_authors_resolved",
    "is_institutional",
    "first_author_gender",
    "first_author_gender_raw",
    "author_gender",
    "female_present",
    "author_genders_raw",
]


def first_name(raw: str | None) -> str | None:
    """Given name from a "Last, First" library string, or None if institutional.

    Kept local because the equivalent in vectorstore_inventory.py is a closure nested
    inside its gender report, so there is nothing importable; the two must stay in step
    by hand. Drops role suffixes like "(Verf.)"/"(Hrsg.)" and treats a missing
    "Last, First" comma as an institutional author rather than guessing a single token
    is a surname.
    """
    if not raw:
        return None
    raw = re.sub(r"\(.*?\)", "", raw).strip()
    if "," not in raw:
        return None
    given = raw.split(",", 1)[1].strip()
    return given.split()[0].split("-")[0] if given else None


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
    count. The raw string is kept in `extent` so this is auditable.
    """
    if not raw:
        return ""
    numbers = [int(n) for n in re.findall(r"\d+", str(raw))]
    return str(max(numbers)) if numbers else ""


def classify(detector, names: list[str | None]) -> tuple[list[str], list[str]]:
    """(raw six-way verdicts, collapsed verdicts) for a document's recorded authors.

    Only authors with a parseable given name are classified; institutional entries are
    excluded rather than counted as unknown people.
    """
    raw: list[str] = []
    collapsed: list[str] = []
    for name in names:
        given = first_name(name)
        if given is None:
            continue
        verdict = detector.get_gender(given)
        raw.append(verdict)
        collapsed.append("female" if verdict in FEMALE else "male" if verdict in MALE else "unknown")
    return raw, collapsed


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


def build_rows(documents: list[dict]) -> list[dict]:
    import gender_guesser.detector as gender

    detector = gender.Detector(case_sensitive=False)
    rows = []
    for doc in documents:
        recorded = [doc.get(field) for field in AUTHOR_FIELDS]
        # pers1 is the display form; it stands in only when no verf* field is present,
        # so a document with structured authors is never double-counted.
        if not any(recorded):
            recorded = [doc.get("pers1")]
        raw, collapsed = classify(detector, recorded)
        n_recorded = sum(1 for name in recorded if name)

        def unresolved(empty: str, n_recorded: int = n_recorded) -> str:
            """Gender when no author could be classified: institutional, or absent.

            Shared by first_author_gender and author_gender so the institutional case
            cannot end up set on one and not the other.
            """
            return "institutional" if n_recorded else empty

        first_raw, first_collapsed = (raw[0], collapsed[0]) if raw else ("", unresolved(""))

        rows.append(
            {
                "doc_id": doc["doc_id"],
                "pub_year": parse_year(doc.get("jahr")),
                "publisher": (doc.get("verl") or "").strip(),
                "place": (doc.get("ort") or "").strip(),
                "extent": (doc.get("umf") or "").strip(),
                "extent_pages": parse_pages(doc.get("umf")),
                "n_chunks": doc["n_chunks"],
                "n_authors": n_recorded,
                "n_authors_resolved": sum(1 for g in collapsed if g != "unknown"),
                "is_institutional": n_recorded > 0 and not raw,
                "first_author_gender": first_collapsed,
                "first_author_gender_raw": first_raw,
                "author_gender": majority(collapsed) if raw else unresolved("unknown"),
                "female_present": "female" in collapsed,
                "author_genders_raw": ";".join(raw),
            }
        )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=None, help="Output CSV path.")
    args = ap.parse_args()

    target = args.out or ws.stage_report_dir("eval") / "corpus_catalog.csv"

    conn = vsi.connect(vsi.fetch_dsn())
    try:
        documents = fetch_documents(conn.cursor())
    finally:
        conn.close()
    rows = build_rows(documents)

    resolved = sum(1 for r in rows if r["n_authors_resolved"])
    institutional = sum(1 for r in rows if r["is_institutional"])
    n_chunks_total = sum(r["n_chunks"] for r in rows)
    ws.write_csv(
        target,
        rows,
        columns=COLUMNS,
        prov=ws.provenance(
            script="scripts/eval/corpus_catalog.py",
            source={"collection": MAIN_COLLECTION, "app": APP_NAME, "resource_group": RESOURCE_GROUP},
            n_documents=len(rows),
            n_chunks_total=n_chunks_total,
            n_documents_with_resolved_author=resolved,
            n_documents_institutional=institutional,
            gender_coverage=round(resolved / len(rows), 4) if rows else None,
            grain="one row per doc_id",
            caveats=[
                "author_gender is inferred from a first-name dictionary "
                "(gender-guesser), not recorded in the corpus, and is not a measure of "
                "how anyone identifies.",
                "The metadata records at most three authors (verf1..verf3), so "
                "'majority' is over RECORDED authors, not all authors.",
                "The heuristic is weaker on non-Western names; *_raw columns keep the "
                "six-way verdict so 'andy' (ambiguous) stays distinct from 'unknown' "
                "(name absent from the dictionary).",
                "Institutional authors have no personal name and are flagged "
                "is_institutional rather than counted as unknown people.",
                "pub_year and extent_pages are parsed from free-text library fields; "
                "the raw extent string is kept in `extent` so the parse is auditable.",
            ],
        ),
    )

    print(f"wrote {target} ({len(rows)} documents, {n_chunks_total} chunks)", file=sys.stderr)
    print(
        f"gender resolved for {resolved}/{len(rows)} documents "
        f"({resolved / len(rows):.1%}); {institutional} institutional-only",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
