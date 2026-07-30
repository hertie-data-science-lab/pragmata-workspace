# Eval report data dictionary

Definitions for the CSVs in `reports/eval/<date>/`. Each CSV ships with a
`*.provenance.json` sidecar naming the code, inputs and parameters it came from; the
sidecar pins this file by SHA256, so a CSV can always be paired with the wording that was
current when it was written. The sidecars record identity — what a number *means* is only
here.

`zentrum-fuer-datenmanagement` is **absent from every table** (decided 2026-07-30). It is a
real programme with imported records that nobody annotated, so every column would be zero
or blank; an all-blank row in a report table reads as a measurement rather than as an
absence. Its coverage gap is recorded in the reproducibility bundle instead.

## Vocabulary

Five nested things, smallest first. "Item" is not used as a term — the one exception is the
column name `n_items` in `annotation_label_summary.csv`, which the client specified and
which means **units**.

| Term | Definition |
|---|---|
| **response** | One annotator's submission on one record. A record annotated by three people has three responses. |
| **record** | One annotatable thing: a **chunk** for retrieval, a **query** for grounding and generation. *Completed* means it met its required annotator count. |
| **unit** | One record's responses majority-consolidated into a single value per label. 1:1 with annotated records, and the grain eval ingests. |
| **panel** | Retrieval only: the *k* chunk-records of one query. *Complete* means every chunk in it has a submitted response. |
| **query group** | One query + answer across all three tasks: its panel, plus its grounding record and its generation record. |

A **query group owns four artefacts** — the query, the bot's answer, the retrieved context
(a chunk set), and the labels — and each task's records are a *projection* of it, which is
why "record" holds different content per task:

```
query group  ── query ── answer ── context set (k chunks)
     ├── retrieval:  k records, each = query + ONE chunk        (answer shown collapsed)
     ├── grounding:  1 record  = answer + the FULL context set  (query shown collapsed)
     └── generation: 1 record  = query + answer                 (context shown collapsed)
```

Verified against the pinned task definitions in pragmata's
`core/annotation/argilla_task_definitions.py`. The consequence for counting: one query
group with k=5 is 7 records and, fully annotated by 3 people, 21 responses.

**Calibration** records are deliberately overlapped so several annotators see the same
thing; they are the only population inter-annotator agreement is computed on. They are
*pooled with production* in `annotation_operations.csv` (the operational question is how
much work happened) and *kept in* the scored corpus (pragmata's majority consolidation
coalesces their extra responses into one unit, exactly as when training).

---

## `annotation_operations.csv`

**Purpose:** how much annotation happened, and how fast. **Grain:** one row per programme
x task; production and calibration pooled, no split.

| Column | Definition |
|---|---|
| `programme` | Programme slug, as in every other CSV here (e.g. `europas-zukunft`). |
| `task` | `retrieval`, `grounding` or `generation`. |
| `n_records_live` | Records in the live Argilla dataset: one per chunk for retrieval, one per query otherwise. The denominator. |
| `n_records_completed` | Records that met their required annotator count. |
| `n_records_pending` | Records not yet completed. |
| `n_responses_submitted` | Individual annotator submissions. |
| `n_units_annotated` | Records with at least one submitted response, i.e. units available to scoring. Always ≤ `n_responses_submitted`. |
| `n_annotators` | Distinct annotators who submitted on this programme x task. |
| `n_responses_discarded` | Responses an annotator explicitly discarded — an abstention, carrying no labels. |
| `discard_rate` | `n_responses_discarded` over submitted + discarded. |
| `discard_reason_*` | The four reason codes, counted. Sum to `n_responses_discarded`. |
| `median_gap_s`, `mean_gap_s`, `gap_p25_s`, `gap_p75_s` | Seconds between one annotator's consecutive submissions, pooled across annotators. |
| `n_gaps_used` | Gaps behind those statistics, after the session-break exclusion. |
| `n_panels` | Retrieval only: panels imported, from the export's own `completeness_summary`. Blank elsewhere. |
| `n_panels_complete` | Retrieval only: panels where every chunk has a submitted response. Blank elsewhere. |

**Caveats.**

- The gap columns come from the Argilla REST API, not the export: the export's `created_at`
  is the *record's* `updated_at` and is identical across a record's annotators. Gaps longer
  than the session threshold are excluded as breaks (overnight, lunch), so these describe
  active pace, not elapsed time. The threshold is in the sidecar
  (`session_gap_threshold_s`).
- `n_records_live` and `n_units_annotated` are *record*-grain and not comparable to
  `n_responses_submitted`, which is *response*-grain. Compare like with like.

## `annotation_label_summary.csv`

**Purpose:** per-label prevalence and reliability. **Grain:** one row per programme x task
x label.

| Column | Definition |
|---|---|
| `programme`, `task`, `label` | The label's identity. |
| `n_items` | **Units** with this label (client-specified column name — see the vocabulary note). |
| `n_annotators` | Distinct annotators on this programme x task. |
| `n_true` | Units whose majority-consolidated value for this label is true. |
| `pct_agree` | Raw percentage agreement on the calibration overlap. |
| `alpha` | Krippendorff's alpha on the calibration overlap. |
| `alpha_ci_low`, `alpha_ci_high` | Bootstrap confidence interval for `alpha`. |
| `n_items_calibration` | Units alpha was actually computed on — the calibration overlap, typically ~30, **not** `n_items`. |
| `degenerate_calibration` | True where the label never varies in the pairable overlap. |

**Caveats.**

