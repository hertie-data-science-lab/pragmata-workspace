"""Connection handling for the publikationsbot vector store (Postgres + pgvector).

Not runnable: this is the module `corpus_catalog.py` imports for `fetch_dsn()`, `connect()`
and the DB coordinates, so the credential-hiding error paths exist once rather than being
copied into the script that reads the store.

The connection string is NOT stored here: it is pulled at runtime from the dev container
app's `publikationsbot-vectorstore-uri` secret via `az` (this VM shares the app's resource
group, so `az containerapp secret show` works without a VNet), which needs an active
`az login` in the tenant.

The name is wider than what is left. It also used to print an inventory - per-collection
document counts and the union of metadata keys, with a glossary - which is gone: it was
wired to no Makefile target and wrote no artifact, so nothing a report cites could come
from it. `corpus_catalog.csv` carries the corpus at document grain, and its
`.provenance.json` pins what that count was taken from.

Env overrides (defaults target the dev app):
    PB_RESOURCE_GROUP, PB_APP_NAME, PB_SECRET_NAME, PB_MAIN_COLLECTION
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import psycopg2

# --- dev container app coordinates (the vectorstore secret lives here) ---
RESOURCE_GROUP = os.environ.get("PB_RESOURCE_GROUP", "rg-chatbot-dev-sweden-001")
APP_NAME = os.environ.get("PB_APP_NAME", "publikationsbot-backend-dev")
SECRET_NAME = os.environ.get("PB_SECRET_NAME", "publikationsbot-vectorstore-uri")

# The collection holding the publications corpus. Per-document work (corpus_catalog.py) is
# restricted to it, since author fields don't exist in the other collections.
MAIN_COLLECTION = os.environ.get("PB_MAIN_COLLECTION", "azureopenaiembeddings")


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
