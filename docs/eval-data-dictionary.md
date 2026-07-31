# Eval report data dictionary

> **This file is injected into the metric-production pipeline as a schema contract; do not edit w/o editing corresponding pipeline code.** 

> Here is the canonical record of definitions for the report data CSVs in `reports/eval/<date>/`. Each CSV ships with a `*.provenance.json` naming the code, inputs and parameters it came from; that file pins *this* one by SHA256, so a CSV can always be paired with the schema & definitions that were current when it was written.

> 3 points in the pipeline depend on this md by path and by hash - see the [Appendix](#appendix).

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

- **Purpose:** how much annotation happened, and how fast. 
- **Grain:** one row per programme x task; production and calibration pooled, no split.

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

- **Purpose:** per-label prevalence and reliability. 
- **Grain:** one row per programme x task x label.

| Column | Definition |
|---|---|
| `programme`, `task`, `label` | The label's identity. |
| `n_items` | Items with this label. |
| `n_annotators` | Distinct annotators on this programme x task. |
| `n_true` | Items whose majority-consolidated value for this label is true. |
| `pct_agree` | Raw percentage agreement on the calibration overlap. |
| `alpha` | Krippendorff's alpha on the calibration overlap. |
| `alpha_ci_low`, `alpha_ci_high` | Bootstrap confidence interval for `alpha`. |
| `n_items_calibration` | Items alpha was actually computed on - the calibration overlap, typically ~30, **not** `n_items`. |
| `degenerate_calibration` | True where the label never varies in the pairable overlap. |

**Caveats.**

- `n_items` / `n_true` are the *pooled production+calibration* prevalence over items; `alpha`,
  `pct_agree` and `n_items_calibration` describe the *calibration overlap only*. 
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

- **Purpose:** the corpus metric taxonomy, scored on human labels. 
- **Grain:** one row per task x metric, pooled across programmes - the taxonomy has no per-programme grain.

| Column | Definition |
|---|---|
| `task`, `metric` | The estimate's identity. |
| `point` | The point estimate. |
| `ci_low`, `ci_high` | Confidence interval, at `ci_level`. |
| `method` | Interval method (Wilson for rates, bootstrap for the continuous retrieval metrics). |
| `n` | Items the metric averages over, (after post-hoc filtering in response to difficulites in annotation velocity/load vs expectation). |
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

- The intervals cover sampling uncertainty over queries only - not annotator
  disagreement and not label error. A tight interval on a label with alpha at or below chance reads as precision that is not there; the `alpha_*` columns exist to stop that reading.
- The `alpha_*` columns are the pooled alpha over every programme's calibration items.
- `top_k` varies per query. It is `max(chunk_rank)`, not a configured K.
- `n` counts the population that survived filtering (submitted responses; complete
  retrieval panels only), not the corpus. Read it beside `n_panels_skipped`.

## `retrieval_manifest.csv`

- **Purpose:** what the retriever returned per query - the join key for the fairness audit.
- **Grain:** one row per (query, retrieved chunk). A query whose retrieval returned nothingkeeps one row with an empty `chunk_id` and `n_retrieved_chunks = 0`, so per-query denominators stay right.

| Column | Definition |
|---|---|
| `query_id` | Stable query identifier, e.g. `europas-zukunft_q17`. |
| `programme` | Programme slug (reflecting BSt's domains). |
| `doc_id` | Source document of this chunk. Joins to `corpus_catalog.csv`. |
| `chunk_id` | The retrieved chunk - **synthesised as `<doc_id>-c1`**. The bot returns one passage per document and the pipeline never splits it, so this is 1:1 with `doc_id` by construction, not by data. |
| `rank` | Retrieval rank within this query, 1-based. |
| `n_retrieved_chunks` | Chunks this query retrieved (query grain, repeated across its rows). |
| `panel_started` | Query grain, **retrieval only**: at least one chunk of this query's retrieval panel received a submitted response. *Started*, not complete - contrast `n_panels_complete` in `annotation_operations.csv`. |
| `n_chunks_annotated` | Query grain: how many of the query's chunks got a submitted response. |
| `language`, `role`, `topic`, `intent`, `difficulty`, `format`, `spec_stem`, `retried` | Per-query metadata from the querygen spec, carried through unchanged. |
| `query_task` | The querygen spec's own `task` - a description of what the query asks for (e.g. "extract evidence refuting a claim"). Renamed from `task` because `task` everywhere else in this bundle means retrieval / grounding / generation. |

**Caveats.**

- **The source is the curated corpus, a superset of what was annotated.** It is *not*
  post-removal: curation selected a subset for import into Argilla, so most rows here belong
  to queries nobody annotated - 464 of 1143 queries are annotated.
- **`panel_started` and `n_chunks_annotated` are the only columns here derived from
  annotation state**; every other column comes from the curated corpus. They exist because
  the join that would reproduce them is not available from this bundle: the exports carry no
  `query_id`, and this file carries no query text.
- **`panel_started` is retrieval-scoped and is not a general "was this query annotated"
  flag.** It says the query's *retrieval panel* got a response. Grounding and generation were
  annotated on their own records and cover more queries than retrieval does (447 grounding
  and 713 generation items, against 464 retrieval-started queries), so filtering a grounding
  or generation question on this column silently drops annotated data. Use it for retrieval
  cuts only; for the other tasks, go to the export.
- **Row fan-out is per retrieved passage, one per document.** Because `chunk_id` is
  `<doc_id>-c1`, document *frequency* means counting rows and distinct *documents* means
  deduplicating on `(query_id, doc_id)` - identical to deduplicating on `(query_id,
  chunk_id)`. **Never join on `doc_id`/`chunk_id` alone**: 739 of the 1092 retrieved
  documents are returned for more than one query (one for 72 of them), so a document-only
  join multiplies rows across unrelated queries.
- **Joining to the annotation exports.** They carry no `query_id`, only `record_uuid`. Join
  on the **query text**, which is verified 1:1 with `query_id` (1143 texts, 1143 ids, both
  directions), or on `(query_id, chunk_id)` once the query is resolved. Join to
  `corpus_catalog.csv` on `doc_id`.

## `corpus_catalog.csv`

- **Purpose:** one row per document in the publikationsbot corpus - the fairness audit's base population. 
- **Grain:** one row per `doc_id`.

| Column | Definition |
|---|---|
| `doc_id` | Document identifier. Joins to `retrieval_manifest.csv`. |
| `title` | Document title (`hst`), with the catalogue's `¬…¬` non-filing markers around a leading article removed. Present for every document. |
| `subtitle` | Title continuation (`hst_zu`); blank for 844 of 2946 documents. |
| `doi` | DOI where the catalogue records one - 897 of 2946 documents. |
| `catalog_url` | BSt-internal library permalink. Reachable inside the BSt network only. |
| `pub_year` | Four-digit year parsed from the free-text year field; blank if unparseable. |
| `publisher`, `place` | Library metadata, verbatim. |
| `extent` | The raw extent string, kept so the page parse is auditable. |
| `extent_pages` | Largest number found in `extent`; blank when it holds no page count. |
| `n_chunks` | Chunks this document contributes to the vector store. |
| `n_authors` | Authors *recorded* on the document (at most three), including any whose name fails the `"Last, First"` parse and so leaves its slot's `_raw` blank. |
| `is_institutional` | The document has recorded authors but no personal names. |
| `author1_gender_raw`, `author2_gender_raw`, `author3_gender_raw` | `gender-guesser`'s six-way verdict for the author in that slot: `female` / `mostly_female` / `andy` / `unknown` / `mostly_male` / `male`. Blank where the slot holds no name **and** where it holds one that does not parse - the `_collapsed` column beside it separates those two. |
| `author1_gender_collapsed`, `author2_gender_collapsed`, `author3_gender_collapsed` | That slot's verdict under our rule (below): `female` / `male` / `unknown` / `institutional`, or blank where the slot holds no author. |
| `author_gender_collapsed` | Majority across the document's authors under the same rule; `mixed` on a tie, `institutional` / `unknown` where none resolved. |

**One pair per author slot**, aligned to the metadata's `verf1..verf3` (`pers1` stands in for `verf1` where no `verf*` field exists). `author2_*` is always the second *recorded* author, even where the first could not be classified. Gender comes in pairs by design: `_raw` is what `gender-guesser` returned, `_collapsed` is the decision we made about it.

**The collapse rule**, applied identically in every `_collapsed` column:

| Slot state | `_raw` | `_collapsed` |
|---|---|---|
| `female` or `mostly_female` | the verdict | `female` |
| `male` or `mostly_male` | the verdict | `male` |
| `andy` (ambiguous) or `unknown` (not in the dictionary) | the verdict | `unknown` - an author, unclassified, excluded from any majority |
| a name is recorded but does not parse as `"Last, First"` | blank | `institutional` |
| no name in this slot | blank | blank |


**Caveats.**

- **Gender is inferred from a first-name dictionary (`gender-guesser` 0.4.0)**, is not recorded in the corpus, and is not a measure of how anyone identifies. It is weaker on non-Western names - the `_raw` columns keep `andy` (ambiguous) distinct from `unknown` (absent from the dictionary) so that coverage stays visible.
- `author_gender_collapsed = 'unknown'` merges two populations: docs with no recorded author at all, and docs whose authors the dictionary cannot classify.
- **Do not collapse the slots into a single list.** Two documents (`52109`, `53806`) record
  `verf1` and `verf3` with no `verf2`; a list of only the classified authors closes that hole
  and presents `verf3`'s verdict as the second author's. Cut by slot, or count across slots -
  never by list position.
- Aggregate counts are one line off the slots: authors classified is the count of slots whose
  `_raw` is in {`female`, `mostly_female`, `male`, `mostly_male`}, and "any female author" is
  whether any slot's `_raw` is in {`female`, `mostly_female`}.
- "Majority" is over *recorded* authors: the metadata holds at most three, so a
  twelve-author volume is judged on three.
- The corpus is a live database with no version of its own, so the `*-provenance.json` pins it by row count plus a checksum over the per-document chunk counts rather than by file hash. Either changing means the corpus moved under the catalog.
- **What the store holds but this catalog does not.** Of its 22 metadata keys, 13 are rolled
  up here. Left out deliberately: `url_doi` (it is `https://doi.org/` + `doi`), `mediengrp`
  (the constant `"G"` on all 544,692 chunks), `mediennr` (a second document id),
  `filename`/`filepath_internal`/`source` (internal paths), and the per-chunk `headline` -
  a section heading has no document-grain meaning, and `retrieval_manifest.csv` cannot join
  to it either, because its `chunk_id` is the pipeline's own `<doc_id>-c1` rather than a key
  this store holds.


## Appendix
>Implications for editing this doc (pipeline deps); 3 points in the pipeline depend on this md by path and by hash:
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
