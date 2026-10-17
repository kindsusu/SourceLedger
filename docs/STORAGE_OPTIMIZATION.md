# Checkpoint storage optimization

Checkpoint format implemented and measured on 2026-10-13. SQLite remains the system of record; XLSX remains the report format. The original evidence files and existing research runs were unchanged by that measurement.

## Behavior

New checkpoint writes store binary captures once per SHA-256 under `checkpoint_blobs/` beside `prices.sqlite3`. Text captures use lossless gzip when smaller; already compressed images are stored without recompression. SQLite stores references instead of repeatedly embedding base64 captures. Files are flushed and atomically installed before the database row is committed. Failed writes leave the previous row intact; unused files from an interrupted write may remain. See the conservative maintenance policy below.

Reading checks file size and SHA-256; missing or damaged files stop recovery. Existing base64 checkpoints remain readable. Scope, time/page budgets, coverage, prices, and resume state are preserved. This does not implement a parallel scheduler.

**Back up or move the entire output directory**, including `checkpoint_blobs/`, `evidence/`, and the database. SQLite alone no longer contains all resume captures. Older program versions cannot read the new blob references. Existing databases do not automatically shrink: free SQLite pages can remain after rewriting a legacy row. Do not compact or delete files while collection is running.

## Measurement

The saved Jetcar/Gongcar subscription benchmark (525 URLs, 27 checkpoints) was replayed offline into disposable copies. Original files were opened read-only and left untouched. Every restored checkpoint matched the original decoded state exactly. Copied databases were vacuumed and closed before size measurement; no transient WAL/SHM files were counted.

| Metric | Before | After |
| --- | ---: | ---: |
| Databases, including new checkpoint blob files | 882,274,304 bytes | 556,936,123 bytes |
| Whole-run equivalent, retaining original evidence/reports/logs | 1,522,551,160 bytes | 1,197,212,979 bytes |
| Checkpoint writes: median of 3 local replays | 17.72 seconds | 4.39 seconds |

Whole-run storage decreases by 325,338,181 bytes (21.37%). Original evidence remains 628,630,060 bytes. The whole-run after figure substitutes measured compacted copies for the original databases; it is not a newly crawled run or a migration of the originals.

Timing uses the same saved 20-page batch, growing from 1 to 20 captures with two writes per step (40 writes). Three runs per format used fresh local directories, with legacy followed by compact each time. Median checkpoint-write time decreases by 75.22%. This is **not** total collection, network, model, or report-generation time, nor a claim of equivalent end-to-end speedup. Memory usage still includes decoded captures.

Validation: 16 checkpoint/resume/follow-up integration tests passed, plus all 27 real checkpoint round trips. Local benchmark details remain outside Git in `.tmp/checkpoint-storage-20261013-115124/result.json`.

## Retention and maintenance

The collected results remain in XLSX and SQLite. Source HTML, screenshots, receipts and other evidence referenced by observations remain on disk so a report can be audited or rebuilt. New identical capture bytes, including PNG screenshots, share a SHA-256-named file under `evidence/`; each collection still has its own receipt. This avoids storing another copy of the same bytes but does not deduplicate older archives automatically. Collection of genuinely new or changed pages still grows the workspace.

The maintenance command inspects **one existing output directory** at a time. Start with a preview:

```powershell
python -m su_crawler storage --output-dir "E:\path\to\one-output-directory"
```

When the preview identifies unreferenced checkpoint internals, apply the same bounded cleanup:

```powershell
python -m su_crawler storage --output-dir "E:\path\to\one-output-directory" --apply
```

Only checkpoint pages and blob files unreachable from every saved checkpoint may be removed. Incomplete `.pending-` checkpoint blob files are eligible only after the default 7-day grace period; `--grace-days 1` through `365` changes that period. The command does **not** delete checkpoint roots, SQLite observations, XLSX reports, source evidence, screenshots, receipts, job records or research workspaces. It does not impose a maximum disk quota, remove evidence by age, or migrate paths after a folder move. A failed integrity check or a concurrent checkpoint writer prevents unsafe cleanup. Back up the whole output directory before a manual cleanup or move.

After a finalized checkpoint is saved, the same conservative cleanup runs at most once per 24 hours by default. An optional `storage-policy.json` in that output directory controls it:

```json
{"automatic_cleanup": true, "grace_days": 7, "maintenance_interval_hours": 24}
```

Set `automatic_cleanup` to `false` to disable scheduled maintenance. The latest maintenance status is written under `.checkpoint-maintenance/status.json`; a failed maintenance attempt does not fail collection. The default policy does not remove evidence or old reports. For an archive already on another volume, assess duplicates and references separately before any one-time archival consolidation. The 2026-10-13 size and speed figures above are historical checkpoint-format measurements, not a measured saving from this retention policy.
