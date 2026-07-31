# Eval report data dictionary

> **This file is injected into the metric-production pipeline as a schema contract; do not edit w/o editing corresponding pipeline code.** 

> Here is the canonical record of definitions for the report data CSVs in `reports/eval/<date>/`. Each CSV ships with a `*.provenance.json` naming the code, inputs and parameters it came from; that file pins *this* one by SHA256, so a CSV can always be paired with the wording that was current when it was written.

> 3 points depend on this md by path and by hash:
>
> - **Injected into every deliverable.** `scripts/lib/workspace.py` (`DATA_DICTIONARY`) writes
>   this file's `{path, sha256}` into every `*.provenance.json`, and `write_csv()` copies the
>   file itself into the output directory beside the CSVs whose `.provenance.json` carries that pin. The
>   eval scripts refuse to run if it is missing.
> - **Pinned by the committed record.** `reproducibility/2026-07-30-eval-report/pins.sha256`
>   pins the copy that travelled with the 2026-07-30 CSVs, at the hash this file had then -
>   binding the delivered numbers to that exact wording. (The pin resolves under `reports/`,
>   which is not in git, so it reads `ABSENT` until the tree is fetched.)
> - **Implemented in code.** `scripts/eval/eval_common.py` is the executable half of the
>   vocabulary defined here (`response`, `record`, `item`, `panel`, `query group`) and is kept
>   in step with it. Referenced from `docs/eval.md`, `docs/implementation-guide.md`,
>   `docs/reproducibility.md`, the `Makefile` header, and the annotation report's footnotes.
>
> **Re: editing.** Any byte change moves the SHA-256, and the `*.provenance.json` files already
> shipped to BSt no longer match it; renaming this file breaks the pinned path as well. Change it as part
> of regenerating `reports/eval/<date>/` and re-pinning the bundle, never on its own.


## Vocabulary

| Term | Definition |
|---|---|
| **response** | One annotator's submission on one record. A record annotated by three people has three responses. |
| **record** | One annotatable thing: a **chunk** for retrieval, a **query** for grounding and generation. *Completed* means it met its required annotator count. |
| **item** | One record's responses majority-consolidated into a single value per label. 1:1 with annotated records, and the grain eval ingests. pragmata's own code calls this the *scoring unit*. |
| **panel** | Retrieval only: the *k* chunk-records of one query. *Complete* means every chunk in it has a submitted response. |
| **query group** | One query + response across all three tasks: its panel, plus its grounding record and its generation record. |

A **query group owns four content artefacts** - the `query`, the bot's `response`, the
retrieved `chunks` (each with its own text and rank), and the `context_set` (those chunks
rendered as one block) - and each task's records are a *projection* of them, which is why
"record" holds different content per task:

```
query group ── query ── response ── chunks (k, each with text + rank) ── context_set
    ├── retrieval:  k records, each = query + ONE chunk's text   (response collapsed)
    ├── grounding:  1 record  = response + the whole context_set   (query collapsed)
    └── generation: 1 record  = query + response                   (context_set collapsed)
```

> Consequence for counting: one query group with k=5 projects into 7 records (1x grounding, 1x generation, 5x retrieval).

**Calibration** records are deliberately overlapped so several annotators see the same
thing; they are the only population inter-annotator agreement is computed on. They are
*pooled with production* in `annotation_operations.csv` and *kept in* the scored corpus (pragmata's majority consolidation
coalesces their extra responses into one item, exactly as it does when passing data for training its synthetic data generation models).

---

## `annotation_operations.csv`

**Purpose:** how much annotation happened, and how fast. **Grain:** one row per programme
x task; production and calibration pooled, no split.

