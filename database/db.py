"""SQLite persistence for jobs, batches and an append-only record of Meshy task IDs.

API keys are never stored here.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from models.job import NON_DUPLICATE_BLOCKING, Job, JobStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    origin TEXT NOT NULL,
    budget INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source_path TEXT NOT NULL,
    image_hash TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL,
    batch_id INTEGER,
    settings_json TEXT,
    task_id TEXT UNIQUE,
    progress INTEGER NOT NULL DEFAULT 0,
    est_credits INTEGER,
    consumed_credits INTEGER,
    message TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    submit_started_at REAL,
    submitted_at REAL,
    finished_at REAL,
    next_action_at REAL NOT NULL DEFAULT 0,
    expires_at INTEGER,
    output_dir TEXT,
    unity_dir TEXT,
    hidden INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_hash ON jobs(image_hash);
-- Append-only: every task ID Meshy ever returned to us, even if a job row is edited.
CREATE TABLE IF NOT EXISTS task_ids (
    task_id TEXT PRIMARY KEY,
    job_id INTEGER NOT NULL,
    recorded_at REAL NOT NULL
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------- helpers
    def _write(self, sql: str, params=()) -> sqlite3.Cursor:
        """Execute a statement; the whole call runs under the connection lock."""
        with self._lock:
            return self._conn.execute(sql, params)

    def _one(self, sql: str, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params=()) -> list:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    @staticmethod
    def _job(row) -> Job | None:
        return Job(**dict(row)) if row else None

    # ---------------------------------------------------------------- jobs
    def add_job(self, *, name: str, source_path: str, image_hash: str, origin: str = "manual",
                status: str = JobStatus.QUEUED, est_credits: int | None = None,
                batch_id: int | None = None, settings_json: str | None = None, message: str = "") -> int:
        now = time.time()
        cur = self._write(
            "INSERT INTO jobs(name, source_path, image_hash, origin, status, est_credits, batch_id,"
            " settings_json, message, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (name, source_path, image_hash, origin, status, est_credits, batch_id, settings_json, message, now, now))
        return int(cur.lastrowid)

    def get_job(self, job_id: int) -> Job | None:
        return self._job(self._one("SELECT * FROM jobs WHERE id=?", (job_id,)))

    def update_job(self, job_id: int, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._write(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    def transition(self, job_id: int, from_statuses, to_status: str, **fields) -> bool:
        """Atomically change status only if the job is currently in one of from_statuses."""
        if isinstance(from_statuses, str):
            from_statuses = (from_statuses,)
        fields["status"] = to_status
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        marks = ",".join("?" * len(from_statuses))
        cur = self._write(f"UPDATE jobs SET {cols} WHERE id=? AND status IN ({marks})",
                         (*fields.values(), job_id, *from_statuses))
        return cur.rowcount == 1

    def jobs_by_status(self, statuses, due_before: float | None = None) -> list[Job]:
        if isinstance(statuses, str):
            statuses = (statuses,)
        marks = ",".join("?" * len(statuses))
        sql = f"SELECT * FROM jobs WHERE status IN ({marks})"
        params: list = list(statuses)
        if due_before is not None:
            sql += " AND next_action_at <= ?"
            params.append(due_before)
        sql += " ORDER BY id"
        return [self._job(r) for r in self._all(sql, params)]

    def list_jobs(self, include_hidden: bool = False, limit: int = 1000) -> list[Job]:
        sql = "SELECT * FROM jobs" + ("" if include_hidden else " WHERE hidden=0") + " ORDER BY id DESC LIMIT ?"
        return [self._job(r) for r in self._all(sql, (limit,))]

    def count_status(self, statuses) -> int:
        if isinstance(statuses, str):
            statuses = (statuses,)
        marks = ",".join("?" * len(statuses))
        return int(self._one(f"SELECT COUNT(*) FROM jobs WHERE status IN ({marks})", statuses)[0])

    def find_duplicate(self, image_hash: str, exclude_job: int | None = None) -> Job | None:
        """A visible job for the same image that is not failed/canceled."""
        marks = ",".join("?" * len(NON_DUPLICATE_BLOCKING))
        row = self._one(
            f"SELECT * FROM jobs WHERE image_hash=? AND hidden=0 AND status NOT IN ({marks}) AND id != ? "
            "ORDER BY id LIMIT 1",
            (image_hash, *NON_DUPLICATE_BLOCKING, exclude_job or -1))
        return self._job(row)

    def stats(self) -> dict:
        rows = self._all("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def recover_after_restart(self) -> int:
        """A create request that was in flight when the app stopped has an unknown outcome."""
        cur = self._write(
            "UPDATE jobs SET status=?, message=?, next_action_at=0, updated_at=? WHERE status=?",
            (JobStatus.UNCERTAIN, "App closed while submitting - checking whether Meshy created the task.",
             time.time(), JobStatus.SUBMITTING))
        return cur.rowcount

    # ------------------------------------------------------------- task ids
    def record_task_id(self, task_id: str, job_id: int) -> None:
        self._write("INSERT OR IGNORE INTO task_ids(task_id, job_id, recorded_at) VALUES (?,?,?)",
                   (task_id, job_id, time.time()))

    def known_task_ids(self) -> set[str]:
        ids = {r[0] for r in self._all("SELECT task_id FROM task_ids")}
        ids |= {r[0] for r in self._all("SELECT task_id FROM jobs WHERE task_id IS NOT NULL")}
        return ids

    # -------------------------------------------------------------- batches
    def create_batch(self, origin: str, budget: int) -> int:
        cur = self._write("INSERT INTO batches(created_at, origin, budget) VALUES (?,?,?)",
                         (time.time(), origin, int(budget)))
        return int(cur.lastrowid)

    def get_batch(self, batch_id: int) -> dict | None:
        row = self._one("SELECT * FROM batches WHERE id=?", (batch_id,))
        return dict(row) if row else None

    def batch_committed_credits(self, batch_id: int, exclude_job: int | None = None) -> int:
        """Credits already committed by a batch: actual consumed credits when known,
        otherwise the estimate for tasks that were (or may have been) created."""
        row = self._one(
            """SELECT COALESCE(SUM(
                   CASE WHEN consumed_credits IS NOT NULL THEN consumed_credits
                        WHEN status = ? THEN 0
                        ELSE COALESCE(est_credits, 0) END), 0)
               FROM jobs WHERE batch_id=? AND id != ?
                 AND (task_id IS NOT NULL OR status IN (?, ?))""",
            (JobStatus.FAILED, batch_id, exclude_job or -1, JobStatus.SUBMITTING, JobStatus.UNCERTAIN))
        return int(row[0])
