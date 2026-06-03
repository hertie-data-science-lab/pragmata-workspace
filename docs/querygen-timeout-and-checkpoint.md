# Plan: Upstream-PR-quality robustness fixes for pragmata querygen

## Context

Pragmata's two-stage querygen pipeline (planning → realization) accumulates batch results in Python lists in memory and only writes them to disk after each *entire* stage completes. If a stage hangs or crashes mid-batch — as observed twice today on `sustainable_communities_de` Stage 2 — every completed batch in memory is lost. The same vulnerability exists at Stage 1; we have just been lucky that hangs landed in Stage 2.

Root cause of today's hangs is `langchain → httpx` with `timeout=None`. We have mitigated this at the workspace yaml level for future runs, but two upstream gaps remain:

1. `pragmata.core.querygen.llm.build_llm_runnable` ships with no default timeout, so every consumer must remember to set one.
2. Neither stage persists intermediate state, so any failure (timeout, crash, SIGKILL from wrapper, OOM) loses the whole stage.

The deliverable is **upstream pragmata PRs**, written to upstream-review quality (full type hints, pydantic schemas, mirrored test layout, no scope creep, generic across all langchain model providers). The same commits land on the local `demo-2026-05-26` branch immediately for the imminent demo.

**Guiding principle (folded in 2026-05-28): maximal write-to-disk-as-soon-as-ready, minimal disruption to the existing workflow.** Persist every durable unit of work at its natural boundary the moment it is ready, so a crash/hang loses at most one in-flight batch and completed work is extractable per-batch rather than only after the whole run finishes. Equally: keep the change *additive* wherever possible — same staged pipeline, same on-disk CSV format and location, same wrapper scripts and bot phase — and only alter existing behaviour where doing so is genuinely useful. Concretely this adds, beyond the two per-batch checkpoint stages:

- a frozen Stage-1 result artifact (`selected_blueprints.json`) that marks "Stage 1 done" and pins the Stage 1 → Stage 2 handoff (so Stage 2 resume skips re-dedup and the embedding-model load, and validates against an immutable set);
- the per-batch Stage 2 realization artifacts doubling as the resume store, with the final `synthetic_queries.csv` treated as a **deterministic projection** of `selected_blueprints.json` + the realization artifacts — the CSV format and write path are unchanged (no fragile incremental append; see Design decisions for why);
- the cross-run planning-summary written at the **end of Stage 1** (in addition to the existing end-of-run write), so a Stage 2 crash still leaves usable planning memory for future runs;
- **resume-by-default** (driven by artifact presence + header/drift checks; same `run_id` just works) with an opt-out `--fresh` flag.

A full safety analysis — that none of this undermines the planning-memory chain, diversification, or dedup, and that it *closes* the partial-resume content-drift edge — is recorded in the "Validation" section below. Out of scope by the same principle: interleaving Stage 1 into Stage 2 (streaming dedup) and bot-phase pipelining (orchestration change in `run_overnight.sh`) — both are useful but disrupt the existing workflow more than they are worth right now.

