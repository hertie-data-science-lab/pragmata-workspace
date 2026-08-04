#!/usr/bin/env python3
"""Inventory the publikationsbot vector store (Postgres + pgvector).

Answers two questions against the live DB: how many publications the bot has (total and
per-collection doc counts), and what all the metadata fields are (union of jsonb keys,
with a glossary). Also the home of the DSN/connection helpers `corpus_catalog.py` imports.

The connection string is NOT stored here: it is pulled at runtime from the dev container
app's `publikationsbot-vectorstore-uri` secret via `az` (this VM shares the app's resource
group, so `az containerapp secret show` works without a VNet).

Run (needs an active `az login` in the tenant):
    .venv/bin/python scripts/eval/vectorstore_inventory.py    # counts + all metadata fields

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

# The collection holding the publications corpus. Per-document work (corpus_catalog.py)
# is restricted to it, since author fields don't exist in the other collections; the
# counts/fields reports below still enumerate every collection.
MAIN_COLLECTION = os.environ.get("PB_MAIN_COLLECTION", "azureopenaiembeddings")

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
                "az",
                "containerapp",
                "secret",
                "show",
                "--name",
                APP_NAME,
                "--resource-group",
                RESOURCE_GROUP,
                "--secret-name",
                SECRET_NAME,
                "--query",
                "value",
                "-o",
                "tsv",
            ],
            capture_output=True,
            text=True,
            check=True,
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
    except Exception as e:  # the message would leak the credential
        sys.exit(f"FATAL: could not connect to the vector store ({type(e).__name__}).")
    conn.set_session(readonly=True, autocommit=True)
    return conn


def banner(title: str, gap: bool = False) -> None:
    if gap:
        print()
    print("=" * 60)
    print(title)
    print("=" * 60)


def report_counts(cur) -> None:
    banner("PUBLICATION COUNTS")

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
    banner("METADATA FIELDS (union of jsonb keys, per collection)", gap=True)

    # DISTINCT in the DB: without it, jsonb_object_keys() explodes every one of
    # ~545k rows into one row per key (~8M rows) shipped over the wire only to
    # be deduped here. The answer is ~30 (collection, key) pairs.
    cur.execute(
        """
        SELECT DISTINCT c.name, jsonb_object_keys(e.cmetadata) AS key
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


def main() -> None:
    argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    ).parse_args()

    conn = connect(fetch_dsn())
    try:
        cur = conn.cursor()
        report_counts(cur)
        report_fields(cur)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
