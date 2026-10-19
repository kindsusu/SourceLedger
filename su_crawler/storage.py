from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import json
import os
import sqlite3

from .models import utc_now


class RunBusyError(RuntimeError):
    pass


@contextmanager
def workspace_lock(directory: Path):
    """OS lock released on process death; stale lock files don't block a restart."""
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / "collection.lock").open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RunBusyError("Collection is already running in this output directory") from exc
        yield
    finally:
        handle.close()


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / "prices.sqlite3", timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, config_hash TEXT NOT NULL, status TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, demo INTEGER NOT NULL,
            report_path TEXT, reason TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
            source_id TEXT NOT NULL, product_id TEXT NOT NULL, status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
            backend TEXT, updated_at TEXT NOT NULL,
            UNIQUE(run_id, source_id, product_id)
        );
        CREATE TABLE IF NOT EXISTS observations (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
            task_id TEXT NOT NULL REFERENCES tasks(id), data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
            source_id TEXT NOT NULL, backend TEXT NOT NULL,
            status TEXT NOT NULL, at TEXT NOT NULL, data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_snapshots (
            cache_key TEXT PRIMARY KEY, data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS catalog_checkpoints (
            id TEXT PRIMARY KEY, data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS browser_submissions (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS run_coverage (
            run_id TEXT PRIMARY KEY REFERENCES runs(id), data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS collection_issues (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
            page_url TEXT NOT NULL, section_key TEXT NOT NULL DEFAULT '',
            option_key TEXT NOT NULL DEFAULT '', reason_code TEXT NOT NULL,
            reason TEXT NOT NULL, status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, next_action TEXT NOT NULL,
            evidence TEXT NOT NULL, scope TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            resolution_evidence TEXT
        );
        CREATE INDEX IF NOT EXISTS collection_issues_run_status
            ON collection_issues(run_id, status, page_url);
        CREATE TABLE IF NOT EXISTS collection_issue_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            issue_id TEXT NOT NULL REFERENCES collection_issues(id),
            previous_status TEXT NOT NULL, new_status TEXT NOT NULL,
            evidence TEXT, at TEXT NOT NULL
        );
        """)

    def close(self):
        self.db.close()

    def source_snapshot(self, cache_key: str) -> dict | None:
        row = self.db.execute("SELECT data FROM source_snapshots WHERE cache_key=?", (cache_key,)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
            return value if isinstance(value, dict) else None
        except (ValueError, TypeError):
            return None

    def save_source_snapshot(self, cache_key: str, data: dict) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO source_snapshots VALUES(?,?)",
                            (cache_key, json.dumps(data, ensure_ascii=False)))

    def run(self, run_id: str) -> dict:
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Run ID does not exist")
        return dict(row)

    def run_coverage(self, run_id: str) -> list[dict]:
        """Read the exact page/section coverage persisted for a run."""
        row = self.db.execute("SELECT data FROM run_coverage WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return []
        value = json.loads(row[0])
        if not isinstance(value, list):
            raise ValueError("Run coverage is not a list")
        return value

    def save_run_coverage(self, run_id: str, coverage: list[dict]) -> None:
        if not isinstance(coverage, list):
            raise ValueError("Run coverage must be a list")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO run_coverage(run_id,data) VALUES(?,?)",
                            (run_id, json.dumps(coverage, ensure_ascii=False)))

    def save_collection_issues(self, issues: list[dict]) -> None:
        """Insert deterministic issue snapshots; never rewrite earlier run evidence."""
        now = utc_now()
        with self.db:
            for issue in issues:
                self.db.execute("""INSERT OR IGNORE INTO collection_issues
                    (id,run_id,page_url,section_key,option_key,reason_code,reason,status,
                     attempts,next_action,evidence,scope,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (issue['id'], issue['run_id'], issue['page_url'], issue.get('section_key', ''),
                     issue.get('option_key', ''), issue['reason_code'], issue['reason'], 'open',
                     issue.get('attempts', 0), issue['next_action'],
                     json.dumps(issue.get('evidence', {}), ensure_ascii=False),
                     json.dumps(issue.get('scope', {}), ensure_ascii=False), now, now))

    def collection_issues(self, run_id: str, *, status: str | None = None,
                          offset: int = 0, limit: int = 200) -> list[dict]:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError('Invalid issue pagination')
        query = 'SELECT * FROM collection_issues WHERE run_id=?'
        params: list = [run_id]
        if status is not None:
            if status not in {'open', 'needs_review', 'resolved'}:
                raise ValueError('Invalid issue status')
            query += ' AND status=?'
            params.append(status)
        query += ' ORDER BY page_url,section_key,option_key,reason_code,id LIMIT ? OFFSET ?'
        return [self._decode_issue(row) for row in self.db.execute(query, (*params, limit, offset))]

    def collection_issue_count(self, run_ids: list[str], *, status: str | None = None) -> int:
        if not run_ids:
            return 0
        if len(run_ids) > 500:
            raise ValueError('Too many batch run IDs')
        if status is not None and status not in {'open', 'needs_review', 'resolved'}:
            raise ValueError('Invalid issue status')
        query = f"SELECT count(*) FROM collection_issues WHERE run_id IN ({','.join('?' for _ in run_ids)})"
        params: list = list(run_ids)
        if status is not None:
            query += ' AND status=?'
            params.append(status)
        return self.db.execute(query, params).fetchone()[0]

    def collection_issue_events(self, issue_id: str) -> list[dict]:
        rows = [dict(row) for row in self.db.execute(
            'SELECT * FROM collection_issue_events WHERE issue_id=? ORDER BY id', (issue_id,))]
        for row in rows:
            row['evidence'] = json.loads(row['evidence']) if row['evidence'] else None
        return rows

    def collection_issues_for_runs(self, run_ids: list[str], *, status: str | None = None,
                                   offset: int = 0, limit: int = 50) -> list[dict]:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError('Invalid issue pagination')
        if not run_ids:
            return []
        if len(run_ids) > 500:
            raise ValueError('Too many batch run IDs')
        if status is not None and status not in {'open', 'needs_review', 'resolved'}:
            raise ValueError('Invalid issue status')
        query = f"SELECT * FROM collection_issues WHERE run_id IN ({','.join('?' for _ in run_ids)})"
        params: list = list(run_ids)
        if status is not None:
            query += ' AND status=?'
            params.append(status)
        query += ' ORDER BY created_at,run_id,page_url,section_key,option_key,id LIMIT ? OFFSET ?'
        return [self._decode_issue(row) for row in self.db.execute(query, (*params, limit, offset))]

    def update_collection_issue(self, issue_id: str, *, status: str,
                                evidence: dict | None = None) -> dict:
        if status not in {'open', 'needs_review', 'resolved'}:
            raise ValueError('Invalid issue status')
        row = self.db.execute('SELECT * FROM collection_issues WHERE id=?', (issue_id,)).fetchone()
        if row is None:
            raise ValueError('Collection issue does not exist')
        if status == 'resolved':
            if not isinstance(evidence, dict) or not evidence.get('kind') or not evidence.get('reference'):
                raise ValueError('Resolution requires explicit evidence kind and reference')
            if evidence['kind'] != 'revalidated_run':
                raise ValueError('Coverage issues can only resolve after a revalidated run')
            run_id = evidence['reference']
            if run_id == row['run_id']:
                raise ValueError('Revalidation must use a later run')
            later = self.db.execute('SELECT started_at FROM runs WHERE id=?', (run_id,)).fetchone()
            original = self.db.execute('SELECT started_at FROM runs WHERE id=?', (row['run_id'],)).fetchone()
            if later is None or original is None or later[0] <= original[0]:
                raise ValueError('Revalidation run must exist and be later')
            old_scope = json.loads(row['scope'])
            if not old_scope.get('source_id') or not (old_scope.get('adapter') or old_scope.get('recipe_fingerprint')):
                raise ValueError('Original issue has no comparable source with adapter or recipe scope')
            matches = []
            for page in self.run_coverage(run_id):
                if page.get('url') != row['page_url'] or page.get('source_id') != old_scope['source_id'] or page.get('adapter') != old_scope.get('adapter'):
                    continue
                changed = {key: {'from': old_scope.get(key), 'to': page.get(key)}
                           for key in ('recipe_fingerprint', 'plan_fingerprint')
                           if old_scope.get(key) != page.get(key)}
                if changed:
                    revision = evidence.get('scope_revision')
                    if not isinstance(revision, dict) or not isinstance(revision.get('reason'), str) or not revision['reason'].strip() or revision.get('changed') != changed:
                        raise ValueError('Changed recipe or plan requires an explicit scope_revision reason and matching fingerprints')
                matches.append(page)
            if not matches:
                raise ValueError('Revalidation run must cover the same URL, source and adapter scope')
            if row['section_key']:
                complete = any(any(section.get('section') == row['section_key'] and
                                   section.get('status') == 'complete' and
                                   (not row['option_key'] or row['option_key'] in section.get('extracted_locators', []))
                                   for section in page.get('sections') or []) for page in matches)
            else:
                complete = any(page.get('coverage_status') == 'complete' for page in matches)
            if not complete:
                raise ValueError('Revalidation run has no complete coverage for this issue scope')
        elif status == 'needs_review' and evidence is not None:
            if not isinstance(evidence, dict) or evidence.get('kind') != 'scope_review' or not evidence.get('reference') or not evidence.get('decision'):
                raise ValueError('Scope review requires a reference and decision')
        with self.db:
            self.db.execute('UPDATE collection_issues SET status=?,resolution_evidence=?,updated_at=? WHERE id=?',
                            (status, json.dumps(evidence, ensure_ascii=False) if evidence else None,
                             utc_now(), issue_id))
            self.db.execute('''INSERT INTO collection_issue_events
                (issue_id,previous_status,new_status,evidence,at) VALUES(?,?,?,?,?)''',
                (issue_id, row['status'], status,
                 json.dumps(evidence, ensure_ascii=False) if evidence else None, utc_now()))
        return self._collection_issue(issue_id)

    def _collection_issue(self, issue_id: str) -> dict:
        return self._decode_issue(self.db.execute('SELECT * FROM collection_issues WHERE id=?', (issue_id,)).fetchone())

    @staticmethod
    def _decode_issue(raw) -> dict:
        row = dict(raw)
        for key in ('evidence', 'scope', 'resolution_evidence'):
            row[key] = json.loads(row[key]) if row[key] else None
        row['url'] = row['page_url']
        row['section'] = row['section_key']
        row['category'] = row['reason_code']
        row['attempt_count'] = row['attempts']
        row['evidence_ref'] = (row['evidence'] or {}).get('evidence_ref')
        return row

    def tasks(self, run_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY source_id,product_id", (run_id,))]

    def source_tasks(self, run_id: str, source_id: str) -> list[dict]:
        """Read the current source's tasks without materializing the whole run."""
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM tasks WHERE run_id=? AND source_id=? ORDER BY product_id", (run_id, source_id))]

    def observations(self, run_id: str) -> list[dict]:
        return [json.loads(r[1]) for r in self.db.execute(
            "SELECT id,data FROM observations WHERE run_id=? UNION ALL SELECT id,data FROM browser_submissions WHERE run_id=? ORDER BY id",
            (run_id, run_id))]

    def update_task(self, task_id: str, status: str, reason: str = "", backend: str | None = None, attempt: bool = False):
        self.db.execute("UPDATE tasks SET status=?, reason=?, backend=?, updated_at=?, attempts=attempts+? WHERE id=?",
                        (status, reason, backend, utc_now(), int(attempt), task_id))
        self.db.commit()

    def finish_task(self, task_id: str, status: str, reason: str, backend: str, observations: list[dict]):
        with self.db:
            self.db.execute("DELETE FROM observations WHERE task_id=?", (task_id,))
            for row in observations:
                self.db.execute("INSERT INTO observations(id,run_id,task_id,data) VALUES(?,?,?,?)",
                                (row["id"], row["run_id"], task_id, json.dumps(row, ensure_ascii=False)))
            self.db.execute("UPDATE tasks SET status=?,reason=?,backend=?,updated_at=? WHERE id=?",
                            (status, reason, backend, utc_now(), task_id))
