# Checkpoint storage optimization

Implemented and measured on 2026-10-13. SQLite remains the system of record; XLSX remains the report format. The original evidence files and existing research runs are unchanged.

## Behavior

New checkpoint writes store binary captures once per SHA-256 under `checkpoint_blobs/` beside `prices.sqlite3`. Text captures use lossless gzip when smaller; already compressed images are stored without recompression. SQLite stores references instead of repeatedly embedding base64 captures. Files are flushed and atomically installed before the database row is committed. Failed writes leave the previous row intact; unused files from an interrupted write may remain. No automatic garbage collection is performed.

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