| Column | Definition |
|---|---|
| `programme` | Programme slug, BSt's domains (e.g. `europas-zukunft`). |
| `task` | `retrieval`, `grounding` or `generation`. |
| `n_records_live` | Records in the live Argilla dataset: one per chunk for retrieval, one per query otherwise. |
| `n_records_completed` | Records that met their required annotator count. 1 for production, 2-3 for calibration (programme-variant). |
| `n_records_pending` | Records not yet completed. |
| `n_responses_submitted` | Individual annotator submissions. |
| `n_items_annotated` | Records with at least one submitted response, i.e. items available to scoring. Always ≤ `n_responses_submitted`. The same count as `n_items` in `annotation_label_summary.csv`, which repeats it per label. |
| `n_annotators` | Distinct annotators who submitted on this programme x task. |
| `n_responses_discarded` | Responses an annotator explicitly discarded. |
| `discard_rate` | `n_responses_discarded` over submitted + discarded. |
| `discard_reason_*` | The four reason codes, counted. Sum to `n_responses_discarded`. |
| `median_gap_s`, `mean_gap_s`, `gap_p25_s`, `gap_p75_s` | Seconds between one annotator's consecutive submissions, pooled across annotators. This data is extracted directly via the live Argilla instance's REST API. |
| `n_gaps_used` | Gaps behind those statistics, after the session-break exclusion (here: >30 mins, but might vary - recorded in `*-provenance.json`'s `session_gap_threshold_s`). |
| `n_panels` | Retrieval only: panels imported, from the export's own `completeness_summary`. Blank elsewhere. |
| `n_panels_complete` | Retrieval only: panels where every chunk has a submitted response. Blank elsewhere. |

**Caveats.**

- The gap columns come from the Argilla REST API, not the export: the export's `created_at`
  is the *record's* `updated_at` and is identical across a record's annotators. Gaps longer
  than the session threshold are excluded as breaks (overnight, lunch), so these describe
  active pace, not elapsed time. The threshold is in the `*-provenance.json`
  (`session_gap_threshold_s`).

## `annotation_label_summary.csv`

**Purpose:** per-label prevalence and reliability. **Grain:** one row per programme x task
x label.

| Column | Definition |
|---|---|
| `programme`, `task`, `label` | The label's identity. |
| `n_items` | **Items** with this label. |
| `n_annotators` | Distinct annotators on this programme x task. |
| `n_true` | Items whose majority-consolidated value for this label is true. |
| `pct_agree` | Raw percentage agreement on the calibration overlap. |
| `alpha` | Krippendorff's alpha on the calibration overlap. |
| `alpha_ci_low`, `alpha_ci_high` | Bootstrap confidence interval for `alpha`. |
| `n_items_calibration` | Items alpha was actually computed on - the calibration overlap, typically ~30, **not** `n_items`. |
| `degenerate_calibration` | True where the label never varies in the pairable overlap. |

**Caveats.**

- `n_items` / `n_true` are the *production+calibration* prevalence over items; `alpha`,
  `pct_agree` and `n_items_calibration` describe the *calibration overlap only*. An alpha
  on a row is evidence about the labelling scheme, not about those particular items.
- **A blank `alpha` is not a low alpha.** It means the calibration overlap was insufficient
  to compute one - too few annotators saw the same records.
- `alpha` itself is **analytic** (`1 - Do/De` off the coincidence matrix); only
  `alpha_ci_low` / `alpha_ci_high` are bootstrapped, at 1000 resamples with a fixed seed
  (both recorded in the `*-provenance.json`). So the point estimate moves only when the underlying
  calibration data does, while the bounds also move if those parameters change.
- **`alpha = 1.0` with `degenerate_calibration = True` is not evidence of reliability.**
  Alpha is `1 - Do/De` and is undefined when expected disagreement is zero (the label never
  varies in the overlap); pragmata returns 1.0 there by convention.
- Ties in majority consolidation fall back to the first row in file order, so a 1-of-2
  split is decided by CSV row order rather than by the data.

## `eval_metric_estimates.csv`

**Purpose:** the corpus metric taxonomy, scored on human labels. **Grain:** one row per
task x metric, pooled across programmes - the taxonomy has no per-programme grain.

| Column | Definition |
|---|---|
| `task`, `metric` | The estimate's identity. |
| `point` | The point estimate. |
| `ci_low`, `ci_high` | Confidence interval, at `ci_level`. |
| `method` | Interval method (Wilson for rates, bootstrap for the continuous retrieval metrics). |
| `n` | Items the metric averages over, after filtering. |
| `n_examples` | Queries scored, as pragmata counted them. |
| `ci_level` | Confidence level, 0.95. |
| `top_k` | `max(chunk_rank)` over the scored panels. |
| `n_panels_skipped` | Incomplete retrieval panels pragmata dropped before scoring. |
| `policy` | The filter combination: `calib-complete` is the reportable one. |
| `source_labels` | Label(s) the metric is computed from. |
| `alpha_min` | Weakest pooled alpha among those labels - the conservative read. |
| `alpha_min_label` | Which label that was. |
| `alpha_n_items` | Calibration items behind `alpha_min`. |
| `alpha_min_degenerate` | True where `alpha_min` is the undefined-returns-1.0 case. |
| `status` | `ok`, `undefined_no_denominator` (a conditional rate with an empty denominator), or `no_rows_after_filter`. |

**Caveats.**

- The intervals cover **sampling uncertainty over queries only** - not annotator
  disagreement and not label error. That is pragmata's explicit design. A tight interval on
  a label with alpha at or below chance reads as precision that is not there; the
  `alpha_*` columns exist to stop that reading.
- The `alpha_*` columns are the **pooled** alpha over every programme's calibration items -
  the matching population for pooled metrics. They carry no interval: alpha's bootstrap is
  resampled independently of the metric's, so the two side by side invited being read as one
  uncertainty budget. Both use 1000 resamples with a fixed seed, so both are reproducible.
- **`top_k` varies per query.** It is `max(chunk_rank)`, not a configured K, so these are
  not "@5" metrics. Do not label them with a fixed K.
- `n` counts the population that survived filtering (submitted responses; complete
  retrieval panels only), not the corpus. Read it beside `n_panels_skipped`.

## `retrieval_manifest.csv`

**Purpose:** what the retriever returned per query - the join key for the fairness audit.
**Grain:** one row per (query, retrieved chunk). A query whose retrieval returned nothing
keeps one row with an empty `chunk_id` and `n_retrieved_chunks = 0`, so per-query
denominators stay right.

| Column | Definition |
|---|---|
| `query_id` | Stable query identifier, e.g. `europas-zukunft_q17`. |
| `programme` | Programme slug. |
| `doc_id` | Source document of this chunk. Joins to `corpus_catalog.csv`. |
| `chunk_id` | The retrieved chunk. |
| `rank` | Retrieval rank within this query, 1-based. |
| `n_retrieved_chunks` | Chunks this query retrieved (query grain, repeated across its rows). |
| `annotated` | Query grain, **retrieval only**: this query's retrieval panel received at least one submitted response. |
| `n_annotated_chunks` | Query grain: how many of its chunks did. |
| `domain` | Human-readable programme name, as the record carries it. |
| `language`, `role`, `topic`, `intent`, `difficulty`, `format`, `spec_stem`, `retried` | Per-query metadata from the querygen spec, carried through unchanged. |
| `query_task` | The querygen spec's own `task` - a description of what the query asks for (e.g. "extract evidence refuting a claim"). Renamed from `task` because `task` everywhere else in this bundle means retrieval / grounding / generation. |

**Caveats.**

- **The source is the curated corpus, a superset of what was annotated.** It is *not*
  post-removal: curation selected a subset for import into Argilla, so most rows here belong
  to queries nobody annotated - 464 of 1143 queries are annotated.
- **`annotated` is retrieval-scoped and is not a general "was this query annotated" flag.**
  It says the query's *retrieval panel* got a response. Grounding and generation were
  annotated on their own records and cover more queries than retrieval does (447 grounding
  and 713 generation items, against 464 retrieval-annotated queries), so filtering a
  grounding or generation question on this column silently drops annotated data. Use it for
  retrieval cuts only; for the other tasks, go to the export.
- **Chunk-grain fan-out.** A `doc_id` appears once per retrieved chunk, so document
  *frequency* means counting rows and distinct *documents* means deduplicating on
  `(query_id, doc_id)`. **Never join on `chunk_id` alone** - 739 chunks here are retrieved
  by more than one query, so a chunk-only join multiplies rows across unrelated queries.
- **Joining to the annotation exports.** They carry no `query_id`, only `record_uuid`. Join
  on the **query text**, which is verified 1:1 with `query_id` (1143 texts, 1143 ids, both
  directions), or on `(query_id, chunk_id)` once the query is resolved. Join to
  `corpus_catalog.csv` on `doc_id`.

## `corpus_catalog.csv`

**Purpose:** one row per document in the publikationsbot corpus - the fairness audit's
base population. **Grain:** one row per `doc_id`.

| Column | Definition |
|---|---|
| `doc_id` | Document identifier. Joins to `retrieval_manifest.csv`. |
| `pub_year` | Four-digit year parsed from the free-text year field; blank if unparseable. |
| `publisher`, `place` | Library metadata, verbatim. |
| `extent` | The raw extent string, kept so the page parse is auditable. |
| `extent_pages` | Largest number found in `extent`; blank when it holds no page count. |
| `n_chunks` | Chunks this document contributes to the vector store. |
| `n_authors` | Authors *recorded* on the document (at most three). |
| `n_authors_resolved` | Of those, how many the name dictionary classified. |
| `is_institutional` | The document has recorded authors but no personal names. |
| `first_author_gender` | `female` / `male` / `unknown` / `institutional` for the first recorded author. |
| `author_gender` | Majority across resolved authors; `mixed` on a tie, else as above. |
| `female_present` | Any resolved author classified female. |
| `first_author_gender_raw`, `author_genders_raw` | `gender-guesser`'s own six-way verdicts, uncollapsed. |

**Caveats.**

- **Gender is inferred from a first-name dictionary**, is not recorded in the corpus, and is
  not a measure of how anyone identifies. It is weaker on non-Western names - the `*_raw`
  columns keep `andy` (ambiguous) distinct from `unknown` (absent from the dictionary) so
  that coverage stays visible.
- **`author_gender = 'unknown'` merges two populations:** documents with no recorded author
  at all, and documents whose authors the dictionary cannot classify. **Split on `n_authors`
  (0 vs > 0) first.** Both gender columns use the same encoding, so a cut on one transfers
  to the other, and neither is ever blank.
- "Majority" is over *recorded* authors: the metadata holds at most three, so a
  twelve-author volume is judged on three.
- The corpus is a live database with no version of its own, so the `*-provenance.json` pins it by row
  count plus a checksum over the per-document chunk counts rather than by file hash. Either
  changing means the corpus moved under the catalog.