- `n_items` / `n_true` are the *production+calibration* prevalence over units; `alpha`,
  `pct_agree` and `n_items_calibration` describe the *calibration overlap only*. An alpha
  on a row is evidence about the labelling scheme, not about those particular units.
- **A blank `alpha` is not a low alpha.** It means the calibration overlap was insufficient
  to compute one — too few annotators saw the same records.
- **`alpha = 1.0` with `degenerate_calibration = True` is not evidence of reliability.**
  Alpha is `1 - Do/De` and is undefined when expected disagreement is zero (the label never
  varies in the overlap); pragmata returns 1.0 there by convention.
- Ties in majority consolidation fall back to the first row in file order, so a 1-of-2
  split is decided by CSV row order rather than by the data.

## `eval_metric_estimates.csv`

**Purpose:** the corpus metric taxonomy, scored on human labels. **Grain:** one row per
task x metric, pooled across programmes — the taxonomy has no per-programme grain.

| Column | Definition |
|---|---|
| `task`, `metric` | The estimate's identity. |
| `point` | The point estimate. |
| `ci_low`, `ci_high` | Confidence interval, at `ci_level`. |
| `method` | Interval method (Wilson for rates, bootstrap for the continuous retrieval metrics). |
| `n` | Units the metric averages over, after filtering. |
| `n_examples` | Queries scored, as pragmata counted them. |
| `ci_level` | Confidence level, 0.95. |
| `top_k` | `max(chunk_rank)` over the scored panels. |
| `n_panels_skipped` | Incomplete retrieval panels pragmata dropped before scoring. |
| `policy` | The filter combination: `calib-complete` is the reportable one. |
| `source_labels` | Label(s) the metric is computed from. |
| `alpha_min` | Weakest pooled alpha among those labels — the conservative read. |
| `alpha_min_label` | Which label that was. |
| `alpha_n_items` | Calibration units behind `alpha_min`. |
| `alpha_min_degenerate` | True where `alpha_min` is the undefined-returns-1.0 case. |
| `status` | `ok`, `undefined_no_denominator` (a conditional rate with an empty denominator), or `no_rows_after_filter`. |

**Caveats.**

- The intervals cover **sampling uncertainty over queries only** — not annotator
  disagreement and not label error. That is pragmata's explicit design. A tight interval on
  a label with alpha at or below chance reads as precision that is not there; the
  `alpha_*` columns exist to stop that reading.
- The `alpha_*` columns are the **pooled** alpha over every programme's calibration units —
  the matching population for pooled metrics. They carry no interval: alpha's bootstrap is
  resampled independently of the metric's, so the two side by side invited being read as one
  uncertainty budget. Both use 1000 resamples with a fixed seed, so both are reproducible.
- **`top_k` varies per query.** It is `max(chunk_rank)`, not a configured K, so these are
  not "@5" metrics. Do not label them with a fixed K.
- `n` counts the population that survived filtering (submitted responses; complete
  retrieval panels only), not the corpus. Read it beside `n_panels_skipped`.

## `retrieval_manifest.csv`

**Purpose:** what the retriever returned per query — the join key for the fairness audit.
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
| `annotated` | Query grain: this query's retrieval panel received at least one submitted response. |
| `n_annotated_chunks` | Query grain: how many of its chunks did. |
| `domain` | Human-readable programme name, as the record carries it. |
| `language`, `role`, `topic`, `intent`, `difficulty`, `format`, `spec_stem`, `retried` | Per-query metadata from the querygen spec, carried through unchanged. |
| `query_task` | The querygen spec's own `task` — a description of what the query asks for (e.g. "extract evidence refuting a claim"). Renamed from `task` because `task` everywhere else in this bundle means retrieval / grounding / generation. |

**Caveats.**

- **The source is the curated corpus, a superset of what was annotated.** It is *not*
  post-removal: curation selected a subset for import into Argilla, so most rows here belong
  to queries nobody annotated. Filter on `annotated` before computing anything about
  annotated data.
- **Chunk-grain fan-out.** A `doc_id` appears once per retrieved chunk, so document
  *frequency* means counting rows and distinct *documents* means deduplicating on
  `(query_id, doc_id)`. **Never join on `chunk_id` alone**: one chunk can be retrieved by
  several queries, so a chunk-only join multiplies rows across unrelated queries.
- **Joining to the annotation exports.** They carry no `query_id`, only `record_uuid`. Join
  on the **query text**, which is 1:1 with `query_id`, or on `(query_id, chunk_id)` after
  resolving the query. Join to `corpus_catalog.csv` on `doc_id`.

## `corpus_catalog.csv`

**Purpose:** one row per document in the publikationsbot corpus — the fairness audit's
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
  not a measure of how anyone identifies. It is weaker on non-Western names — the `*_raw`
  columns keep `andy` (ambiguous) distinct from `unknown` (absent from the dictionary) so
  that coverage stays visible.
- **`author_gender = 'unknown'` merges two populations:** documents with no recorded author
  at all, and documents whose authors the dictionary cannot classify. **Split on `n_authors`
  (0 vs > 0) first.** Both gender columns use the same encoding, so a cut on one transfers
  to the other, and neither is ever blank.
- "Majority" is over *recorded* authors: the metadata holds at most three, so a
  twelve-author volume is judged on three.
- The corpus is a live database with no version of its own, so the sidecar pins it by row
  count plus a checksum over the per-document chunk counts rather than by file hash. Either
  changing means the corpus moved under the catalog.
