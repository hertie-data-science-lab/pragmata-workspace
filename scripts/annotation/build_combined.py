#!/usr/bin/env python3
"""
scripts/annotation/build_combined.py — pool successive querygen runs and intersperse edgecases.

For each requested domain, reads every JSONL output produced by run_bot.py
across successive runs:

  Baseline pool:
    data/publikationsbot/<domain>.jsonl
    data/publikationsbot/<domain>_batch*.jsonl    (e.g. _batch2, _batch3)

  Edgecase pool:
    data/publikationsbot/<domain>_edgecase.jsonl
    data/publikationsbot/<domain>_edgecase_batch*.jsonl

Within each pool, deduplicates on the literal query string (first occurrence
wins). Cross-run duplicates can happen even with planning memory, because
the planning summary biases toward diversification but does not enforce
strict deduplication against prior runs.

Writes three files per domain:

  data/publikationsbot/<domain>_pooled.jsonl           (deduped baseline pool)
  data/publikationsbot/<domain>_edgecase_pooled.jsonl  (deduped edgecase pool)
  data/publikationsbot/<domain>_combined.jsonl         (interspersed order)

Interspersion rules:
  - First LEAD records are baseline only (no edgecase in the warm-up).
  - Edgecases land in random positions within [LEAD, max(LEAD + n_edge, N * WINDOW_FRAC)).
  - With WINDOW_FRAC = 1/3, every edgecase appears within the first third of
    the run, so annotators encounter the edge distribution early.
  - Edgecase positions are drawn uniformly at random via a per-domain seeded
    RNG (reproducible across re-runs).

Provenance: each record's `query_id` field already carries
`<domain>_qN` (baseline) or `<domain>_edgecase_qN` (edgecase), so the
source partition survives at build time.

Calibration: edgecases are kept out of the calibration set at import time
(not here) by importing the pooled baseline first — the partition manifest
fills its `calibration_max_records` cap from baseline records, leaving zero
calibration slots for the subsequent edgecase import. See setup_and_import.sh.

Usage:
  scripts/annotation/build_combined.py                                    # all domains (configs/annotation/domains/)
  scripts/annotation/build_combined.py demokratie-und-zusammenhalt        # one
  scripts/annotation/build_combined.py demokratie-und-zusammenhalt europas-zukunft  # subset
"""

from __future__ import annotations

import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import workspace as ws
from workspace import read_jsonl, write_jsonl

# Interspersion algorithm parameters (rarely changed; not operational knobs).
LEAD = 10  # first N records are baseline-only (warm-up)
WINDOW_FRAC = 1 / 3  # all edgecases land within the first third of the run
OUT_DIR = ws.OUT_DIR


def pool_paths(domain: str, *, edgecase: bool) -> list[Path]:
    """Return all per-batch input JSONLs for a domain's baseline or edgecase pool.

    Baseline:  <domain>.jsonl, <domain>_batch<N>.jsonl
    Edgecase:  <domain>_edgecase.jsonl, <domain>_edgecase_batch<N>.jsonl
    """
    stem = f"{domain}_edgecase" if edgecase else domain
    pattern = re.compile(rf"^{re.escape(stem)}(_batch\d+)?\.jsonl$")
    matches = sorted(p for p in OUT_DIR.glob(f"{stem}*.jsonl") if pattern.match(p.name))
    return matches


def pool_records(paths: list[Path]) -> tuple[list[dict], int]:
    """Concatenate JSONLs and dedup on the literal query string.

    Returns (deduped_records, n_duplicates_dropped).
    """
    seen: set[str] = set()
    out: list[dict] = []
    duplicates = 0
    for p in paths:
        for r in read_jsonl(p):
            q = r.get("query", "")
            if q in seen:
                duplicates += 1
                continue
            seen.add(q)
            out.append(r)
    return out, duplicates


def intersperse(baseline: list[dict], edgecase: list[dict], seed: str) -> list[dict]:
    n_base, n_edge = len(baseline), len(edgecase)
    if n_edge == 0:
        return list(baseline)
    if n_base == 0:
        return list(edgecase)

    total = n_base + n_edge
    lead = min(LEAD, n_base)
    window_end = max(lead + n_edge, int(total * WINDOW_FRAC))
    # Clamp to total so we never try to sample beyond the end of the run.
    window_end = min(window_end, total)

    rng = random.Random(seed)
    edge_positions = set(rng.sample(range(lead, window_end), n_edge))

    out: list[dict] = []
    base_iter = iter(baseline)
    edge_iter = iter(edgecase)
    for i in range(total):
        out.append(next(edge_iter) if i in edge_positions else next(base_iter))
    return out


def build_for_domain(domain: str) -> int:
    base_paths = pool_paths(domain, edgecase=False)
    edge_paths = pool_paths(domain, edgecase=True)

    if not base_paths:
        print(f"  ! no baseline JSONLs for {domain} — skipping", file=sys.stderr)
        return 0

    baseline, n_base_dup = pool_records(base_paths)
    edgecase, n_edge_dup = pool_records(edge_paths)

    pooled_base_path = OUT_DIR / f"{domain}_pooled.jsonl"
    pooled_edge_path = OUT_DIR / f"{domain}_edgecase_pooled.jsonl"
    combined_path = OUT_DIR / f"{domain}_combined.jsonl"

    write_jsonl(pooled_base_path, baseline)
    if edgecase:
        write_jsonl(pooled_edge_path, edgecase)
    elif pooled_edge_path.exists():
        pooled_edge_path.unlink()

    combined = intersperse(baseline, edgecase, seed=domain)
    write_jsonl(combined_path, combined)

    edge_positions = [
        i for i, r in enumerate(combined) if "_edgecase_" in r.get("query_id", "")
    ]
    base_src = ", ".join(p.name for p in base_paths)
    edge_src = ", ".join(p.name for p in edge_paths) if edge_paths else "(none)"
    print(
        f"  {domain}:"
        f"\n    baseline sources: {base_src}"
        f"\n    edgecase sources: {edge_src}"
        f"\n    pooled: baseline={len(baseline)} (-{n_base_dup} dup)"
        f" edgecase={len(edgecase)} (-{n_edge_dup} dup)"
        f"\n    combined: {len(combined)} records"
        f"; edgecase positions: {edge_positions}",
        file=sys.stderr,
    )
    return len(combined)


def main(argv: list[str]) -> int:
    known = ws.domains()  # single source of truth: configs/annotation/domains/*.yaml
    domains = argv[1:] if len(argv) > 1 else known
    unknown = [d for d in domains if d not in known]
    if unknown:
        print(f"ERROR: unknown domain(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"Available: {', '.join(known)}", file=sys.stderr)
        return 2

    print(f"Building combined JSONLs for: {', '.join(domains)}", file=sys.stderr)
    total = sum(build_for_domain(d) for d in domains)
    print(f"Total combined records written: {total}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