**Urgency note (2026-05-27 ~12:00 UTC):** the in-flight `qgen-resume` recovered from what appeared to be a hang earlier today — Stage 2 of `sustainable_communities_de` completed at 11:55 UTC (248/248 queries delivered). We are not currently bleeding work, so these PRs can move at upstream-review pace, not emergency pace. The structural risk remains real (today's earlier 02:47→09:35 hang was diagnosed as `httpx.Client(timeout=None)`) and will recur on future long runs without these fixes.

## Review outcomes & revisions (2026-05-28)

Two skeptical reviews (SOTA-correctness + codebase-feasibility) ran against this plan. Decisions taken and revisions that **override earlier text** below:

**Decisions**
- **Keep both stages' per-batch checkpoints + the frozen handoff.** The feasibility review proposed dropping Stage 1 checkpoints as "cheap", but at high reasoning Stage 1 is ~2× Stage 2's LLM calls (planning + summary per batch vs. one realization call), so a Stage 1 crash-before-freeze loses *more* work. Symmetry is justified, not gold-plating. PR ordering unchanged: **PR 2 ships first and alone** (it is the actual fix for the incident; root cause already partly mitigated by the wrapper's `timeout 4h` and the yaml).
- **`query_id` stays positional; scope the determinism claim instead of changing the format.** Changing `query_id` to be `candidate_id`-keyed would alter values that the publikationsbot resume and annotation-import depend on — violates "minimal disruption". Instead: keep `{run_id}_q{n}`, and make the load-bearing invariant explicit.

**Correctness revisions (supersede conflicting statements elsewhere in this doc)**
- **Byte-identity is conditional.** The "full resume / partial-Stage-2 resume → byte-identical CSV" guarantee holds only when (a) the frozen `selected_blueprints.json` is reused and (b) the realized set is *complete* (`len(filtered_realization_outputs) == len(selected_blueprints)`). The api layer must assert (b) before relying on positional `query_id` stability. Embedding-based dedup (`all-MiniLM-L6-v2`) is bit-reproducible only under a fixed `torch`/`sentence-transformers`/device/dtype/thread/batch configuration and a strict `>=` tolerance, so do **not** promise byte-identity across a *re-derived* Stage 1 (only across reuse of the frozen result). Tests and verification steps that assert unconditional byte-identity are to be reworded accordingly.
- **Invalidation key extends beyond `pragmata_version`.** `selected_blueprints.json` is a function of the embedding stack. Add the embedding model checkpoint name and `importlib.metadata.version("sentence-transformers")` to its header (and validate them on read). This is a lightweight step toward the content-addressed fingerprinting that HF `datasets`/DVC/distilabel use; a full single-hash-of-effective-config is noted as a future improvement, not v1.
- **Atomic-write safety rationale corrected.** The reason no-fsync is acceptable is **not** "rename is all-or-nothing"; it is that a torn/zero-length file fails `ValidationError`/`JSONDecodeError` on read → treated as drift → recompute. Keep no-fsync (house style), but state the real reason.
- **Tempfile names must be uniquified.** Resume-by-default invites a same-`run_id` race (half-dead tmux + relaunch). The fixed `path.with_suffix(".json.tmp")` name is only single-writer-safe. Use a PID/uuid-qualified suffix (e.g. `f".{os.getpid()}.{uuid4().hex}.tmp"`) so concurrent writers cannot clobber each other's tempfile; `Path.replace` to the final name stays atomic.
- **Drop the orphan-`.tmp` sweep.** Unspecified scope creep; the read side only ever loads `batch_NNNN.json` / `selected_blueprints.json`, and `export_runner.py` already cleans up its own tmp on failure. Uniquified tmp names make orphans rare and harmless.
- **`fresh` is a `gen_queries` parameter, not a setting.** It is a per-invocation control flag, not persisted config; putting it in `QueryGenRunSettings` would deep-merge from yaml and round-trip into `QueryGenRunResult.settings`. Add it as a direct `fresh: bool = False` arg on `gen_queries` plus a `--fresh/--no-fresh` CLI flag, bypassing the settings model.
- **Helper signatures intentionally diverge from precedent.** The existing `read_planning_summary_artifact(artifact_path, spec)` / `export_planning_summary(artifact, artifact_path)` are positional 2-arg functions; the new `*`-keyword `expected_*` helpers are a deliberate (richer) shape, not a "mirror". Stated honestly so reviewers aren't misled.
- **Test fixture needs real work.** `_install_default_workflow_stubs` (`tests/unit/api/test_querygen.py:164-316`) covers `run_planning_stage`/`run_realization_stage` but has no stubs for the new read/export/assemble helpers; budget ~6 new stubs (export helpers must write under `tmp_path`, not real run dirs).

**Corrected code references** (the body uses approximate line numbers): Stage 1 loop `querygen.py:218-239`, filter+dedup `241-249`, Stage 2 loop `261-271`, end-of-run summary `308-317`; atomic-write precedent is `record_builder.py:205-214` (`write_partition_manifest`) and `export_runner.py:96-113` (`write_export_csv`, which *does* clean up its tmp). `read_planning_summary_artifact` raising on malformed JSON is incidental (`json.loads`), not a designed contract — the new helpers wrap `json.loads` + `model_validate` deliberately.

## Scope: three PRs

### In scope

| PR | Subject | Scope |
|---|---|---|
| **PR 1a** | `feat(querygen): persist Stage 1 batches and result, resume on rerun` | Per-batch planning checkpoints (`planning_batches/batch_NNNN.json`) **+ the frozen Stage-1 result artifact `selected_blueprints.json`** written after dedup **+ the cross-run planning-summary written at end of Stage 1** + matching api-layer logging. Foundation for PR 1b's correctness. |
| **PR 1b** | `feat(querygen): persist Stage 2 batches as resume store, project CSV from artifacts` | Per-batch realization checkpoints (`realization_batches/batch_NNNN.json`) that double as the resume store; **Stage 2 reads `selected_blueprints.json` to skip Stage 1 wholesale on resume**; the final CSV is **assembled as a deterministic projection** of the frozen result + realization artifacts (format/path unchanged); **resume-by-default + `--fresh` opt-out**; matching api-layer logging. Depends on 1a's frozen Stage-1 result. |
| **PR 2** | `feat(querygen): set default request timeout on LLM client` | One-line default in `llm.py` (`int 600`); two tests. Independent of PR 1a/1b. Ships first. |

Bundling both stages into a single "PR 1" was tempting, but reviewer feedback flagged that Stage 1's nondeterminism would silently invalidate every Stage 2 checkpoint, so Stage 1 checkpointing is necessary for Stage 2 checkpointing to be reliably usable. Sequencing them lets reviewers digest the pattern in PR 1a and the symmetric extension in PR 1b. The frozen `selected_blueprints.json` (in 1a) is what makes the dependency airtight: once Stage 1's deduplicated output is on disk, Stage 2 (1b) validates and resumes against an immutable set, so partial-Stage-1 nondeterminism can no longer reach Stage 2 at all.

### Out of scope (deferred to separate follow-up PRs)

- **PR 3 — classified retry semantics** in `llm.py` (transient vs permanent exceptions). Worth doing but doesn't help today's pain — our hangs fire before any exception is raised.
- **PR 4 — token-usage + duration metrics** in the per-batch logs and checkpoint headers. Cost attribution + early-warning signal. Coupled to PR 1's log format.
- **PR 5 — concurrent batches** via `asyncio.gather`. Performance, not safety. Needs PR 1a/1b + PR 4 stable.
- **Persistent rate-limiter state.** No current 429 pressure; skip indefinitely.
- **In-process batch-skip recovery.** When a batch raises after retries exhaust, the current code wraps it in a stage-level error and the process exits. PR 1a/1b only protects *cross-process* restarts (kill + relaunch). In-process skip-and-continue is a larger semantic change (do you mark the batch failed forever? re-attempt at end of stage? abort?). Document explicitly as a known limitation; not addressed.
- **Streaming Stage 1 into Stage 2 (incremental dedup).** `deduplicate_blueprints` only ever removes *later* near-duplicates, so survival is in principle decidable incrementally and Stage 1 could feed Stage 2 as it goes. This would interleave the two stages and complicate the planning-memory chain timing — a real redesign that breaks the clean staged model. Violates "minimal disruption"; not now.
- **Bot-phase pipelining.** The per-batch realization artifacts make completed queries durable mid-run, but having the publikationsbot *consume* them before querygen finishes needs an orchestration change in `scripts/run_overnight.sh` (a tailer over the growing output), not a pragmata change. The persistence here is the prerequisite; the pipelining is a separate follow-up.
- **Literal incremental CSV append.** Considered and rejected in favour of CSV-as-projection — see Design decisions for the `candidate_id`/positional-`query_id` reasons.

## Files modified — full list (PR 1a + PR 1b)

### New

- `pragmata/core/querygen/planning_batches.py` — **read** helper for Stage 1 batch artifacts. Pure I/O + validation; **no logging**. (PR 1a)
- `pragmata/core/querygen/selected_blueprints.py` — **read** helper for the frozen Stage-1 result artifact `selected_blueprints.json`. Same read/validate shape as `planning_batches.py`. (PR 1a)
- `pragmata/core/querygen/realization_batches.py` — **read** helper for Stage 2 batch artifacts. Same shape. (PR 1b)
- `tests/unit/core/querygen/test_planning_batches.py` — unit tests for the read helper (round-trip, header validation, drift).
- `tests/unit/core/querygen/test_selected_blueprints.py` — unit tests for the result-artifact read helper (round-trip, header validation, drift). (PR 1a)
- `tests/unit/core/querygen/test_realization_batches.py` — same, for Stage 2. (PR 1b)
- Resume integration tests **merged into the existing** `tests/unit/api/test_querygen.py` rather than a new file, to reuse the `_install_default_workflow_stubs` fixture at lines 164-316 (style reviewer flagged duplication or fragile cross-test imports otherwise).

### Modified

- `pragmata/api/querygen.py` — replace Stage 1 loop (lines ~219-240) and Stage 2 loop (lines ~258-271) with calls to two new private helpers `_resume_or_run_planning_batch` and `_resume_or_run_realization_batch` (defined in the same file, NOT in `core/querygen/` — keeps logger in api layer). After the Stage 1 loop + `deduplicate_blueprints`, **read-or-write `selected_blueprints.json`**: if a valid frozen result exists, load it and **skip the entire Stage 1 loop + dedup**; otherwise run Stage 1, dedup, then write it. **Move the cross-run planning-summary write to the end of Stage 1** (keep an idempotent end-of-run write too). Stage 2 then chunks the frozen `selected_blueprints` and resumes per batch; the final `assemble_query_rows` + `export_queries` consume the (possibly checkpoint-reconstructed) realization outputs unchanged. Add `spec_fingerprint = fingerprint_querygen_spec(settings.spec)` as an explicit named binding before the existing `paths = resolve_querygen_paths(...)` call. Add `from pydantic import ValidationError` to imports. Honour a new `fresh: bool` setting that, when true, ignores all on-disk checkpoints/artifacts for this run.
- `pragmata/core/querygen/export.py` — add `export_planning_batch_artifact`, `export_realization_batch_artifact`, **and `export_selected_blueprints`**, matching the existing `export_planning_summary` pattern (write side of the read/write split per `pragmata/core/querygen/export.py:31-44` and `pragmata/core/querygen/planning_summary.py:61-87` precedent). Atomic-write uses the existing pragmata idiom: tmpfile via `path.with_suffix(path.suffix + ".tmp")` in target dir, then `Path.replace(target)`. NO fsync — matches `record_builder.py:199-208` and `export_runner.py:74-109` precedent. The predictable `.json.tmp` suffix makes orphan-cleanup possible (on dir scan, ignore anything not matching `batch_NNNN.json`). `selected_blueprints.json` is a single whole-file atomic write (rename), so it is never observed half-written — no incremental-append fragility.
- `pragmata/core/paths/querygen_paths.py` — add `planning_batches_dir` and `realization_batches_dir` to `QueryGenRunPaths`; resolve as `run_dir / "planning_batches"` and `run_dir / "realization_batches"`; create both in `ensure_dirs`; **add `selected_blueprints_json = run_dir / "selected_blueprints.json"`** (a file in `run_dir`, no new dir). Extend the `Attributes:` docstring block. PR 1a adds `planning_batches_dir` + `selected_blueprints_json`; PR 1b adds `realization_batches_dir`.
- `pragmata/core/schemas/querygen_output.py` — add `PlanningBatchArtifact` (PR 1a), **`SelectedBlueprintsArtifact` (PR 1a)**, and `RealizationBatchArtifact` (PR 1b), alongside the existing `PlanningSummaryArtifact`. All use `ConfigDict(extra="forbid")` and include a `model_validator` with docstring matching `SyntheticQueriesMeta.validate_query_counts` style (return annotation as string form `-> "PlanningBatchArtifact"`, not `Self` — file-local consistency).
- `pragmata/core/querygen/assembly.py` — add `assemble_planning_batch_artifact` (PR 1a), **`assemble_selected_blueprints_artifact` (PR 1a)**, and `assemble_realization_batch_artifact` (PR 1b). Each stamps `created_at = datetime.now(UTC)` and `pragmata_version = importlib.metadata.version("pragmata")` internally. Matches `assemble_planning_summary` precedent. `assemble_query_rows` is unchanged — the CSV stays a pure projection of `selected_blueprints` + realized queries.
- `pragmata/cli/...` (querygen subcommand) — add a `--fresh/--no-fresh` flag (default `--no-fresh`, i.e. resume-by-default) plumbed through to the `fresh` setting. Matches the existing CLI-knob plumbing added in `feat(cli): expose querygen runtime and throttle options`.

### Modified (PR 2 — separate, ships first)

- `pragmata/core/querygen/llm.py` — add `"timeout": 600` (int) to `init_kwargs` between `rate_limiter` and the `if model_kwargs: init_kwargs.update(model_kwargs)` line. Update docstring with override path. Reviewer flagged: `int` not `float` because `ChatMistralAI.timeout: int = 120` rejects floats; OpenAI/Azure accept both.
- `tests/unit/core/querygen/test_llm.py` (existing) — extend with three tests: default timeout applied (Azure-double), default timeout applied (Mistral-double, asserts int round-trips through pydantic), caller `model_kwargs["timeout"]` overrides.

## Schemas (PR 1a + PR 1b)

```python
# pragmata/core/schemas/querygen_output.py — added next to PlanningSummaryArtifact

class PlanningBatchArtifact(BaseModel):
    """Persisted result of one Stage 1 planning batch.

    Written atomically to ``<run_dir>/planning_batches/batch_NNNN.json``
    after each successful Stage 1 batch invocation. Used to skip already-
    planned batches on rerun of the same run_id (subject to header-field
    matches).
    """
    model_config = ConfigDict(extra="forbid")

    spec_fingerprint: NonEmptyStr
    pragmata_version: NonEmptyStr
    source_run_id: NonEmptyStr
    n_queries: PositiveInt
    batch_size: PositiveInt
    batch_idx: NonNegativeInt
    candidate_ids: list[NonEmptyStr]
    blueprints: list[QueryBlueprint]
    planning_summary_state: PlanningSummaryState | None
    created_at: datetime

    @model_validator(mode="after")
    def validate_blueprint_count(self) -> "PlanningBatchArtifact":
        """Enforce 1:1 mapping between batch candidate_ids and produced blueprints."""
        if len(self.candidate_ids) != len(self.blueprints):
            raise ValueError(
                "candidate_ids and blueprints must have equal length"
            )
        return self


class RealizationBatchArtifact(BaseModel):
    """Persisted result of one Stage 2 realization batch.

    Written atomically to ``<run_dir>/realization_batches/batch_NNNN.json``
    after each successful Stage 2 batch invocation.
    """
    model_config = ConfigDict(extra="forbid")

    spec_fingerprint: NonEmptyStr
    pragmata_version: NonEmptyStr
    source_run_id: NonEmptyStr
    n_queries: PositiveInt
    batch_size: PositiveInt
    batch_idx: NonNegativeInt
    candidate_ids: list[NonEmptyStr]
    queries: list[RealizedQuery]
    created_at: datetime

    @model_validator(mode="after")
    def validate_query_count(self) -> "RealizationBatchArtifact":
        """Enforce 1:1 mapping between batch candidate_ids and realized queries."""
        if len(self.candidate_ids) != len(self.queries):
            raise ValueError(
                "candidate_ids and queries must have equal length"
            )
        return self


class SelectedBlueprintsArtifact(BaseModel):
    """Persisted frozen result of Stage 1 (post-filter, post-dedup).

    Written atomically to ``<run_dir>/selected_blueprints.json`` once, after
    the Stage 1 loop and ``deduplicate_blueprints`` complete. Its existence is
    the authoritative "Stage 1 is done" marker: on resume, if a valid artifact
    is present the entire Stage 1 loop and dedup (including the
    SentenceTransformer model load) are skipped and Stage 2 chunks/validates
    against this immutable ``blueprints`` list.

    No ``batch_size`` of its own beyond the header echo: the dedup output is a
    single global result, not batched.
    """
    model_config = ConfigDict(extra="forbid")

    spec_fingerprint: NonEmptyStr
    pragmata_version: NonEmptyStr
    source_run_id: NonEmptyStr
    n_queries: PositiveInt
    batch_size: PositiveInt
    near_duplicate_tolerance: float
    blueprints: list[QueryBlueprint]
    created_at: datetime
```

`SelectedBlueprintsArtifact` adds `near_duplicate_tolerance` to its header because the dedup result depends on it: changing the tolerance between runs (same `run_id`) must invalidate the frozen result and force a re-dedup. The other header fields mirror the batch artifacts.

Header fields (`spec_fingerprint`, `pragmata_version`, `source_run_id`, `n_queries`, `batch_size`) collectively catch silent-corruption cases:
- `spec_fingerprint` — user edited the spec yaml between runs (same run_id).
- `pragmata_version` — user upgraded pragmata between runs (prompt templates / schema may have changed without spec touch). Addresses reviewer N-C1/N-H4.
- `source_run_id` — defensive against checkpoint-dir collision via misconfiguration.
- `n_queries` / `batch_size` — the "bumped N from 200 to 250" / "changed batch size" cases.

A mismatch on any of these invalidates the entire checkpoint dir for this run, surfaces a specific drift-reason in the log line, and re-runs from the first drifting batch onward.

Field naming/typing follows the immediately adjacent `PlanningSummaryArtifact` precedent (`NonEmptyStr` for IDs, `source_run_id` name, string-form validator return annotation). `pragmata_version` is set via `importlib.metadata.version("pragmata")` inside the assemble helper.

## Module surface (PR 1a; mirrored in PR 1b)

Read helpers live in `pragmata/core/querygen/planning_batches.py` (and the Stage 2 sibling). Write helpers live in `pragmata/core/querygen/export.py` alongside the existing `export_planning_summary`. This split matches the existing pragmata precedent (`read_planning_summary_artifact` in `planning_summary.py` + `export_planning_summary` in `export.py`).

```python
# pragmata/core/querygen/planning_batches.py — READ only

def read_planning_batch_artifact(
    *,
    path: Path,
    expected_spec_fingerprint: str,
    expected_source_run_id: str,
    expected_n_queries: int,
    expected_batch_size: int,
    expected_candidate_ids: list[str],
) -> PlanningBatchArtifact | None:
    """Load and validate a planning-batch artifact at ``path``.

    Returns None if (a) the file does not exist, or (b) the artifact loads
    successfully but its header (spec_fingerprint, pragmata_version,
    source_run_id, n_queries, batch_size) or its candidate_ids do not
    match the expected values — i.e. the checkpoint was written by an
    incompatible run.

    Raises pydantic.ValidationError on schema mismatch (older/newer
    artifact format) and json.JSONDecodeError on malformed file content.
    The api layer catches these, logs, and treats the dir as drifted.
    """
```

```python
# pragmata/core/querygen/export.py — WRITE side, added alongside export_planning_summary

def export_planning_batch_artifact(
    *,
    artifact: PlanningBatchArtifact,
    path: Path,
) -> None:
    """Atomically persist a planning-batch artifact to ``path``.

    Contract:
    - Tempfile written at ``path.with_suffix(path.suffix + ".tmp")``
      (same dir as target; predictable name so orphan .tmp files from
      interrupted writes can be swept by the caller).
    - ``Path.replace(target)`` for the atomic rename — matches the
      existing pragmata convention in ``record_builder.py:199-208``
      and ``export_runner.py:74-109``. No explicit fsync (consistent
      with house style; durability tradeoff documented in the
      "Risks" section).
    - On any OSError, raises and leaves no .json file at target
      (the .tmp may remain — see orphan-cleanup note).
    """
```

The api layer owns the resume-control flow and all logging. Read helpers raise typed errors; api catches `ValidationError` / `JSONDecodeError`, logs a specific drift reason, and re-runs from that batch forward.

## Helper extraction (PR 1a; `api/querygen.py`)

To keep `gen_queries` from ballooning, the per-batch resume-or-run logic moves into a private module-level helper. The Stage 1 loop in `gen_queries` becomes a thin enumeration that delegates to this helper for the resume decision, logging, and persistence — keeping logger calls in the api layer per the style reviewer's BLOCKING finding.

```python
# pragmata/api/querygen.py — new private helper near top of module

def _resume_or_run_planning_batch(
    *,
    paths: QueryGenRunPaths,
    settings: QueryGenRunSettings,
    spec_fingerprint: str,
    pragmata_version: str,
    api_key: str,
    batch_idx: int,
    total_batches: int,
    batch_candidate_ids: list[str],
    planning_summary_state: PlanningSummaryState | None,
    drift_in_dir: bool,
) -> tuple[list[QueryBlueprint], PlanningSummaryState | None, bool]:
    """Resume one Stage 1 batch from a valid checkpoint, or run it fresh and persist.

    Returns ``(batch_blueprints, updated_summary_state, drift_in_dir_after)``.

    Drift sticks: once any prior batch in this run reported drift, all subsequent
    batches skip the read attempt and re-run fresh.
    """
    ckpt_path = paths.planning_batches_dir / f"batch_{batch_idx:04d}.json"
    artifact: PlanningBatchArtifact | None = None

    if not drift_in_dir:
        try:
            artifact = read_planning_batch_artifact(
                path=ckpt_path,
                expected_spec_fingerprint=spec_fingerprint,
                expected_source_run_id=settings.run_id,
                expected_n_queries=settings.n_queries,
                expected_batch_size=settings.batch_size,
                expected_candidate_ids=batch_candidate_ids,
            )
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning(
                "Stage 1 batch %d/%d: checkpoint %s unreadable (%s); "
                "redoing this and all subsequent batches in this run",
                batch_idx + 1, total_batches, ckpt_path.name, exc,
            )
            drift_in_dir = True
        else:
            if artifact is None and ckpt_path.exists():
                logger.warning(
                    "Stage 1 batch %d/%d: checkpoint %s drifts "
                    "(header or candidate_ids changed); "
                    "redoing this and all subsequent batches in this run",
                    batch_idx + 1, total_batches, ckpt_path.name,
                )
                drift_in_dir = True

    if artifact is not None:
        logger.info(
            "Stage 1 batch %d/%d resumed from checkpoint for run %s",
            batch_idx + 1, total_batches, settings.run_id,
        )
        return artifact.blueprints, artifact.planning_summary_state, drift_in_dir

    batch_blueprints = run_planning_stage(
        spec=settings.spec,
        llm_settings=settings.llm,
        api_key=api_key,
        batch_candidate_ids=batch_candidate_ids,
        planning_summary=planning_summary_state,
    )

    if settings.enable_planning_memory:
        planning_summary_state = run_planning_summary(
            spec=settings.spec,
            candidates=batch_blueprints,
            llm_settings=settings.llm,
            api_key=api_key,
            prior_summary_state=planning_summary_state,
        )

    artifact = assemble_planning_batch_artifact(
        spec_fingerprint=spec_fingerprint,
        pragmata_version=pragmata_version,
        source_run_id=settings.run_id,
        n_queries=settings.n_queries,
        batch_size=settings.batch_size,
        batch_idx=batch_idx,
        candidate_ids=batch_candidate_ids,
        blueprints=batch_blueprints,
        planning_summary_state=planning_summary_state,
    )
    export_planning_batch_artifact(artifact=artifact, path=ckpt_path)
    logger.info(
        "Stage 1 batch %d/%d complete for run %s (%d blueprints)",
        batch_idx + 1, total_batches, settings.run_id, len(batch_blueprints),
    )
    return batch_blueprints, planning_summary_state, drift_in_dir
```

`gen_queries` then calls this helper inside a thin loop:

```python
        # spec_fingerprint named binding (was inline at resolve_querygen_paths before)
        spec_fingerprint = fingerprint_querygen_spec(settings.spec)
        pragmata_version = importlib.metadata.version("pragmata")
        paths = resolve_querygen_paths(
            workspace=workspace,
            run_id=settings.run_id,
            spec_fingerprint=spec_fingerprint,
        ).ensure_dirs()

        # ... existing planning_summary_state seeding ...

        # Stage 1: planning (with per-batch checkpointing)
        candidate_ids = build_candidate_ids(settings.n_queries)
        batch_candidate_id_lists = [
            list(islice(iter(candidate_ids), 0, n))  # see note below
            for n in iter_batch_sizes(
                n_queries=settings.n_queries,
                batch_size=settings.batch_size,
            )
        ]
        total_batches = len(batch_candidate_id_lists)

        planning_outputs: list[QueryBlueprint] = []
        drift_in_dir = False
        for batch_idx, batch_candidate_ids in enumerate(batch_candidate_id_lists):
            batch_blueprints, planning_summary_state, drift_in_dir = (
                _resume_or_run_planning_batch(
                    paths=paths,
                    settings=settings,
                    spec_fingerprint=spec_fingerprint,
                    pragmata_version=pragmata_version,
                    api_key=api_key,
                    batch_idx=batch_idx,
                    total_batches=total_batches,
                    batch_candidate_ids=batch_candidate_ids,
                    planning_summary_state=planning_summary_state,
                    drift_in_dir=drift_in_dir,
                )
            )
            planning_outputs.extend(batch_blueprints)
```

`batch_candidate_id_lists` is materialised so `total_batches` is available before iteration. The list-comprehension pattern shown is illustrative — the actual implementation should walk `iter(candidate_ids)` once (single-pass) and consume `iter_batch_sizes` lazily. (Style reviewer noted the structural change; trivial memory cost at realistic N but worth implementing correctly.)

The Stage 2 helper `_resume_or_run_realization_batch` (PR 1b) mirrors this shape against `realization_batches_dir`, calling `run_realization_stage` instead of `run_planning_stage`, and `RealizationBatchArtifact` instead of `PlanningBatchArtifact`. No `planning_summary_state` to thread through; Stage 2 batches are independent.

## Stage-1 result freeze, CSV projection, cross-run timing, and `--fresh` (folded in 2026-05-28)

These four changes implement the "maximal write-as-ready, minimal disruption" principle on top of the per-batch checkpointing above.

**1. Frozen Stage-1 result (`selected_blueprints.json`) — PR 1a.** The Stage 1 region of `gen_queries` becomes read-or-compute at the *stage* granularity, wrapping the existing per-batch resume:

```python
        # After paths.ensure_dirs(), before the Stage 1 batch loop:
        selected_blueprints: list[QueryBlueprint] | None = None
        if not settings.fresh:
            result_artifact = read_selected_blueprints_artifact(
                path=paths.selected_blueprints_json,
                expected_spec_fingerprint=spec_fingerprint,
                expected_source_run_id=settings.run_id,
                expected_n_queries=settings.n_queries,
                expected_batch_size=settings.batch_size,
                expected_near_duplicate_tolerance=settings.near_duplicate_tolerance,
            )
            if result_artifact is not None:
                selected_blueprints = result_artifact.blueprints
                logger.info(
                    "Stage 1 result loaded from frozen artifact for run %s "
                    "(%d selected); skipping planning + dedup",
                    settings.run_id, len(selected_blueprints),
                )

        if selected_blueprints is None:
            # ... existing Stage 1 per-batch loop (PR 1a) + filter + dedup ...
            selected_blueprints = deduplicate_blueprints(
                filtered_planning_outputs,
                near_duplicate_tolerance=settings.near_duplicate_tolerance,
            )
            export_selected_blueprints(
                artifact=assemble_selected_blueprints_artifact(
                    spec_fingerprint=spec_fingerprint,
                    pragmata_version=pragmata_version,
                    source_run_id=settings.run_id,
                    n_queries=settings.n_queries,
                    batch_size=settings.batch_size,
                    near_duplicate_tolerance=settings.near_duplicate_tolerance,
                    blueprints=selected_blueprints,
                ),
                path=paths.selected_blueprints_json,
            )
```

When the frozen result loads, the per-batch planning checkpoints are not even read — the result supersedes them. They remain on disk as audit/cruft; an optional sweep can delete `planning_batches/` once the result is frozen, but the plan leaves them (cheap, useful for debugging). `read_selected_blueprints_artifact` follows the exact return contract of `read_planning_batch_artifact`: `None` on missing-or-header-mismatch, raises `ValidationError`/`JSONDecodeError` on corruption (api catches → treat as absent → recompute).

**2. CSV as a deterministic projection — PR 1b.** Stage 2 chunks `selected_blueprints` (now sourced from the frozen artifact on resume) and resumes per batch. The realized outputs are reassembled in `selected_blueprints` order by the existing `filter_aligned_candidate_ids` (L273-276) — which is deterministic — so `assemble_query_rows` + `export_queries` produce a byte-identical `synthetic_queries.csv` regardless of how many Stage 2 batches were resumed vs. fresh. **No change to `assemble_query_rows`, the CSV schema, or the write path.** The CSV is therefore always reconstructable from `selected_blueprints.json` + the realization artifacts; a run killed mid-Stage-2 reassembles the complete CSV on relaunch. Optionally the CSV may be re-projected after each Stage 2 batch for a live partial file (~500 rows, trivially cheap) — but this is an additive nicety, off by default, and does not change the final artifact.

Note the row-schema constraint that forces this design: `SyntheticQueryRow` carries **no `candidate_id`**, and `query_id` is positional (`{run_id}_q{n}`). A literally-appended CSV could not serve as its own resume record (you cannot map a partial CSV back to outstanding candidate_ids) and would risk torn trailing lines. The realization artifacts carry `candidate_ids` and are atomically written, so they are the resume store; the CSV is a clean projection over them.

**3. Cross-run planning-summary at end of Stage 1 — PR 1a.** The existing end-of-run write (`querygen.py:308-317`) is duplicated to fire immediately after the Stage 1 loop, where `planning_summary_state` is already final (Stage 2 never touches it). The write is idempotent (same atomic whole-file rename to `tool_root/<fingerprint>.json`), so keeping both call sites is harmless and means a Stage 2 crash still leaves this run's accumulated planning memory available to seed future (different `run_id`) runs.

**4. `fresh` setting / `--fresh` flag.** A new boolean on `QueryGenRunSettings` (default `False`). When `True`, all read-or-resume branches above are short-circuited (`if not settings.fresh:` guards), forcing a clean from-scratch run that still *writes* all artifacts. Resume is thus the default; `--fresh` is the explicit escape hatch. The wrapper script's existing skip-on-complete (CSV ≥ N) is unaffected and complementary.

## Design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Stage 1 + Stage 2 both checkpointed | Yes, both | User answer (3). Stage 2 checkpoints alone are unreliable because Stage 1 nondeterminism shifts `selected_blueprints` ordering across reruns (reviewer CRITICAL #1). Stage 1 checkpointing pins Stage 1 output → makes Stage 2 checkpoints valid. |
| Module names | `planning_batches.py`, `realization_batches.py` | Reviewer IMPORTANT: "cache" mis-frames a persistent log of completed batches; matches new run-dir folder names. |
| Schema names | `PlanningBatchArtifact`, `RealizationBatchArtifact` | Reviewer IMPORTANT: Artifact suffix matches `PlanningSummaryArtifact`; no stage-number prefix lexical leak. |
| Schema location | `pragmata/core/schemas/querygen_output.py` | Reviewer IMPORTANT: persisted-artifact schemas live there (`PlanningSummaryArtifact`); LLM-output contracts (`RealizedQuery`, `QueryBlueprint`) live in their own files. |
| Checkpoint header fields | `spec_fingerprint`, `pragmata_version`, `source_run_id`, `n_queries`, `batch_size`, `candidate_ids` | Reviewer HIGH: catches "user bumped N with same run_id" silent-corruption, "user changed batch_size" silent invalidation, AND "user upgraded pragmata between runs (prompt templates / schema may have changed without spec edit)" — addresses second-round N-C1/N-H4. |
| Drift handling | Eager: re-run drifting batch + all subsequent in this run | User answer (5). Once drift is detected we cannot trust later checkpoints either; flag and re-run. |
| Lifecycle after success | Leave checkpoints in place | User answer (4) option 1. Useful audit; cheap to delete manually. |
| Logging location | All in `api/querygen.py` | Reviewer BLOCKING: existing pragmata pattern. `grep "logger\." /home/azureuser/pragmata/src/pragmata/core/querygen/*.py` returns empty. |
| Error-handling pattern | Helpers raise; api catches `ValidationError`/`JSONDecodeError`, logs, sets drift flag, redoes batch | Reviewer BLOCKING: matches `read_planning_summary_artifact` precedent at `planning_summary.py:61-87`. Silent swallow contradicts existing tests `test_read_planning_summary_artifact_raises_on_malformed_json`. |
| Logging detail | Basic only: start/complete/resumed lines, no elapsed time | User answer (7) "basic logging that matches existing pragmata style"; reviewer IMPORTANT: `time.monotonic` has no precedent in the repo. Elapsed time deferred to PR 4. |
| Time-stamping | In `assemble_*_batch_artifact` helpers, not at api-layer call site | Reviewer IMPORTANT: matches `assemble_planning_summary` precedent in `assembly.py`. Keeps api purely orchestration. |
| Iterator vs helpers | Two separate helpers (read + assemble); api owns the loop | Reviewer IMPORTANT: 3-tuple iterator with sentinel doesn't match existing pragmata patterns. |
| Atomic-write | Tempfile at `path.with_suffix(path.suffix + ".tmp")` in `path.parent`, then `Path.replace`. No fsync. | Style reviewer BLOCKING: no fsync precedent in pragmata; existing atomic writers (`record_builder.py:199-208`, `export_runner.py:74-109`) use same idiom without fsync. Match house style. Durability tradeoff (post-crash you can lose the most recent rename) documented in Risks. Predictable `.json.tmp` suffix enables orphan-cleanup; non-matching files (`batch_*.json` only) are ignored on read. |
| Test paths | `tests/unit/core/querygen/`, `tests/unit/api/` | Reviewer BLOCKING: pragmata's actual test tree layout. |
| Provider genericity (PR 2) | `int 600`, not `float 600.0` | User answer (8) "fully generic"; reviewer HIGH: `ChatMistralAI.timeout: int = 120` raises on float; OpenAI/Azure accept both. Use int. |
| Freeze Stage-1 result to disk | Yes — `selected_blueprints.json` after dedup | Marks "Stage 1 done"; lets Stage 2 resume skip the planning loop + dedup + embedding-model load; pins an immutable handoff so partial-Stage-1 nondeterminism cannot reach Stage 2. Closes the content-drift edge that would otherwise need a blueprint-content hash in the Stage 2 header. |
| Result-artifact header includes `near_duplicate_tolerance` | Yes | The frozen set is a function of the tolerance; changing it on a same-`run_id` rerun must invalidate and re-dedup. Other header fields mirror the batch artifacts. |
| CSV: project, don't append incrementally | Project from frozen result + realization artifacts | `SyntheticQueryRow` has no `candidate_id` and `query_id` is positional, so a partial CSV can't be its own resume record; append also risks torn lines. Projection keeps the CSV format/path **unchanged** (minimal disruption) and byte-identical across any resume mix. |
| Live per-batch CSV re-projection | Optional, off by default | ~500-row rewrite is cheap, but it's an additive nicety, not needed for resume or correctness. Default off to keep behaviour unchanged. |
| Cross-run summary write timing | End of Stage 1 **and** end of run (idempotent) | Stage 2 crash still leaves usable cross-run planning memory for future runs. Idempotent atomic write makes the duplicate call site harmless. |
| Resume by default; `--fresh` to opt out | Implicit resume | Same `run_id` rerun "just works" via artifact presence + drift checks; no flag needed for the common path. `--fresh` forces a clean run for the rare case. Complementary to the wrapper's skip-on-complete. |
| Don't stream Stage 1 into Stage 2 | Keep stages separate | Incremental dedup is feasible (dedup removes only *later* near-dups) but interleaving disrupts the staged model and memory-chain timing. Violates "minimal disruption". Out of scope. |

## PR proposal strategy

### Sequencing and dependencies

```
PR 2  ─────────────────► (lands first; independent; protects all consumers)
        │
        ▼ (informational only)
PR 1a ─────────────────► (lands second; foundation for PR 1b)
        │
        ▼ (depends on)
PR 1b ─────────────────► (lands third)
        │
        ▼
PR 3, PR 4, PR 5 (deferred follow-ups, no schedule)
```

Rationale:
- **PR 2 first.** Trivial review surface, immediate value for every pragmata consumer, doesn't block on anything.
- **PR 1a before PR 1b.** Stage 1 checkpointing stabilises Stage 1 output across reruns, which is a precondition for Stage 2 checkpoints to be valid. PR 1a also lands the frozen `selected_blueprints.json` — once that exists, Stage 2 (PR 1b) validates against an immutable set and partial-Stage-1 nondeterminism cannot reach it. Splitting the bundle lets reviewers digest the pattern once in PR 1a and confirm the symmetric extension in PR 1b. Lower risk of either PR being held up by a discussion that affects both.

### Branch names

- PR 1a: `feat/querygen-stage1-batch-persistence`
- PR 1b: `feat/querygen-stage2-batch-persistence`
- PR 2: `feat/querygen-llm-default-timeout`

All `feat/` prefix; matches recent history (`feat/cli-querygen-expose-runtime-knobs`, `refactor/annotation-compose-shipped-package-data`).

### Commit subjects (squash-merge per repo workflow)

- PR 1a: `feat(querygen): persist Stage 1 batches and frozen result, resume on rerun`
- PR 1b: `feat(querygen): persist Stage 2 batches as resume store, project CSV from artifacts`
- PR 2: `feat(querygen): set default request timeout on LLM client`

Reviewer NIT noted PR 2 is a new behavioural default, not a bug fix, so `feat(querygen)` is more accurate than `fix(querygen)`. The PR 1a/1b subjects fold the result-freeze and CSV-projection scope; if reviewers prefer narrower diffs, the frozen-result artifact can be split into its own `feat/querygen-stage1-result-freeze` PR between 1a and 1b without disturbing either's logic.

### Labels (manual, per `pragmata/CLAUDE.md`)

- All three: `querygen`, `api`.
- PR 1a + PR 1b: `contracts` (new pydantic schemas), `paths` (new run-dir artifacts).

### PR description sections (all three)

- **Why** — link to the hang incident; cite specific overnight.log lines and the `httpx.Client(timeout=None)` observation.
- **What** — the change, briefly.
- **Design notes** — relevant subset of the decisions table above.
- **Compatibility** — new run-dir subdirs are additive; existing runs unaffected on first run; callers passing `timeout` in `model_kwargs` continue to override the new default.
- **Out of scope / follow-up** — explicit list of deferred PRs (PR 3 retry classification, PR 4 metrics, PR 5 concurrency, persistent rate-limiter, in-process batch-skip recovery).
- **Test plan** — unit + integration + smoke-run notes.

### Demo-branch deployment (parallel to upstream)

Each PR's branch merges into `demo-2026-05-26` immediately after local smoke tests pass, before upstream review completes. Workspace venv's editable install resolves pragmata to `/home/azureuser/pragmata/src/pragmata` (verified), so file edits apply on the next Python invocation.

```bash
# After PR 1a's feat/ branch is local and smoke-tested:
cd /home/azureuser/pragmata
git checkout demo-2026-05-26
git merge feat/querygen-stage1-batch-persistence  # fast-forward if cleanly branched off
/home/azureuser/pragmata-workspace/.venv/bin/python -c "
from pragmata.core.querygen import planning_batches  # noqa
print('patched ok')
"
```

## Tests (PR 1a + PR 1b — symmetric structure)

Test names follow pragmata's verbose convention (mirroring `test_read_planning_summary_artifact_*` precedent at `tests/unit/core/querygen/test_planning_summary.py:670+`).

### `tests/unit/core/querygen/test_planning_batches.py` (and `test_realization_batches.py`)

- `test_read_planning_batch_artifact_roundtrip_via_export` — write via `export_planning_batch_artifact`, read back, assert equal incl. nested fields
- `test_export_planning_batch_artifact_writes_tmp_in_target_dir` — assert `path.with_suffix(".json.tmp")` is created in `path.parent` during write
- `test_export_planning_batch_artifact_leaves_no_partial_file_on_crash` — mock `Path.replace` to raise; assert no `.json` file at target (the `.tmp` may remain)
- `test_read_planning_batch_artifact_returns_none_for_missing_path`
- `test_read_planning_batch_artifact_returns_none_for_header_mismatch_spec_fingerprint`
- `test_read_planning_batch_artifact_returns_none_for_header_mismatch_pragmata_version`
- `test_read_planning_batch_artifact_returns_none_for_header_mismatch_source_run_id`
- `test_read_planning_batch_artifact_returns_none_for_header_mismatch_n_queries`
- `test_read_planning_batch_artifact_returns_none_for_header_mismatch_batch_size`
- `test_read_planning_batch_artifact_returns_none_for_candidate_ids_mismatch`
- `test_read_planning_batch_artifact_raises_validation_error_for_extra_field` — write json with unknown field, assert raises
- `test_read_planning_batch_artifact_raises_json_decode_error_for_malformed_file`
- `test_validate_blueprint_count_rejects_length_mismatch`

### `tests/unit/core/querygen/test_selected_blueprints.py` (PR 1a)

Mirrors the batch-artifact read-helper tests for the frozen Stage-1 result.

- `test_read_selected_blueprints_roundtrip_via_export` — write via `export_selected_blueprints`, read back, assert blueprints equal incl. nested fields
- `test_export_selected_blueprints_writes_tmp_in_target_dir` — assert atomic `.json.tmp` in `path.parent` during write
- `test_read_selected_blueprints_returns_none_for_missing_path`
- `test_read_selected_blueprints_returns_none_for_header_mismatch_spec_fingerprint`
- `test_read_selected_blueprints_returns_none_for_header_mismatch_pragmata_version`
- `test_read_selected_blueprints_returns_none_for_header_mismatch_n_queries`
- `test_read_selected_blueprints_returns_none_for_header_mismatch_near_duplicate_tolerance`
- `test_read_selected_blueprints_raises_validation_error_for_extra_field`
- `test_read_selected_blueprints_raises_json_decode_error_for_malformed_file`

### Resume integration tests — added to existing `tests/unit/api/test_querygen.py`

Reusing the `_install_default_workflow_stubs` fixture at lines 164-316; cross-file imports would be fragile.

- `test_gen_queries_first_run_writes_all_stage1_checkpoints` — fresh run, assert N checkpoint files in `planning_batches/` with valid content
- `test_gen_queries_first_run_writes_all_stage2_checkpoints` — same for Stage 2 (PR 1b)
- `test_gen_queries_resumes_stage1_after_simulated_hang` — patch `run_planning_stage` to raise on batch 3; first call raises; second call (same run_id) with the patched stage now succeeding completes; assert `run_planning_stage.call_count` on the *second* run equals `total_batches - 3`
- `test_gen_queries_resumes_stage2_after_simulated_hang` — same shape for Stage 2 (PR 1b)
- `test_gen_queries_resume_with_changed_n_queries_invalidates_checkpoints` — first run with N=15, second with N=30, assert all old Stage 1 checkpoints ignored, full Stage 1+2 reruns, no data corruption in CSV
- `test_gen_queries_resume_with_changed_batch_size_invalidates_checkpoints` — same shape for batch_size
- `test_gen_queries_resume_with_changed_pragmata_version_invalidates_checkpoints` — monkeypatch `importlib.metadata.version("pragmata")` to return a different value; assert checkpoints ignored
- `test_gen_queries_drift_in_mid_run_redoes_subsequent_batches` — write checkpoint 0 and 1 valid, write checkpoint 2 with mismatching candidate_ids; assert run reuses 0 and 1, redoes 2-N
- `test_gen_queries_full_stage1_resume_produces_byte_identical_csv` — fresh run produces CSV; rerun (all Stage 1 cached, all Stage 2 cached) produces byte-identical CSV. **Important caveat: this only holds for 100% resume; a *partial* Stage 1 resume can produce a different post-dedup `selected_blueprints` ordering because `deduplicate_blueprints` uses embedding-based similarity (`deduplication.py:145-156`), which is not deterministic across LLM-text variation. Document this in the test docstring; do NOT promise byte-identity for partial resumes.**
- `test_gen_queries_writes_selected_blueprints_after_stage1` — fresh run; assert `selected_blueprints.json` exists with valid `SelectedBlueprintsArtifact` whose `blueprints` equal the post-dedup set (PR 1a)
- `test_gen_queries_stage2_resume_loads_frozen_result_and_skips_stage1` — first run completes Stage 1 then dies in Stage 2 (patch `run_realization_stage` to raise on a later batch); on the second run assert `run_planning_stage` and `deduplicate_blueprints` are **not called at all** (frozen result loaded) and only outstanding Stage 2 batches re-run
- `test_gen_queries_changed_tolerance_invalidates_frozen_result` — first run at `near_duplicate_tolerance=0.95`, second at `0.80`; assert frozen result ignored, Stage 1 + dedup re-run
- `test_gen_queries_csv_byte_identical_across_partial_stage2_resume` — kill after k Stage 2 batches, resume; assert final CSV byte-identical to a from-scratch run *given the same frozen `selected_blueprints.json`* (projection is deterministic even under partial Stage 2 resume)
- `test_gen_queries_writes_cross_run_summary_at_end_of_stage1` — patch Stage 2 to raise on its first batch; assert `tool_root/<fingerprint>.json` was nonetheless written (end-of-Stage-1 call site)
- `test_gen_queries_fresh_flag_ignores_all_checkpoints` — pre-seed valid planning, result, and realization artifacts; run with `fresh=True`; assert every stage re-runs and artifacts are overwritten

### `tests/unit/core/querygen/test_llm.py` (PR 2)

- `test_default_timeout_applied_to_azure_provider` — call `build_llm_runnable` with `model_provider="azure_openai"`, no timeout in model_kwargs; intercept `init_chat_model` invocation; assert `timeout=600` in init_kwargs
- `test_default_timeout_applied_to_mistral_provider` — same with `model_provider="mistralai"`; assert no pydantic ValidationError (the int-vs-float trap)
- `test_caller_timeout_overrides_default` — pass `model_kwargs={"timeout": 120}`; assert that wins
- `test_underlying_client_timeout_received` — build the runnable for `model_provider="azure_openai"` with a dummy api_key, inspect `runnable.steps[1].root_client._client.timeout`, assert it reflects 600 (catches future langchain regressions that drop the kwarg)

## Verification (end-to-end)

Layered, smallest first.

1. **Unit tests pass locally** for each PR: `pytest tests/unit/core/querygen/test_planning_batches.py tests/unit/core/querygen/test_realization_batches.py tests/unit/api/test_querygen_resume.py tests/unit/core/querygen/test_llm.py`.
2. **Smoke run on a tiny spec.** Pick `democracy_edgecase_de` (N=30, 2 batches each stage). Run via `bash scripts/run_querygen.sh democracy_edgecase_de` after deleting the existing CSV. Confirm:
   - `planning_batches/batch_0000.json` and `_0001.json` exist with valid `PlanningBatchArtifact` content
   - `selected_blueprints.json` exists in `run_dir` with valid `SelectedBlueprintsArtifact` content (its `blueprints` equal the post-dedup set)
   - `realization_batches/batch_0000.json` and `_0001.json` exist with valid `RealizationBatchArtifact` content
   - Headers (`spec_fingerprint`, `run_id`, `n_queries`, `batch_size`) match the live run's settings
   - `overnight.log` shows the new per-batch progress lines for both stages, plus the cross-run summary written at end of Stage 1
3. **Full-resume smoke test.** Re-run the same spec immediately (frozen result present, every Stage 2 batch cached). Confirm:
   - Logs show the frozen Stage-1 result is loaded and the **planning loop + dedup are skipped entirely** (no SentenceTransformer load)
   - Logs show `resumed from checkpoint` for the Stage 2 batches
   - Zero pydantic warnings (no LLM calls)
   - Zero rate-limiter pacing
   - Generated CSV byte-identical to step 2's CSV. (Byte-identity holds whenever the frozen `selected_blueprints.json` is reused — including partial Stage 2 resume — because the CSV is a deterministic projection of it. A *partial Stage 1* resume that re-derives the result can shift `selected_blueprints` ordering after embedding-based dedup. See Risks.)
4. **Partial Stage-2 resume smoke test.** Delete the CSV and the *later* `realization_batches/*.json` (keep the frozen result + early Stage 2 batches), re-run. Confirm only the missing Stage 2 batches re-run, Stage 1 stays skipped, and the reassembled CSV is byte-identical to step 2's.
5. **Invalidation smoke test.** Edit `_runtime.yaml` to `batch_size: 25` (and separately `near_duplicate_tolerance`). Re-run. Confirm:
   - Logs show drift warning for both the batch checkpoints and the frozen result ("frozen result invalidated: batch_size/near_duplicate_tolerance changed")
   - Stage 1 + dedup re-run fresh, frozen result rewritten, CSV reproduces correctly with the new settings
6. **`--fresh` smoke test.** Re-run a fully-cached spec with `--fresh`; confirm every stage re-runs (LLM calls fire) and all artifacts are overwritten.
7. **Real target — restart with PR 1a + 1b active.** This step is forward-looking: by the time PR 1a + 1b land, today's `qgen-resume` will likely already have finished naturally (it recovered from its apparent hang). The verification target is the *next* long Stage 2 run after the PRs land. If that one hangs, the restart resumes from last checkpoint in both stages. Watch `overnight.log` for the per-batch lines as the early-warning signal that things are flowing.

   Caveat acknowledged: any in-flight pre-PR-1a/1b run cannot retroactively benefit from checkpointing — work in progress *now* is still lost if killed. PR 1a/1b protect *future* runs only.

## Risks & known limitations

- **In-process recovery is NOT added.** A batch that raises after `with_retry` exhausts still kills the stage. The wrapper script's per-spec retry-once handles the cross-process restart. Documented in the PR description.
- **Partial *Stage 1* resume is not guaranteed byte-identical to from-scratch.** `deduplicate_blueprints` uses sentence-embedding similarity (`pragmata/core/querygen/deduplication.py:145-156`), deterministic for fixed input text but not across LLM text variation. If the frozen `selected_blueprints.json` is *absent* and Stage 1 is partially re-derived (some batches cached, some fresh), the post-dedup ordering can shift. **Once `selected_blueprints.json` exists this is moot:** every subsequent run — including any partial *Stage 2* resume — projects the CSV from the frozen set and is byte-identical. So the only non-deterministic window is a crash *during* Stage 1 (before the result is frozen), which is exactly when there is no Stage 2 work to be inconsistent with. The content-drift edge flagged in the Validation section is **closed** by the frozen result; no blueprint-content hash in the Stage 2 header is needed.
- **Frozen result supersedes Stage 1 batch checkpoints.** Once `selected_blueprints.json` is valid, the per-batch `planning_batches/*.json` are not read. They are left in place (cheap audit trail); an optional start-of-run sweep could delete them but the plan does not, to aid debugging. No correctness dependency either way.
- **Stage 1's `planning_summary_state` evolves per batch.** PlanningBatchArtifact stores the state *after* the batch; resume reads it to seed the next batch. Drift in the state across reruns is implicitly caught by the header check.
- **Concurrent runs of the same `run_id`** are not prevented by pragmata; only the workspace's `run_overnight.sh` lockfile does, and only for the wrapper-script entry point. Two manually-launched `run_querygen.sh` for the same spec would race on `_batches/` writes. Out of scope; documented in PR 1a's "Compatibility" section.
- **`extra="forbid"`** is brittle in one direction: if a future PR (e.g. PR 4 adds `token_usage`) extends the artifact schema, older pragmata versions reading newer checkpoints will raise. Acceptable for v1; `pragmata_version` in the header at least makes the mismatch instantly diagnosable rather than mysterious.
- **No fsync, matching pragmata's existing atomic-write idiom.** `record_builder.py:199-208` and `export_runner.py:74-109` also use tmpfile + rename without fsync. The durability tradeoff: a kernel crash or power loss between rename and disk-flush can lose the most recent checkpoint. On the Azure VM this is exceptionally rare; on user-kill (SIGKILL/SIGTERM) the rename has either completed atomically or not happened at all — no partial state either way. Acceptable, consistent with house style.
- **Orphan `.tmp` files** from interrupted writes accumulate in the checkpoint dirs (since `Path.replace` only renames; if the process dies mid-write the `.tmp` stays). Read-side ignores them (only `batch_NNNN.json` is read). The api layer should sweep `*.tmp` files older than e.g. 1 hour from the run-dir at start; alternatively, document orphans as harmless cruft.
- **`PlanningSummaryState` schema strictness — VERIFIED OK (2026-05-28).** `pragmata/core/schemas/querygen_summary.py:9` sets `model_config = ConfigDict(extra="forbid")`, so the old-writer/new-reader field-drop asymmetry does not apply. (It is embedded in `PlanningBatchArtifact`, so this matters.)
- **Memory bound is unchanged.** Per-batch persistence improves crash recovery, not RAM ceiling. A 10000-query run still holds all rows in memory at the end of Stage 2. Out of scope.
- **CSV-as-projection assumes a stable frozen result.** The deterministic-CSV guarantee for partial Stage 2 resume relies on `selected_blueprints.json` not changing between resumes. It cannot (immutable once written; tolerance/N/batch_size changes invalidate and re-freeze it). The realization filter `filter_aligned_candidate_ids` is order-deterministic over `selected_blueprints` order, so the projection is stable.
- **`--fresh` is destructive of resume only, not of outputs.** It ignores existing artifacts and overwrites them; it does not delete unrelated run dirs. Wrapper-level skip-on-complete (CSV ≥ N) is independent and still applies unless the CSV is also removed.

## Validation: does checkpointing undermine chaining / memory / diversification / dedup? (2026-05-28)

Traced the full Stage 1/2 data flow in `pragmata/api/querygen.py:202-325` and the four modules below. Verdict: **no**, with one invariant that must be implemented as specified.

- **Planning-memory chain** (`run_planning_stage(..., planning_summary=state)` → `run_planning_summary(..., prior_summary_state=state)`, `querygen.py:223-239`). `PlanningBatchArtifact` stores the *post-batch* `planning_summary_state`; resume returns it and threads it onward, reproducing the chain exactly. **Not undermined.**
- **Cross-run memory** (`read_planning_summary_artifact`, `planning_summary.py:61-87`). Read unchanged; written only on success — now also at end of Stage 1. A crash never corrupts the seed. **Not undermined.**
- **Diversification** (`planning.py:52-92` injects `diversification_targets`/`redundancy_patterns` into the prompt). Pure function of the chained state, which is preserved. **Not undermined.**
- **Dedup** (`deduplicate_blueprints`, `deduplication.py`). Stays global, post-Stage-1, on raw per-batch blueprints; order-sensitive (earliest in `candidate_id` order survives) and deterministic for fixed text. `filter_aligned_candidate_ids` reorders to `c001..cN` *before* dedup, so order does not depend on LLM return order. Frozen to disk as `selected_blueprints.json`. **Not undermined.**

**The invariant:** Stage 2 checkpoints carry the batch's `candidate_ids` and validate them on read. Combined with the frozen `selected_blueprints.json` (Stage 2 always chunks the immutable frozen set), a Stage 2 checkpoint can never be reused against a different blueprint. Dropping the `candidate_ids` check would reintroduce a silent-corruption path.

**Crash-scenario reachability** (why the partial-resume edge is closed):
- Crash *during* Stage 1 → no frozen result, no Stage 2 checkpoints → Stage 1 re-derives, Stage 2 fully fresh. Consistent.
- Crash *during* Stage 2 → frozen result present → Stage 1 fully skipped, `selected_blueprints` immutable → Stage 2 candidate_ids *and* content match. Consistent.
- Spec / N / batch_size / tolerance / pragmata_version change → header drift invalidates all artifacts → full re-run. Consistent.

`SyntheticQueryRow` has no `candidate_id` and `query_id` is positional (`querygen_output.py:11-25`, `assembly.py:13-26`) — the reason the CSV is a projection over the candidate-id-bearing artifacts rather than its own resume record.

## Restart workflow after PR 1a + 1b land locally

```bash
# 1. Stop the broken run
tmux kill-session -t qgen-resume

# 2. Clear the stale merged config so the new _runtime.yaml is picked up
rm -f /tmp/tmp.nUFzai9wwG.yaml

# 3. Re-launch — both stages now persist per-batch
cd /home/azureuser/pragmata-workspace
tmux new -d -s qgen-resume 'bash scripts/run_querygen.sh sustainable_communities_de,sustainable_communities_edgecase_de >> overnight.log 2>&1'
```
