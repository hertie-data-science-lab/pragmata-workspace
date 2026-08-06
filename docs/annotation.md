# Annotation pipeline

Generate synthetic queries, run them through the publikationsbot, and load the results into Argilla for annotation.

```mermaid
flowchart TD
  specs["querygen_specs + _runtime.yaml"] -->|run_querygen.sh| sq[synthetic_queries.csv]
  sq -->|run_bot.py| jsonl[publikationsbot JSONL]
  jsonl -->|build_combined.py| comb["per-domain combined.jsonl"]
  comb -->|"setup.sh + import.sh"| arg[("Argilla: 3 tasks × prod/calibration")]
```

Everything under `data/` is gitignored - see [Data & secrets](configuration.md#data--secrets).

## Orchestrator

`scripts/pipeline.sh` runs any contiguous slice of the stages over an optional domain filter, owning the cross-cutting concerns the stage scripts don't: stage-aware pre-flight, lockfile, bot parallelism, tee logging, continue-on-error.

| Invocation                             | Covers                          |
| -------------------------------------- | ------------------------------- |
| `pipeline.sh`                          | full pipeline                   |
| `pipeline.sh --to bot-run`             | querygen-run + bot-run          |
| `pipeline.sh --from combine-run`       | combine-run + the two annotation stages |
| `pipeline.sh --only annotation-setup`  | provision workspaces/users      |
| `pipeline.sh --only annotation-import` | import every domain             |

`--filter` takes domains (`querygen-run`/`bot-run` expand each to `<domain>` + `<domain>_edgecase`); `--dry-run` prints the plan without running (`make plan`).

`setup.sh` and `import.sh` are thin wrappers over pragmata's native `annotation setup` / `annotation import`. The only workspace-specific bits are the password merge in `setup.sh` (see [Annotator roster](configuration.md#annotator-roster)) and `import.sh`'s inline `jq` projection. For anything non-standard, call the pragmata CLI directly.

## Running it

`make help` lists every target - the single stages, the orchestrated `pipeline`, and the ops below. Each stage is a thin wrapper; read `scripts/annotation/` to see the exact native command it runs.

```bash
make pipeline                                 # full pipeline, all domains
make pipeline TO=bot-run FILTER=gesundheit    # querygen-run + bot-run for one domain
tmux new -s pipeline 'make pipeline'          # unattended, survives disconnect
```

## Logging & reporting

- **Logging** is automatic and daily. The nightly job - `scripts/daily.sh` (`make annotation-daily`) - chains `export.sh` (submitted annotations → per-domain CSVs) then `log.py --use-export` (live counts + IAA + cadence → append one snapshot to `logs/annotation/log.jsonl`). `export.sh` and `make annotation-freeze` share a lock (`.export.lock`), so a freeze can never copy a tree the cron is halfway through rewriting; whichever runs second exits 3.
- **Reporting** is manual (`make annotation-report`): render the latest snapshot into `reports/annotation/<date>/` - `report_tables.py` writes `report.md` (pure data tables), `plot_summary.py` writes the PNGs, and `_latest` is repointed to the newest.

Each snapshot carries three metrics (production vs calibration where it applies):

1. **Counts** - submitted responses (the work count), completed records (met `min_submitted`), and total records.
2. **Calibration agreement** - Krippendorff α over the calibration overlap. The headline is **pooled**: item-level data from every domain goes into one reliability matrix per (task, label), and those α are averaged unweighted across labels to give a figure per task. α = 1 − Do/De is a ratio, so per-domain α are never averaged into a headline - they stay as a diagnostic in the report's collapsed breakdown. Each pooled α is published with the marginals behind it (ratings, minority-class count, prevalence, De), because a label whose minority class is small has De ≈ 0 and an unstable α; where the minority count is 0 the label never varies, De = 0, and α is undefined (reported as 1.000 by pragmata's convention, and flagged ⚠ in the table).
3. **Cadence** - median seconds between consecutive submissions, per-annotator (individual pace) and global (team throughput). A session guard drops gaps over `LOG_SESSION_GAP_MIN` (default 30 min) as pauses.

Timestamps come from the REST endpoint - the SDK and export CSVs drop per-response submission times. `log.py` emits a one-line status, not tables; pass `--summary` for an ad-hoc table. Nightly cron:

```cron
0 2 * * * /home/azureuser/pragmata-workspace/scripts/daily.sh > /dev/null 2>&1
```

## Backup & restore

`scripts/annotation/argilla_backup.py` (`make annotation-backup`) takes a status-preserving snapshot of every Argilla dataset - records, metadata, suggestions, and responses with their `submitted`/`draft`/`discarded` status (the SDK's own `to_disk` drops response status). Read-only; writes a timestamped tree under `argilla_backup/<UTC-ts>/` plus a `manifest.json`.

```bash
make annotation-backup                                     # dump all datasets
make annotation-restore DIR=argilla_backup/<ts>            # preview restore (dry-run)
make annotation-restore DIR=argilla_backup/<ts> APPLY=1    # write it
```

`annotation-restore` reinstates the full snapshot - creating any dataset that no longer exists, and writing onto ones that still exist. It always previews first (record counts, plus any response/metadata that would change) and only writes with `APPLY=1`. To narrow the scope with `--workspace` / `--dataset` / `--record-id` (repeatable, AND'd), or restrict attributes with `--only {metadata,suggestions,responses}`, call `scripts/annotation/argilla_backup.py restore` directly. Take a fresh backup before restoring onto a live dataset - restoring reverts to that point in time, including any activity recorded after the snapshot.
