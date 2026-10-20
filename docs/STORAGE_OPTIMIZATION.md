# Checkpoint storage optimization

Checkpoint format implemented and checked with retained captures. SQLite remains the system of record; XLSX remains the report format. The original evidence files and existing research runs were unchanged by that measurement.

## Behavior

New checkpoint writes store binary captures once per SHA-256 under `checkpoint_blobs/` beside `prices.sqlite3`. Text captures use lossless gzip when smaller; already compressed images are stored without recompression. SQLite stores references instead of repeatedly embedding base64 captures. Files are flushed and atomically installed before the database row is committed. Failed writes leave the previous row intact; unused files from an interrupted write may remain. See the conservative maintenance policy below.

Reading checks file size and SHA-256; missing or damaged files stop recovery. Existing base64 checkpoints remain readable. Scope, time/page budgets, coverage, prices, and resume state are preserved. This does not implement a parallel scheduler.

**Back up or move the entire output directory**, including `checkpoint_blobs/`, `evidence/`, and the database. SQLite alone no longer contains all resume captures. Older program versions cannot read the new blob references. Existing databases do not automatically shrink: free SQLite pages can remain after rewriting a legacy row. Do not compact or delete files while collection is running.

## Measurement

Storage changes should be compared by replaying the same retained captures into disposable directories. Verify decoded checkpoint equality and source-file hashes before comparing database, blob, evidence, report, and elapsed-write sizes separately. Compression savings do not imply shorter total collection time. Site-specific measurements remain in ignored local benchmark records.

## Retention and maintenance

The collected results remain in XLSX and SQLite. Source HTML, screenshots, receipts and other evidence referenced by observations remain on disk so a report can be audited or rebuilt. Browser screenshots are full-page JPEG (quality 70, stored as `.jpg`; older captures stay `.png`), which is several times smaller than PNG and faster to encode on tall pages. Screenshots are supplementary: the DOM snapshot remains the price record. New identical capture bytes, including screenshots, share a SHA-256-named file under `evidence/`; each collection still has its own receipt. This avoids storing another copy of the same bytes but does not deduplicate older archives automatically. When an identical uncompressed checkpoint blob (`checkpoint_blobs/<xx>/<sha256>.bin`, such as a PNG screenshot) already exists, the evidence file is created as a hard link to it after its SHA-256 is verified; filesystems without hard links get an ordinary copy. Captures that the checkpoint stores as gzip still keep a raw evidence copy, because evidence is verified by the SHA-256 of the file itself. Retention removing a blob name never removes the evidence name. Collection of genuinely new or changed pages still grows the workspace.

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

Set `automatic_cleanup` to `false` to disable scheduled maintenance. The latest maintenance status is written under `.checkpoint-maintenance/status.json`; a failed maintenance attempt does not fail collection. The default policy does not remove evidence or old reports. For an archive already on another volume, assess duplicates and references separately before any one-time archival consolidation. The retention policy itself does not establish a measured saving for any particular collection.
