#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["psycopg2-binary", "gender-guesser"]
# ///
"""Inventory the publikationsbot vector store (Postgres + pgvector).

Answers two questions against BSt's live DB, cross-checked from independent sources:
  1. How many publications does the bot have?    -> total + per-collection doc counts
  2. What are ALL the metadata fields?           -> union of jsonb keys, with glossary

The connection string is NOT stored here. It is pulled at runtime from the dev
container app's `publikationsbot-vectorstore-uri` secret via `az` (this VM shares
the app's resource group, so `az containerapp secret show` works without a VNet).

Run (needs an active `az login` in the BSt tenant):
    ./vectorstore_inventory.py               # count + all metadata fields
    ./vectorstore_inventory.py --gender      # + rough author-gender breakdown (opt-in)

Env overrides (defaults target the dev app):
    PB_RESOURCE_GROUP, PB_APP_NAME, PB_SECRET_NAME
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

import psycopg2

# --- dev container app coordinates (the vectorstore secret lives here) ---
RESOURCE_GROUP = os.environ.get("PB_RESOURCE_GROUP", "rg-chatbot-dev-sweden-001")
APP_NAME = os.environ.get("PB_APP_NAME", "publikationsbot-backend-dev")
SECRET_NAME = os.environ.get("PB_SECRET_NAME", "publikationsbot-vectorstore-uri")

# German library/ILS metadata keys -> English meaning. Keys not listed here fall
# back to "(unknown)" so a schema change surfaces loudly rather than silently.
FIELD_GLOSSARY = {
    "hst": "Title (main title)",
    "hst_zu": "Subtitle",
    "jahr": "Year",
    "verl": "Publisher",
    "ort": "Place of publication",
    "umf": "Extent (page count)",
    "pers1": "Author (display form)",
    "verf1": "Author 1",
    "verf2": "Author 2",
    "verf3": "Author 3",
    "doi": "DOI",
    "url_doi": "DOI URL",
    "url_bibliothekskatalog": "Library catalog URL",
    "mediennr": "Media/item number",
    "mediengrp": "Media group/type code",
    "doc_id": "Document ID",
    "id": "ID",
    "filename": "Filename",
    "source": "Source file",
    "filepath_internal": "Internal file path",
    "headline": "Headline",
    "Code": "Code (internal classification)",
    # richtlinienradar-only keys (a different tool's collection):
    "ansprechpartner": "Contact person",
    "bereich": "Area/domain",
    "converter": "Converter",
    "export_type": "Export type",
    "url": "URL",
}


def fetch_dsn() -> str:
    """Pull the vectorstore DSN from Azure. Errors never echo the value."""
    try:
        out = subprocess.run(
            [
                "az", "containerapp", "secret", "show",
                "--name", APP_NAME,
                "--resource-group", RESOURCE_GROUP,
                "--secret-name", SECRET_NAME,
                "--query", "value", "-o", "tsv",
            ],
            capture_output=True, text=True, check=True,
        )
    except FileNotFoundError:
        sys.exit("FATAL: `az` not found. Install the Azure CLI and `az login`.")
    except subprocess.CalledProcessError:
        # az prints its own error to stderr; do not re-echo (may contain context).
        sys.exit("FATAL: could not read the vectorstore secret (check `az login`).")
    dsn = out.stdout.strip()
    if not dsn:
        sys.exit("FATAL: vectorstore secret was empty.")
    # SQLAlchemy scheme (postgresql+psycopg://) -> libpq scheme psycopg2 accepts.
    return re.sub(r"^postgresql\+\w+://", "postgresql://", dsn)


def connect(dsn: str):
    """Open a read-only connection. Any failure hides the DSN-bearing message."""
    try:
        conn = psycopg2.connect(dsn)
    except Exception as e:  # noqa: BLE001 - message would leak the credential
        sys.exit(f"FATAL: could not connect to the vector store ({type(e).__name__}).")
    conn.set_session(readonly=True, autocommit=True)
    return conn


def report_counts(cur) -> None:
    print("=" * 60)
    print("PUBLICATION COUNTS")
    print("=" * 60)

    cur.execute("SELECT count(*), count(DISTINCT id) FROM public.publications;")
    total, distinct = cur.fetchone()
    print(f"\npublications table:  {total} rows  ({distinct} distinct id)")

    cur.execute(
        """
        SELECT c.name,
               count(e.id)                              AS chunk_rows,
               count(DISTINCT e.cmetadata->>'doc_id')   AS distinct_docs
        FROM public.langchain_pg_collection c
        LEFT JOIN public.langchain_pg_embedding e ON e.collection_id = c.uuid
        GROUP BY c.name
        ORDER BY c.name;
        """
    )
    print("\nembedding collections:")
    print(f"  {'collection':<40} {'chunks':>9} {'docs':>7}")
    for name, chunks, docs in cur.fetchall():
        print(f"  {name:<40} {chunks:>9} {docs:>7}")
    print(
        "\n=> The publications corpus total is the distinct-doc count of the main\n"
        "   collection, which cross-checks against the publications table above."
    )


def report_fields(cur) -> None:
    print("\n" + "=" * 60)
    print("METADATA FIELDS (union of jsonb keys, per collection)")
    print("=" * 60)

    cur.execute(
        """
        SELECT c.name, jsonb_object_keys(e.cmetadata) AS key
        FROM public.langchain_pg_collection c
        JOIN public.langchain_pg_embedding e ON e.collection_id = c.uuid;
        """
    )
    by_coll: dict[str, set[str]] = {}
    for name, key in cur.fetchall():
        by_coll.setdefault(name, set()).add(key)

    for name, keys in sorted(by_coll.items()):
        print(f"\n{name}  ({len(keys)} fields):")
        for key in sorted(keys):
            print(f"  {key:<24} {FIELD_GLOSSARY.get(key, '(unknown)')}")


def report_gender(cur) -> None:
    """Rough author-gender breakdown. Opt-in: name-guessing is imprecise."""
    import gender_guesser.detector as gender

    det = gender.Detector(case_sensitive=False)
    cur.execute(
        """
        SELECT DISTINCT ON (e.cmetadata->>'doc_id')
               e.cmetadata->>'pers1', e.cmetadata->>'verf1',
               e.cmetadata->>'verf2', e.cmetadata->>'verf3'
        FROM public.langchain_pg_embedding e
        JOIN public.langchain_pg_collection c ON c.uuid = e.collection_id
        WHERE c.name = 'azureopenaiembeddings'
        ORDER BY e.cmetadata->>'doc_id';
        """
    )
    rows = cur.fetchall()

    def first_name(raw: str | None) -> str | None:
        if not raw:
            return None
        raw = re.sub(r"\(.*?\)", "", raw).strip()  # drop (Verf.), (Hrsg.)
        if "," not in raw:  # no "Last, First" comma -> institutional author
            return None
        given = raw.split(",", 1)[1].strip()
        return given.split()[0].split("-")[0] if given else None

    FEMALE, MALE = {"female", "mostly_female"}, {"male", "mostly_male"}
    has_female = male_only = unknown_only = no_person = 0
    for names in rows:
        genders = [
            det.get_gender(fn)
            for n in names
            if (fn := first_name(n)) is not None
        ]
        if not genders:
            no_person += 1
        elif any(g in FEMALE for g in genders):
            has_female += 1
        elif all(g in MALE for g in genders):
            male_only += 1
        else:
            unknown_only += 1

    total = len(rows)
    print("\n" + "=" * 60)
    print("AUTHOR GENDER (rough estimate — see caveats)")
    print("=" * 60)
    print(f"\nTotal publications:                       {total}")
    print(f"At least one author classified female:    {has_female} ({has_female/total:.1%})")
    print(f"All authors classified male:              {male_only} ({male_only/total:.1%})")
    print(f"Institutional author only / no author:    {no_person} ({no_person/total:.1%})")
    print(f"Personal author, gender unresolved:       {unknown_only} ({unknown_only/total:.1%})")
    print(
        "\nCaveats: first-name heuristic; weaker on non-Western names; multi-author\n"
        "docs count as female-included if ANY author matches (presence, not share)."
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--gender", action="store_true",
        help="also print a rough author-gender breakdown (imprecise, opt-in)",
    )
    args = ap.parse_args()

    conn = connect(fetch_dsn())
    try:
        cur = conn.cursor()
        report_counts(cur)
        report_fields(cur)
        if args.gender:
            report_gender(cur)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
