# Deliverables data dictionary

> **This file is injected into the metric-production pipeline as a schema contract; do not edit w/o editing corresponding pipeline code.** 

> Here is the canonical record of definitions for the report data CSVs in `reports/eval/<date>/`. Each CSV ships with a `*.provenance.json` naming the code, inputs and parameters it came from; that file pins *this* one by SHA256, so a CSV can always be paired with the schema & definitions that were current when it was written.

> 3 points in the pipeline depend on this md by path and by hash - see the [Appendix](#appendix---implications-for-editing-this-doc-pipeline-deps).

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
| `degenerate_calibration` | True where the label never varies in the pairable overlap; False where it does vary. **Blank where there is no pairable overlap at all** - nothing was measured, which is what the blank `alpha` beside it says too. |

**Caveats.**

- `n_items` / `n_true` are the *pooled production+calibration* prevalence over items; `alpha`,
  `pct_agree` and `n_items_calibration` describe the *calibration overlap only*. 
- **A blank `alpha` is not a low alpha.** It means the calibration overlap was insufficient
  to compute one - too few annotators saw the same records.
- `alpha`, `pct_agree`, `n_items_calibration` and the interval are **recomputed from the
  frozen export CSVs** as the report is built, with pragmata's own IAA implementation - not
  read out of the export's `iaa/report.json`. That file records no seed, and a re-export
  overwrites the CSVs beside it without regenerating it, so its interval could neither be
  re-derived nor be trusted to describe the rows in this table.
- `alpha` itself is **analytic** (`1 - Do/De` off the coincidence matrix); only
  `alpha_ci_low` / `alpha_ci_high` are bootstrapped, at 1000 resamples with seed 0 at the
  0.95 level (all three recorded in the `*-provenance.json`). So the point estimate moves
  only when the underlying calibration data does, while the bounds also move if those
  parameters change - and, being seeded, they re-derive exactly from the same tree.
- **`alpha = 1.0` with `degenerate_calibration = True` is not evidence of reliability.**
  Alpha is `1 - Do/De` and is undefined when expected disagreement is zero (the label never
  varies in the overlap); pragmata returns 1.0 there by convention.
- Consolidation is pragmata's own `consolidate_labels_by_majority` - the function eval
  scoring ingests through - so these counts are eval's by construction. A label with a
  strict majority (> half positive) is decided independently; a tied label (a 1-of-2 split)
  takes its value from the row that matches every strict-majority label, and only from the
  group's first row in file order when no row does. Either way a tie is settled by row
  selection rather than by the data.

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
| `score` | The retriever's own relevance value for this chunk, as the bot reported it. Blank where the run that produced the data did not capture it - the bot response is not retained, so it cannot be filled in afterwards. |
| `n_retrieved_chunks` | Chunks this query retrieved (query grain, repeated across its rows). |
| `panel_started` | Query grain repeated across its rows, **retrieval only**: at least one chunk of this query's retrieval panel received a submitted response. *Started*, not complete - contrast `n_panels_complete` in `annotation_operations.csv`. |
| `n_chunks_annotated` | Query grain, repeated across its rows: how many of the query's chunks got a submitted response. |
| `language`, `role`, `topic`, `intent`, `difficulty`, `format`, `spec_stem`, `retried` | Per-query metadata from the querygen spec, carried through unchanged. |
| `query_task` | The querygen spec's own `task` - a description of what the query asks for (e.g. "extract evidence refuting a claim"). |

**Caveats.**

- The source is the curated corpus, a superset of what was annotated - 464 of 1143 queries are annotated.
- `panel_started` and `n_chunks_annotated` are the only columns here derived from
  annotation state; every other column comes from the curated corpus. They exist because the join that would reproduce them is not available from this bundle: the exports carry no
  `query_id`, and this file carries no query text. (TODO-DEFERRED - fix this in pragmata)
- **Row fan-out is per retrieved passage, one per document.** Because `chunk_id` is
  `<doc_id>-c1`, document *frequency* means counting rows and distinct *documents* means
  deduplicating on `(query_id, doc_id)` - identical to deduplicating on `(query_id,
  chunk_id)`. Avoid joining on `doc_id`/`chunk_id` alone: 739 of the 1092 retrieved
  documents are returned for more than one query (one for 72 of them), so a document-only
  join multiplies rows across unrelated queries.
- Joining to the annotation exports. They carry no `query_id`, only `record_uuid` (TODO-DEFERRED - fix this in pragmata). Join
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


## `synthetic_metric_estimates.csv`

Written as `synthetic_metric_estimates.<population>.csv`, one file per predicted population.

- **Purpose:** the same corpus metric taxonomy as `eval_metric_estimates.csv`, scored on a
  *synthetic evaluator's* predictions instead of on human labels. Produced by the same
  `pragmata eval score` CLI, from the same eval pin, with `--prediction-id` in place of
  `--path`.
- **Grain:** one row per task x metric, over one predicted population.

Every column means exactly what it means in
[`eval_metric_estimates.csv`](#eval_metric_estimatescsv) - the row builder is literally shared -
with two differences and three additions:

| Column | Definition |
|---|---|
| *(the `alpha_*` four)* | **Absent by definition.** A prediction has one label per item, so there is no annotator disagreement to measure and no calibration population to measure it on. What replaces them is not in this file: it is the evaluator's own quality, in `evaluator_metrics.csv`. |
| `policy` | The same two-part slug shape, with `pred-` as the first part: `pred-complete` (the reportable one) or `pred-allpanels`. `calib-`/`prod-` cannot apply - predictions carry no annotator, so nothing was double-annotated. |
| `source_labels` | Unchanged in meaning: which label(s) the *metric's formula* reads. It is what says whether a metric rests on a label this evaluator was trained for at all. |
| `evaluator_run_id` | The training run whose model produced the predictions. Joins to `evaluator_metrics.csv`'s `training`, and to that run's `train_provenance.workspace.json`. |
| `prediction_id` | The prediction directory these numbers were scored from, `<evaluator_run_id>-<population>`. What `eval score --prediction-id` was given. |
| `population` | Which unlabelled rows were predicted: `annotated` (the frozen export with labels stripped - the rows the human metrics describe) or `corpus` (the curated corpus, most of which was never annotated). |
| `status` | As in the human CSV, plus **`evaluator_labels_incomplete`**: the evaluator does not predict a label pragmata's score contract requires, so the metric was not computed. This is grounding, always - see below. |

**Caveats.**

- **Read this file with `evaluator_metrics.csv` open.** A corpus rate produced by a model whose
  AUC is near chance is not a measurement. The intervals here cover sampling uncertainty over
  queries *only* - they say nothing about the evaluator being wrong, which is the dominant
  source of error for two of the three tasks.
- **`population = annotated` is largely in-sample.** Each evaluator's train and validation
  splits are roughly three quarters of exactly these items (retrieval 1,152 of 1,561; grounding
  335 of 447; generation 530 of 713). A synthetic estimate on this population is therefore
  optimistic about the evaluator by an unknown amount. It is the right population for "does the
  evaluator reproduce the human metric" and the wrong one for "how good is the evaluator".
- **`population = corpus` has no human baseline at all**, and for **grounding and generation it
  carries the evaluator-quality caveat in full**: generation's evaluator shows majority-class
  collapse on its two highest-prevalence labels (F1 0.93 against an AUC of 0.53), and
  grounding's is directional at best on two of its three trained labels. Read those corpus rates
  as an indication of what a better evaluator would be measuring.
- **Grounding rows are always `n = 0`.** Its evaluator trains on three of five labels, and
  pragmata's grounding score schema requires all five (it is built from the label map at import
  time, so the narrowing that makes training possible cannot reach it). The rows are written
  explicitly rather than omitted, so the gap is visible; inventing the two missing columns would
  be fabricating labels.
- **Retrieval's population differs sharply between the two files.** `--skip-incomplete-panels`
  drops 283 of 464 panels on the `annotated` population, exactly as it does for the human run,
  because annotation coverage is partial. Corpus panels are complete by construction (every
  chunk of a record is staged), so a corpus run drops none and `n_panels_skipped` is 0. The two
  `n` values are not comparable without reading `n_panels_skipped` beside them.

## `evaluator_metrics.csv`

- **Purpose:** how good each synthetic evaluator is, per label. The file to read beside any
  `synthetic_metric_estimates.*.csv` number.
- **Grain:** one row per task x label x training run, on **that run's own held-out test split**.

| Column | Definition |
|---|---|
| `task` | `retrieval`, `grounding` or `generation`. |
| `label` | One of the task's label columns - but only the ones this run actually trained. |
| `training` | The evaluator training run id (e.g. `a1b33eec8c9c41f181c61cbd8400913a`). Opaque on purpose: it is the join key to that run's own records - `train_provenance.workspace.json` beside its checkpoints, and the prediction directories named after it - where a friendly name would identify a configuration rather than the run that produced these numbers. |
| `roc_auc` | Area under the ROC curve for this label, as `tlmtc` reported it. The discrimination measure to trust: it is threshold-independent, where `f1`/`precision`/`recall` all depend on the run's decision threshold. |
| `accuracy` | Fraction of test rows classified correctly. **Derived, not persisted** - see below. Blank on degenerate labels. |
| `f1`, `precision`, `recall` | At the run's own persisted decision threshold, as `tlmtc` reported them. |
| `n` | Rows in the run's held-out test split (`data/test.parquet`) - 409 retrieval, 112 grounding, 183 generation. The same value on every row of a task, because every label is scored on the same split. |

**How `accuracy` is derived.** `tlmtc` persists `f1`, `precision`, `recall`, `roc_auc`,
`pr_auc`, `true_prevalence` and `pred_prevalence` per label, and *not* accuracy. Three of
those (`true_prevalence`, `recall`, `pred_prevalence`), with the split size `n`, pin the
whole 2x2 table, so the reconstruction is exact rather than approximate:

```
P  = true_prevalence * n      TP = recall * P       PP = pred_prevalence * n
FP = PP - TP                  FN = P - TP           TN = n - TP - FP - FN
accuracy = (TP + TN) / n
```

Every one of those is a count of test rows and must come out whole; each is checked to within
0.01 rows and the run **aborts** on a miss, because publishing a plausible accuracy derived from
the wrong model of these metrics is worse than failing. The derivation and the tolerance are also
written into the `*.provenance.json`, since a derived column that appears in no input is the one
thing a reader of the CSV cannot check.

**Caveats.**

- **`accuracy` is blank where `pred_prevalence = 0`** - the evaluator never predicts the label
  positive. (Not keyed on `precision = 0`, which is ambiguous: precision also reads 0.0 when the
  model makes positive predictions that are all wrong, and there accuracy is computed normally.)
  Where nothing is predicted positive, accuracy *is* still determined (`TP = FP = 0`, so it
  equals `1 - true_prevalence`), and it is deliberately left blank anyway - filled in, it reads
  as performance, when what it measures is the prevalence of the negative class. The blank plus
  the `f1`/`precision`/`recall` zeros beside it say what happened. In the current runs that is
  `grounding/contradicted_claim_present`, `grounding/fabricated_source` and
  `generation/unsafe_content`.
- **Grounding contributes three labels, not five.** `support_present` and `source_cited` have
  too few negative items for any split ratio to give `tlmtc` full class support, so they are not
  trained and have no row here. Their absence is recorded in the `*.provenance.json`
  (`labels_not_trained`) rather than written as blank rows, because a blank row in a metrics
  table reads as a measurement of zero. See [Eval training](eval-training.md).
- **`accuracy` is a weak summary on skewed labels, and most of these are skewed.**
  `generation/response_on_topic` has `true_prevalence` 0.93: always predicting positive scores
  0.93. Compare `roc_auc` (0.53) for what the model actually discriminates.
- **`n` is small.** 112 grounding test rows means a single flipped prediction moves a rate by
  ~0.9 points, and the two labels with 2-4 test positives move AUC by 0.2-0.3. Treat those as
  directional.
- These are the *evaluator's* metrics, not the corpus's. Nothing here describes the
  publikationsbot; it describes how well a model reproduces human labels on held-out annotated
  data.

## `evaluator_calibration.csv`

- **Purpose:** whether an evaluator's stated probabilities mean what they say - the reliability
  data behind a calibration curve.
- **Grain:** one row per task x label x probability bin, on **that run's own held-out test
  split**. Bins with no rows in them are omitted, so a task x label has at most 10 rows and
  usually fewer.

| Column | Definition |
|---|---|
| `task` | `retrieval`, `grounding` or `generation`. |
| `label` | One of the run's trained labels (grounding: three of five, as above). |
| `prob_bin` | One of ten fixed-width bins over the predicted probability: `[0.0,0.1)`, `[0.1,0.2)`, ... `[0.9,1.0]`. Fixed rather than quantile bins, because the question is whether a stated probability is accurate - a claim about the value, not about its rank - and because fixed edges let two runs' rows line up. The top bin is closed so a probability of exactly 1.0 has somewhere to go. |
| `mean_pred` | Mean predicted probability of the rows in this bin. Plot against `frac_true`; a perfectly calibrated model puts every point on the diagonal. |
| `frac_true` | Fraction of rows in this bin whose held-out label is actually positive. |
| `n` | Rows in this bin. **Load-bearing - see the caveat.** |

**Caveats.**

- **`n` is the first column to read, not the last.** The test splits are 409 / 112 / 183 rows,
  spread over up to 10 bins per label. In the current runs retrieval's bins hold 9-70 rows
  (median 41.5), which supports a curve; grounding's one usable label spreads 112 rows over ten
  bins and seven of them hold 1-3 rows (median 3), which does not. A `frac_true` computed on 3
  rows can only take four values: such a point is noise, not miscalibration. Read bins under
  roughly 20 rows as part of a trend, never individually.
- **Empty bins are skipped, not written as zeros.** A row of zeros would read as "the model was
  right 0% of the time here", where an absent row is the absence of evidence it actually is. So a
  label whose predictions never leave the bottom bin has exactly one row - which is what
  majority-class collapse looks like here, and is itself the finding:
  `grounding/contradicted_claim_present`, `grounding/fabricated_source` and
  `generation/unsafe_content` each produce a single `[0.0,0.1)` row covering the whole split.
- **The probabilities are re-predicted, not read from a training artifact.** `tlmtc` persists
  aggregate and per-label metrics, not the per-row scores behind them, so each run is applied
  again to its own test split through the ordinary prediction path
  (`predict_evaluators.py`, `population=testsplit`). The probabilities are joined back to the
  held-out labels on a row-index column carried through prediction - not zipped by position -
  and the join is checked to cover every test row exactly once.
- **Reproducibility.** `tlmtc` inference is deterministic given the same model artifacts and
  batching, so a re-run reproduces these bins; `mean_pred` may move in the last decimal with a
  different `BATCH_SIZE` (floating-point accumulation), which does not move any bin membership.
- Calibration is a property of the *scores*, not of the decision. A well-calibrated model can
  still have a badly chosen threshold, and vice versa: read this beside `evaluator_metrics.csv`,
  where `roc_auc` is threshold-free and `f1`/`precision`/`recall` are not.

## Appendix - Implications for editing this doc (pipeline deps)
>3 points in the pipeline depend on this md by path and by hash:
>
> - **Injected into every deliverable.** `scripts/lib/workspace.py` (`DATA_DICTIONARY`) writes
>   this file's `{path, sha256}` into every `*.provenance.json`, and `write_csv()` copies the
>   file itself into the output directory beside the CSVs whose `.provenance.json` carries that pin. The
>   eval scripts refuse to run if it is missing.
> - **Pinned by the committed record.** `reproducibility/<date>-eval-report/pins.sha256`
>   pins the copy that travelled with that date's CSVs, at the hash this file had then -
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
