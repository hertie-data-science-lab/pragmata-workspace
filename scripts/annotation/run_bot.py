#!/usr/bin/env python3
"""
Pipe pragmata querygen output through publikationsbot prod's /stream endpoint
and emit annotation-import-ready JSONL.

For each ``data/annotation/querygen/runs/<stem>/synthetic_queries.csv``:
  - Skip query_ids already present in ``data/annotation/publikationsbot/<stem>.jsonl``
  - For each remaining query:
      1. Acquire/refresh Azure AD bearer token (via ``az``)
      2. POST /login -> sessionToken (with retry on 401)
      3. POST /stream (SSE) -> retrieved chunks block + streamed answer
      4. Parse SSE into a record, append to JSONL, flush
  - Ctrl+C / fatal errors leave persisted state intact; re-run resumes.

Output record schema (one JSON object per line). The first 5 fields satisfy
pragmata's ``QueryResponsePair`` schema; the rest are extras that preserve
provenance and are stripped at annotation-import time:

    query, answer, chunks[{chunk_id, doc_id, chunk_rank, title, text}],
    context_set, language,
    [extras] query_id, domain, role, topic, intent, task, difficulty, format,
             spec_stem

``context_set`` is rendered as markdown: each chunk is a **bold** header line
carrying the doc title inline -- ``**[chunk N • doc <id> (<title>) • <cid>]**``
-- followed by its body, with chunks separated by a ``---`` rule. ``<title>`` is
the bot's native main title (``metadata.title``); it is ``title unavailable``
when the bot omits it. This markdown renders in both the Grounding TextField
(``use_markdown=True``) and the Generation collapsible widget.

Modes:
  --probe              : one query from the first available spec, dump raw
                         SSE lines to data/annotation/publikationsbot/probe_<stem>.raw.txt
                         for inspection. Does NOT write to <stem>.jsonl.
  --spec <stem>        : process only this spec (default: all under data/annotation/querygen/runs/)
  --max-per-spec N     : cap queries per spec (smoke testing)
  (no flags)           : process all specs found, all queries, with resume
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import workspace as ws

ws.load_env()  # config/workspace.env + .env; existing env wins

RUNS_DIR = ws.RUNS_DIR
OUT_DIR = ws.OUT_DIR
OUT_DIR.mkdir(exist_ok=True)

PRD = os.environ["PUBLIKATIONSBOT_URL"]  # from config/workspace.env
LANG_MAP = {"german": "de", "english": "en"}

# Throttle: seconds to sleep after each network-touching iteration (skipped/done
# rows are unaffected). The bot's own /stream latency is ~30s, so 2s adds ~7%
# overhead while giving the bot's upstream (LLM, vector store) breathing room.
# Default from config/workspace.env (INTER_QUERY_DELAY_S); --delay overrides.
INTER_QUERY_DELAY_S = float(os.environ.get("INTER_QUERY_DELAY_S", "2.0"))

# HTTP 5xx backoff-retry schedule. Each entry is "wait this long, then retry".
# After the schedule is exhausted, the error is logged and we move on.
HTTP_5XX_BACKOFFS = [10.0, 30.0]


# --- token management -------------------------------------------------------

def fetch_aad_token() -> str:
    """Fresh Azure AD bearer for Microsoft Graph (publikationsbot's is_authorized check)."""
    r = subprocess.run(
        ["az", "account", "get-access-token",
         "--resource", "https://graph.microsoft.com",
         "--query", "accessToken", "-o", "tsv"],
        check=True, capture_output=True, text=True,
    )
    return r.stdout.strip()


class TokenManager:
    """Caches a Graph bearer; refresh on 401."""

    def __init__(self) -> None:
        self._token: str | None = None

    def get(self) -> str:
        if self._token is None:
            self._token = fetch_aad_token()
        return self._token

    def refresh(self) -> str:
        self._token = fetch_aad_token()
        return self._token


# --- bot calls --------------------------------------------------------------

def login(client: httpx.Client, auth_token: str) -> None:
    """Verify auth with /login. The bot returns {'authenticated': true} only —
    NO sessionToken is issued (verified against live /openapi.json). The
    sessionToken required by /stream is client-generated; we synthesize a
    per-query unique value in the caller (see process_spec / probe_mode) so
    each query is a fresh chat with no history contamination across queries.
    """
    r = client.post(f"{PRD}/login", json={"authToken": auth_token}, timeout=30.0)
    r.raise_for_status()
    if not r.json().get("authenticated"):
        raise RuntimeError(f"/login did not confirm auth: {r.text!r}")


def stream_query(
    client: httpx.Client,
    auth_token: str,
    session_token: str,
    query_text: str,
    *,
    capture_raw: bool = False,
) -> tuple[str, list[dict], list[str]]:
    """POST /stream and parse the bot's custom-framed streaming response.

    Despite content-type text/event-stream, the bot does NOT use standard SSE
    `data: <payload>\\n\\n` framing. Verified empirically (2026-05-26):

        <data>{"query": "<keywords>", "retrieved_docs": [Document, ...]}</data><answer text streamed token-by-token without newlines>

    `retrieved_docs` is a list of LangChain-serialized Document objects (see
    normalize_chunks for the shape). The answer text after </data> arrives as
    LLM tokens, no line framing.

    We must read with iter_text() not iter_lines(): the bot rarely emits \\n,
    so iter_lines() buffers indefinitely (the 180s ReadTimeout symptom).
    """
    body = {
        "query": {
            "authToken": auth_token,
            "content": query_text,
            "sessionToken": session_token,
        },
        "lr": {"authToken": auth_token},
    }
    raw_text = ""

    with client.stream("POST", f"{PRD}/stream", json=body, timeout=300.0) as r:
        r.raise_for_status()
        for chunk in r.iter_text():
            raw_text += chunk

    raw_docs: list[dict] = []
    answer = ""

    if raw_text.startswith("<data>"):
        end = raw_text.find("</data>")
        if end >= 0:
            data_json = raw_text[len("<data>"):end]
            answer = raw_text[end + len("</data>"):]
            try:
                obj = json.loads(data_json)
                if isinstance(obj, dict):
                    raw_docs = obj.get("retrieved_docs", []) or []
            except json.JSONDecodeError:
                pass
        else:
            # truncated — no closing tag
            answer = raw_text
    else:
        # unexpected — no <data> prefix; treat whole body as answer
        answer = raw_text

    raw_capture = [raw_text] if capture_raw else []
    return answer, raw_docs, raw_capture


def normalize_chunks(raw_docs: list[dict]) -> list[dict]:
    """Coerce bot-side LangChain Document objects to pragmata's Chunk schema.

    The bot returns LangChain-serialized Document objects of the form:
        {
          "lc": 1, "type": "constructor",
          "id": ["langchain", "schema", "document", "Document"],
          "kwargs": {
            "metadata": {"score": .., "url": .., "title": .., "year": ..,
                         "publisher": .., "id": .., "ref": .., ...},
            "page_content": "<summary>...</summary>\\n\\n<chunks><chunk>...</chunk></chunks>",
            "type": "Document",
          }
        }

    Each Document represents one retrieved publication; page_content includes
    both an LLM-generated <summary> and the actual retrieved <chunks>.

    Mapping (one Document -> one Chunk in pragmata's schema):
      doc_id     <- metadata.id (numeric publication id) or url as fallback
      chunk_id   <- f"{doc_id}-c1" (if/when we split per <chunk> later, -c2/-c3)
      chunk_rank <- metadata.ref (1-based retrieval rank)
      text       <- kwargs.page_content (full summary+chunks markup, gives
                    annotators max context)
    """
    out: list[dict] = []
    for i, doc in enumerate(raw_docs, start=1):
        if not isinstance(doc, dict):
            continue
        kwargs = doc.get("kwargs")
        if not isinstance(kwargs, dict):
            continue
        meta = kwargs.get("metadata") if isinstance(kwargs.get("metadata"), dict) else {}
        text = (kwargs.get("page_content") or "").strip()
        if not text:
            continue
        doc_id = str(meta.get("id") or meta.get("url") or f"doc{i}")
        rank = int(meta.get("ref") or i)
        out.append({
            "chunk_id": f"{doc_id}-c1",
            "doc_id": doc_id,
            "chunk_rank": rank,
            # bot's native main title (= vectorstore `hst`); shown inline in the
            # context_set header. Subtitle (hst_zu) is not in the bot response.
            "title": meta.get("title"),
            "text": text,
        })
    return out


# --- per-spec processing ----------------------------------------------------

def load_done_query_ids(out_path: Path) -> set[str]:
    if not out_path.exists():
        return set()
    done: set[str] = set()
    with out_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                qid = rec.get("query_id")
                if qid:
                    done.add(qid)
            except json.JSONDecodeError:
                continue
    return done


def _log_error(err_path: Path, *, query_id: str, spec_stem: str, error_type: str,
               message: str, attempt_count: int, status_code: int | None = None,
               response_body: str | None = None) -> None:
    """Append one structured error record to <spec>.errors.jsonl. Lazy-created.

    response_body: optional truncated bot response body for 5xx debugging.
    The bot's prod backend often returns a JSON or text body explaining
    *why* a 5xx happened (vector store error, model fault, rate limit).
    """
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "query_id": query_id,
        "spec_stem": spec_stem,
        "error_type": error_type,
        "message": message,
        "attempt_count": attempt_count,
    }
    if status_code is not None:
        record["status_code"] = status_code
    if response_body is not None:
        record["response_body"] = response_body[:1000]
    with err_path.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _safe_body(exc: Exception) -> str | None:
    """Extract response body from an httpx HTTPStatusError, if any. Truncated."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    try:
        text = resp.text
    except Exception:
        return None
    return text[:1000] if text else None


def _log_no_retrieval(nr_path: Path, *, query_id: str, spec_stem: str, query: str,
                       answer: str, attempt_count: int, csv_row: dict) -> None:
    """Append a no-retrieval record to <spec>.no_retrieval.jsonl.

    Captures cases where the bot processed the query and produced an answer
    but returned ZERO chunks (typically a canned "no relevant documents"
    response). Can't be imported into annotation (pragmata requires
    chunks >= 1) but preserves the bot's actual response for later analysis
    of retrieval-gap behaviour.
    """
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "query_id": query_id,
        "spec_stem": spec_stem,
        "query": query,
        "answer": answer,
        "attempt_count": attempt_count,
        # CSV provenance (same fields as the main jsonl extras):
        "domain": csv_row.get("domain"),
        "role": csv_row.get("role"),
        "topic": csv_row.get("topic"),
        "intent": csv_row.get("intent"),
        "task": csv_row.get("task"),
        "difficulty": csv_row.get("difficulty"),
        "format": csv_row.get("format"),
    }
    with nr_path.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def process_spec(spec_stem: str, tm: TokenManager, *, max_queries: int | None = None,
                 delay_s: float = INTER_QUERY_DELAY_S) -> tuple[int, int, int]:
    csv_path = RUNS_DIR / spec_stem / "synthetic_queries.csv"
    out_path = OUT_DIR / f"{spec_stem}.jsonl"
    err_path = OUT_DIR / f"{spec_stem}.errors.jsonl"
    nr_path = OUT_DIR / f"{spec_stem}.no_retrieval.jsonl"
    if not csv_path.exists():
        print(f"  ! no CSV at {csv_path}, skipping")
        return 0, 0, 0

    done = load_done_query_ids(out_path)
    n_added = n_skipped = n_error = n_retried = n_no_retrieval = 0

    with csv_path.open() as f, out_path.open("a") as out, httpx.Client(timeout=240.0) as client:
        rows = list(csv.DictReader(f))
        if max_queries:
            rows = rows[:max_queries]
        total = len(rows)
        print(f"  {total} rows, {len(done)} already done, {total - len(done)} to process")

        token = tm.get()
        login(client, token)  # verify auth upfront; bot issues no sessionToken

        for i, row in enumerate(rows, start=1):
            qid = row["query_id"]
            if qid in done:
                n_skipped += 1
                continue

            session = f"pragmata-eval-{qid}"  # unique per query = fresh chat, no history bleed
            retried = False
            answer = None
            raw_chunks = None
            try:
                answer, raw_chunks, _ = stream_query(client, token, session, row["query"])
            except httpx.HTTPStatusError as e:
                sc = e.response.status_code
                if sc == 401:
                    token = tm.refresh()
                    login(client, token)
                    try:
                        answer, raw_chunks, _ = stream_query(client, token, session, row["query"])
                    except Exception as e2:
                        print(f"    ! {qid}: 401 retry failed: {type(e2).__name__}: {e2}")
                        _log_error(err_path, query_id=qid, spec_stem=spec_stem,
                                   error_type="auth_retry_failed",
                                   message=f"{type(e2).__name__}: {e2}",
                                   attempt_count=2, status_code=401,
                                   response_body=_safe_body(e2))
                        n_error += 1
                        time.sleep(delay_s)
                        continue
                elif sc in (500, 502, 503, 504):
                    # 5xx backoff-retry: server-side hiccup, wait then retry
                    recovered = False
                    final_sc = sc
                    final_body = _safe_body(e)
                    for attempt_idx, wait in enumerate(HTTP_5XX_BACKOFFS, start=2):
                        print(f"    [{i}/{total}] {qid}: HTTP {sc}, sleeping {wait}s before attempt {attempt_idx}...")
                        time.sleep(wait)
                        try:
                            answer, raw_chunks, _ = stream_query(client, token, session, row["query"])
                            recovered = True
                            print(f"    [{i}/{total}] {qid}: recovered on attempt {attempt_idx}")
                            break
                        except httpx.HTTPStatusError as e2:
                            final_sc = e2.response.status_code
                            final_body = _safe_body(e2)
                            if final_sc not in (500, 502, 503, 504):
                                break  # different error class — don't keep retrying
                        except Exception as e2:
                            print(f"    [{i}/{total}] {qid}: backoff retry threw {type(e2).__name__}")
                            break
                    if not recovered:
                        print(f"    ! {qid}: HTTP {final_sc} after {len(HTTP_5XX_BACKOFFS) + 1} attempts")
                        _log_error(err_path, query_id=qid, spec_stem=spec_stem,
                                   error_type="http_5xx_after_backoff",
                                   message=f"HTTP {final_sc}",
                                   attempt_count=len(HTTP_5XX_BACKOFFS) + 1,
                                   status_code=final_sc,
                                   response_body=final_body)
                        n_error += 1
                        time.sleep(delay_s)
                        continue
                else:
                    print(f"    ! {qid}: HTTP {sc}")
                    _log_error(err_path, query_id=qid, spec_stem=spec_stem,
                               error_type="http_error",
                               message=f"HTTP {sc}",
                               attempt_count=1, status_code=sc,
                               response_body=_safe_body(e))
                    n_error += 1
                    time.sleep(delay_s)
                    continue
            except Exception as e:
                print(f"    ! {qid}: {type(e).__name__}: {e}")
                _log_error(err_path, query_id=qid, spec_stem=spec_stem,
                           error_type="exception",
                           message=f"{type(e).__name__}: {e}",
                           attempt_count=1)
                n_error += 1
                time.sleep(delay_s)
                continue

            chunks = normalize_chunks(raw_chunks)
            answer = answer.strip()

            # Retry once on transient retrieval failure (chunks=0 but answer
            # non-empty — bot processed the query, vector search just returned
            # nothing this time). LLM query-expansion + vector search are
            # non-deterministic; same query often succeeds on a 2nd try.
            if not chunks and answer:
                print(f"    [{i}/{total}] {qid}: chunks=0 on attempt 1, retrying...")
                time.sleep(2)
                try:
                    retry_answer, retry_raw, _ = stream_query(client, token, session, row["query"])
                    retry_chunks = normalize_chunks(retry_raw)
                    if retry_chunks:
                        chunks = retry_chunks
                        answer = retry_answer.strip()
                        retried = True
                        n_retried += 1
                        print(f"    [{i}/{total}] {qid}: retry recovered {len(chunks)} chunks")
                except Exception as e:
                    print(f"    [{i}/{total}] {qid}: retry failed: {type(e).__name__}: {e}")
                    _log_error(err_path, query_id=qid, spec_stem=spec_stem,
                               error_type="retry_exception",
                               message=f"{type(e).__name__}: {e}",
                               attempt_count=2,
                               response_body=_safe_body(e))

            if not chunks or not answer:
                attempts = 2 if answer else 1
                print(f"    ! {qid}: empty response after retry (chunks={len(chunks)}, answer_len={len(answer)})")
                _log_error(err_path, query_id=qid, spec_stem=spec_stem,
                           error_type="empty_response_after_retry",
                           message=f"chunks={len(chunks)}, answer_len={len(answer)}",
                           attempt_count=attempts)
                # Sidecar: when bot produced an answer but no chunks, preserve
                # the answer text for later analysis of retrieval-gap behaviour.
                if answer and not chunks:
                    _log_no_retrieval(nr_path, query_id=qid, spec_stem=spec_stem,
                                       query=row["query"], answer=answer,
                                       attempt_count=attempts, csv_row=row)
                    n_no_retrieval += 1
                n_error += 1
                time.sleep(delay_s)
                continue

            record = {
                # QueryResponsePair contract:
                "query": row["query"],
                "answer": answer,
                "chunks": chunks,
                # Concatenated chunk text (not the query_id). Used as the
                # primary visible field in the Grounding workspace (annotators
                # need to read the actual context to judge support/contradiction)
                # and as the auxiliary collapsible context in Generation.
                # Each chunk = a bold markdown header line (title inline) then
                # its body; chunks separated by a '---' rule. Markdown renders
                # in both the Grounding TextField and the Generation widget.
                "context_set": "\n\n---\n\n".join(
                    f"**[chunk {c['chunk_rank']} • doc {c['doc_id']} "
                    f"({c.get('title') or 'title unavailable'}) • {c['chunk_id']}]**\n\n{c['text']}"
                    for c in chunks
                ),
                "language": LANG_MAP.get(row.get("language", "").lower(), row.get("language") or None),
                # extras (strip at annotation-import time):
                "query_id": qid,
                "domain": row.get("domain"),
                "role": row.get("role"),
                "topic": row.get("topic"),
                "intent": row.get("intent"),
                "task": row.get("task"),
                "difficulty": row.get("difficulty"),
                "format": row.get("format"),
                "spec_stem": spec_stem,
                "retried": retried,
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()
            n_added += 1
            print(f"    [{i}/{total}] {qid}: ok ({len(chunks)} chunks, {len(answer)}-char answer{', retried' if retried else ''})")
            time.sleep(delay_s)

    if n_error:
        print(f"  errors logged to: {err_path}")
    if n_no_retrieval:
        print(f"  no-retrieval cases (answer w/ 0 chunks) saved to: {nr_path}  ({n_no_retrieval} records)")
    if n_retried:
        print(f"  recovered via retry-once: {n_retried}")
    return n_added, n_skipped, n_error


# --- probe mode -------------------------------------------------------------

def probe_mode(spec_stem: str | None) -> int:
    if spec_stem is None:
        candidates = sorted(
            p.name for p in RUNS_DIR.iterdir()
            if p.is_dir() and (p / "synthetic_queries.csv").exists()
        )
        if not candidates:
            print(f"No querygen runs with synthetic_queries.csv under {RUNS_DIR}")
            return 1
        spec_stem = candidates[0]

    csv_path = RUNS_DIR / spec_stem / "synthetic_queries.csv"
    print(f"PROBE: spec={spec_stem}, csv={csv_path}")

    with csv_path.open() as f:
        first = next(csv.DictReader(f))
    print(f"  query_id: {first['query_id']}")
    print(f"  query   : {first['query'][:140]}{'...' if len(first['query']) > 140 else ''}")

    tm = TokenManager()
    with httpx.Client(timeout=240.0) as client:
        token = tm.get()
        print(f"  [token] {len(token)} chars")
        login(client, token)
        print(f"  [login] ok (bot returns {{authenticated: true}}; no sessionToken issued)")

        session = f"pragmata-eval-{first['query_id']}"
        print(f"  [session] synthesized: {session}")

        print(f"  [stream] POST {PRD}/stream ...")
        t0 = time.time()
        answer, raw_chunks, raw_lines = stream_query(
            client, token, session, first["query"], capture_raw=True,
        )
        dt = time.time() - t0
        print(f"  [stream] done in {dt:.1f}s, {len(raw_lines)} SSE lines captured")

    raw_path = OUT_DIR / f"probe_{spec_stem}.raw.txt"
    with raw_path.open("w") as f:
        f.write(raw_lines[0] if raw_lines else "")
    print(f"  raw response body -> {raw_path}  ({raw_path.stat().st_size} bytes)")

    chunks = normalize_chunks(raw_chunks)
    print(f"\n  parsed answer: {len(answer)} chars")
    print(f"  parsed retrieved_docs: {len(raw_chunks)} raw -> {len(chunks)} normalized chunks")
    if raw_chunks and isinstance(raw_chunks[0], dict):
        first_doc = raw_chunks[0]
        kw = first_doc.get("kwargs", {}) if isinstance(first_doc.get("kwargs"), dict) else {}
        meta = kw.get("metadata", {}) if isinstance(kw.get("metadata"), dict) else {}
        print(f"  doc[0] metadata keys: {list(meta.keys())}")
        print(f"  doc[0] meta sample : id={meta.get('id')}, ref={meta.get('ref')}, title={meta.get('title')!r}, year={meta.get('year')}")
        print(f"  doc[0] page_content[:300]: {(kw.get('page_content') or '')[:300]!r}")
    if chunks:
        c0 = chunks[0]
        print(f"  normalized chunk[0]: chunk_id={c0['chunk_id']!r}, doc_id={c0['doc_id']!r}, chunk_rank={c0['chunk_rank']}, text_len={len(c0['text'])}")
    print(f"\n  answer preview: {answer[:300]}{'...' if len(answer) > 300 else ''}")
    return 0


# --- main -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--probe", action="store_true",
                    help="Single-query inspect mode; no JSONL write.")
    ap.add_argument("--spec", help="Process only this spec stem (default: all).")
    ap.add_argument("--max-per-spec", type=int,
                    help="Cap queries per spec (smoke testing).")
    ap.add_argument("--delay", type=float, default=INTER_QUERY_DELAY_S,
                    help=f"Seconds to sleep after each network-touching query (default: {INTER_QUERY_DELAY_S}). "
                         "Tune higher if the bot returns 5xx; lower at your own risk.")
    args = ap.parse_args()

    if args.probe:
        return probe_mode(args.spec)

    if args.spec:
        specs = [args.spec]
    else:
        specs = sorted(p.name for p in RUNS_DIR.iterdir() if p.is_dir())
    if not specs:
        print(f"No specs found under {RUNS_DIR}")
        return 1

    print(f"Processing {len(specs)} spec(s) (inter-query delay: {args.delay}s, 5xx backoff: {HTTP_5XX_BACKOFFS})...")
    tm = TokenManager()
    totals = {"added": 0, "skipped": 0, "error": 0}
    for stem in specs:
        print(f"\n=== {stem} ===")
        try:
            a, s, e = process_spec(stem, tm, max_queries=args.max_per_spec, delay_s=args.delay)
        except Exception as exc:
            print(f"  !! fatal in {stem}: {type(exc).__name__}: {exc}")
            continue
        totals["added"] += a
        totals["skipped"] += s
        totals["error"] += e
        print(f"  -> +{a} new, {s} skipped, {e} errors")

    print(f"\nTOTAL: +{totals['added']} new, {totals['skipped']} skipped, {totals['error']} errors")
    return 0 if totals["error"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
