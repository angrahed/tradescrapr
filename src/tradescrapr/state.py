"""SQLite-backed job/task/quota/release state.

Single-writer by design (the worker holds an exclusive flock), so contention is
minimal; WAL + busy_timeout keep concurrent `status` reads safe. Data files are
the leaves, the task row is the commit point: a task only flips to 'done' after
its parquet and manifest are on disk.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .planner import TaskParams

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  job_id      TEXT PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('targeted','monitor')),
  spec_json   TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','done','cancelled'))
);

CREATE TABLE IF NOT EXISTS tasks (
  task_id         TEXT PRIMARY KEY,
  job_id          TEXT NOT NULL REFERENCES jobs(job_id),
  parent_task_id  TEXT REFERENCES tasks(task_id),
  kind            TEXT NOT NULL DEFAULT 'get' CHECK (kind IN ('get','bulk')),
  params_json     TEXT NOT NULL,
  params_hash     TEXT NOT NULL,
  dataset  TEXT NOT NULL,
  freq     TEXT NOT NULL,
  period   TEXT NOT NULL,
  reporter TEXT NOT NULL,
  depth           INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','in_progress','done','failed',
                                    'dead','split','cancelled')),
  attempts        INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT,
  records_fetched INTEGER,
  output_paths    TEXT,
  created_at  TEXT NOT NULL,
  claimed_at  TEXT,
  finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_tasks_dedup      ON tasks(job_id, params_hash);
CREATE INDEX IF NOT EXISTS ix_tasks_claim             ON tasks(status, attempts, created_at);
CREATE INDEX IF NOT EXISTS ix_tasks_job_status        ON tasks(job_id, status);
CREATE INDEX IF NOT EXISTS ix_tasks_partition         ON tasks(dataset, freq, period, reporter);

CREATE TABLE IF NOT EXISTS quota_ledger (
  quota_date TEXT PRIMARY KEY,
  calls_used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS releases (
  dataset  TEXT NOT NULL,
  freq     TEXT NOT NULL,
  reporter TEXT NOT NULL,
  period   TEXT NOT NULL,
  last_released TEXT NOT NULL,
  job_id   TEXT,
  status   TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done')),
  updated_at TEXT NOT NULL,
  PRIMARY KEY (dataset, freq, reporter, period)
);

CREATE TABLE IF NOT EXISTS partition_cleanups (
  job_id   TEXT NOT NULL,
  dataset  TEXT NOT NULL,
  freq     TEXT NOT NULL,
  period   TEXT NOT NULL,
  reporter TEXT NOT NULL,
  cleaned_at TEXT NOT NULL,
  PRIMARY KEY (job_id, dataset, freq, period, reporter)
);
"""

_CLAIM_SQL = """
UPDATE tasks
SET status='in_progress', claimed_at=:now, attempts=attempts+1
WHERE task_id = (
    SELECT t.task_id FROM tasks t
    WHERE t.status IN ('pending','failed')
      AND (:job_id IS NULL OR t.job_id = :job_id)
      AND (:kind IS NULL OR t.job_id IN (SELECT job_id FROM jobs WHERE kind = :kind))
    ORDER BY t.attempts ASC, t.created_at ASC, t.task_id ASC
    LIMIT 1
)
RETURNING *
"""


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def task_id_for(job_id: str, params_hash: str) -> str:
    return hashlib.sha256(f"{job_id}:{params_hash}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Task:
    task_id: str
    job_id: str
    parent_task_id: str | None
    kind: str
    params: dict[str, Any]
    params_hash: str
    dataset: str
    freq: str
    period: str
    reporter: str
    depth: int
    status: str
    attempts: int
    last_error: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Task:
        return cls(
            task_id=row["task_id"],
            job_id=row["job_id"],
            parent_task_id=row["parent_task_id"],
            kind=row["kind"],
            params=json.loads(row["params_json"]),
            params_hash=row["params_hash"],
            dataset=row["dataset"],
            freq=row["freq"],
            period=row["period"],
            reporter=row["reporter"],
            depth=row["depth"],
            status=row["status"],
            attempts=row["attempts"],
            last_error=row["last_error"],
        )


@dataclass
class JobProgress:
    job_id: str
    kind: str
    created_at: str
    counts: dict[str, int] = field(default_factory=dict)
    records: int = 0

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def remaining(self) -> int:
        return sum(self.counts.get(s, 0) for s in ("pending", "in_progress", "failed"))

    @property
    def pct_done(self) -> float:
        settled = sum(self.counts.get(s, 0) for s in ("done", "split", "dead"))
        return 100.0 * settled / self.total if self.total else 0.0


@dataclass(frozen=True)
class Release:
    dataset: str
    freq: str
    reporter: str
    period: str
    last_released: str
    status: str
    job_id: str | None


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(_SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # -- jobs ---------------------------------------------------------------

    def create_job(self, job_id: str, kind: str, spec_json: str) -> bool:
        with self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO jobs(job_id, kind, spec_json, created_at) VALUES(?,?,?,?)",
                (job_id, kind, spec_json, _now()),
            )
        return cur.rowcount > 0

    def get_job(self, job_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()

    # -- tasks ----------------------------------------------------------------

    def _insert_task_rows(
        self,
        job_id: str,
        params_list: Sequence[TaskParams],
        parent_task_id: str | None,
        depth: int,
    ) -> int:
        n = 0
        now = _now()
        for p in params_list:
            ph = p.params_hash()
            cur = self.db.execute(
                """INSERT OR IGNORE INTO tasks
                   (task_id, job_id, parent_task_id, kind, params_json, params_hash,
                    dataset, freq, period, reporter, depth, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id_for(job_id, ph),
                    job_id,
                    parent_task_id,
                    p.kind,
                    p.to_json(),
                    ph,
                    p.dataset,
                    p.freq,
                    p.period,
                    p.reporter,
                    depth,
                    now,
                ),
            )
            n += cur.rowcount
        return n

    def add_tasks(
        self,
        job_id: str,
        params_list: Sequence[TaskParams],
        parent_task_id: str | None = None,
        depth: int = 0,
    ) -> int:
        with self.db:
            return self._insert_task_rows(job_id, params_list, parent_task_id, depth)

    def claim_next_task(self, job_id: str | None = None, kind: str | None = None) -> Task | None:
        with self.db:
            row = self.db.execute(
                _CLAIM_SQL, {"now": _now(), "job_id": job_id, "kind": kind}
            ).fetchone()
        return Task.from_row(row) if row else None

    def finish_task(self, task_id: str, *, records: int, paths: list[str]) -> None:
        with self.db:
            self.db.execute(
                """UPDATE tasks SET status='done', records_fetched=?, output_paths=?,
                   finished_at=? WHERE task_id=?""",
                (records, json.dumps(paths), _now(), task_id),
            )

    def fail_task(self, task_id: str, error: str, *, permanent: bool, max_attempts: int) -> str:
        with self.db:
            row = self.db.execute(
                "SELECT attempts FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            attempts = row["attempts"] if row else 0
            status = "dead" if permanent or attempts >= max_attempts else "failed"
            self.db.execute(
                "UPDATE tasks SET status=?, last_error=?, finished_at=? WHERE task_id=?",
                (status, error[:2000], _now() if status == "dead" else None, task_id),
            )
        return status

    def split_task(self, task: Task, children: Sequence[TaskParams]) -> int:
        """Atomically insert split children and retire the parent."""
        with self.db:
            n = self._insert_task_rows(
                task.job_id, children, parent_task_id=task.task_id, depth=task.depth + 1
            )
            self.db.execute(
                "UPDATE tasks SET status='split', finished_at=? WHERE task_id=?",
                (_now(), task.task_id),
            )
        return n

    def release_task(self, task_id: str) -> None:
        """Return an in_progress task to pending without consuming an attempt
        (quota exhaustion, shutdown)."""
        with self.db:
            self.db.execute(
                """UPDATE tasks SET status='pending', claimed_at=NULL,
                   attempts=MAX(attempts-1, 0)
                   WHERE task_id=? AND status='in_progress'""",
                (task_id,),
            )

    def reset_stale_in_progress(self, older_than_min: int | None = None) -> int:
        """Reclaim in_progress tasks (all of them, or only stale ones). Safe because
        task execution is idempotent."""
        with self.db:
            if older_than_min is None:
                cur = self.db.execute(
                    "UPDATE tasks SET status='pending', claimed_at=NULL WHERE status='in_progress'"
                )
            else:
                cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=older_than_min)).isoformat(
                    timespec="seconds"
                )
                cur = self.db.execute(
                    "UPDATE tasks SET status='pending', claimed_at=NULL "
                    "WHERE status='in_progress' AND claimed_at < ?",
                    (cutoff,),
                )
        if cur.rowcount:
            log.info("reclaimed %d in_progress task(s)", cur.rowcount)
        return cur.rowcount

    def retry_dead(self, job_id: str | None = None, task_id: str | None = None) -> int:
        with self.db:
            cur = self.db.execute(
                """UPDATE tasks SET status='pending', attempts=0, claimed_at=NULL,
                   finished_at=NULL
                   WHERE status='dead'
                     AND (:job_id IS NULL OR job_id=:job_id)
                     AND (:task_id IS NULL OR task_id=:task_id)""",
                {"job_id": job_id, "task_id": task_id},
            )
        return cur.rowcount

    def dead_tasks(self, job_id: str | None = None) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT task_id, job_id, dataset, freq, period, reporter, attempts, last_error
               FROM tasks WHERE status='dead' AND (:job_id IS NULL OR job_id=:job_id)
               ORDER BY job_id, period, reporter""",
            {"job_id": job_id},
        ).fetchall()

    def job_progress(self, job_id: str | None = None) -> list[JobProgress]:
        rows = self.db.execute(
            """SELECT j.job_id, j.kind, j.created_at, t.status AS tstatus,
                      COUNT(t.task_id) AS n, COALESCE(SUM(t.records_fetched), 0) AS recs
               FROM jobs j LEFT JOIN tasks t ON t.job_id = j.job_id
               WHERE (:job_id IS NULL OR j.job_id = :job_id)
               GROUP BY j.job_id, t.status ORDER BY j.created_at""",
            {"job_id": job_id},
        ).fetchall()
        by_job: dict[str, JobProgress] = {}
        for r in rows:
            jp = by_job.setdefault(
                r["job_id"], JobProgress(r["job_id"], r["kind"], r["created_at"])
            )
            if r["tstatus"] is not None:
                jp.counts[r["tstatus"]] = r["n"]
                jp.records += r["recs"]
        return list(by_job.values())

    # -- dashboard queries (read-only) -----------------------------------------

    def quota_history(self, days: int = 7) -> list[sqlite3.Row]:
        rows = self.db.execute(
            "SELECT quota_date, calls_used FROM quota_ledger ORDER BY quota_date DESC LIMIT ?",
            (days,),
        ).fetchall()
        return rows[::-1]  # oldest first

    def release_summary(self) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT status, COUNT(*) AS n FROM releases GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def release_periods(self, limit: int = 6) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT period, SUM(status='done') AS done, SUM(status='pending') AS pending
               FROM releases GROUP BY period ORDER BY period DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    def in_progress_tasks(self, limit: int = 5) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT task_id, job_id, dataset, freq, period, reporter, claimed_at
               FROM tasks WHERE status='in_progress'
               ORDER BY claimed_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    # -- quota ledger -----------------------------------------------------------

    def quota_used(self, quota_date: str) -> int:
        row = self.db.execute(
            "SELECT calls_used FROM quota_ledger WHERE quota_date=?", (quota_date,)
        ).fetchone()
        return row["calls_used"] if row else 0

    def quota_try_reserve(self, quota_date: str, budget: int | None) -> bool:
        if budget is not None and budget <= 0:
            return False
        with self.db:
            if budget is None:
                self.db.execute(
                    """INSERT INTO quota_ledger(quota_date, calls_used) VALUES(?, 1)
                       ON CONFLICT(quota_date) DO UPDATE SET calls_used = calls_used + 1""",
                    (quota_date,),
                )
                return True
            cur = self.db.execute(
                """INSERT INTO quota_ledger(quota_date, calls_used) VALUES(?, 1)
                   ON CONFLICT(quota_date) DO UPDATE SET calls_used = calls_used + 1
                   WHERE calls_used < ?""",
                (quota_date, budget),
            )
            return cur.rowcount > 0

    def quota_force_exhaust(self, quota_date: str, budget: int | None) -> None:
        if budget is None:
            return
        with self.db:
            self.db.execute(
                """INSERT INTO quota_ledger(quota_date, calls_used) VALUES(?, ?)
                   ON CONFLICT(quota_date) DO UPDATE SET calls_used = MAX(calls_used, ?)""",
                (quota_date, budget, budget),
            )

    # -- releases (monitor memory) --------------------------------------------------

    def get_release(self, dataset: str, freq: str, reporter: str, period: str) -> Release | None:
        row = self.db.execute(
            "SELECT * FROM releases WHERE dataset=? AND freq=? AND reporter=? AND period=?",
            (dataset, freq, reporter, period),
        ).fetchone()
        if not row:
            return None
        return Release(
            dataset=row["dataset"],
            freq=row["freq"],
            reporter=row["reporter"],
            period=row["period"],
            last_released=row["last_released"],
            status=row["status"],
            job_id=row["job_id"],
        )

    def all_releases(self, dataset: str, freq: str) -> dict[tuple[str, str], Release]:
        rows = self.db.execute(
            "SELECT * FROM releases WHERE dataset=? AND freq=?", (dataset, freq)
        ).fetchall()
        return {
            (r["reporter"], r["period"]): Release(
                dataset=r["dataset"],
                freq=r["freq"],
                reporter=r["reporter"],
                period=r["period"],
                last_released=r["last_released"],
                status=r["status"],
                job_id=r["job_id"],
            )
            for r in rows
        }

    def upsert_release(
        self,
        dataset: str,
        freq: str,
        reporter: str,
        period: str,
        last_released: str,
        job_id: str,
    ) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO releases(dataset, freq, reporter, period, last_released,
                                        job_id, status, updated_at)
                   VALUES(?,?,?,?,?,?, 'pending', ?)
                   ON CONFLICT(dataset, freq, reporter, period) DO UPDATE SET
                     last_released=excluded.last_released, job_id=excluded.job_id,
                     status='pending', updated_at=excluded.updated_at""",
                (dataset, freq, reporter, period, last_released, job_id, _now()),
            )

    def finalize_releases(self) -> int:
        """Flip pending releases to done once every task for that reporter-period in
        the recorded job has settled as done/split — never on failed/dead/pending."""
        with self.db:
            cur = self.db.execute(
                """UPDATE releases SET status='done', updated_at=:now
                   WHERE status='pending' AND job_id IS NOT NULL
                     AND EXISTS (
                       SELECT 1 FROM tasks t
                       WHERE t.job_id=releases.job_id AND t.dataset=releases.dataset
                         AND t.freq=releases.freq AND t.reporter=releases.reporter
                         AND t.period=releases.period AND t.status='done')
                     AND NOT EXISTS (
                       SELECT 1 FROM tasks t
                       WHERE t.job_id=releases.job_id AND t.dataset=releases.dataset
                         AND t.freq=releases.freq AND t.reporter=releases.reporter
                         AND t.period=releases.period
                         AND t.status NOT IN ('done','split'))""",
                {"now": _now()},
            )
        return cur.rowcount

    def supersede_monitor_tasks(
        self, dataset: str, freq: str, reporter: str, period: str, new_job_id: str
    ) -> int:
        """Cancel unsettled tasks for a reporter-period left over from earlier
        monitor jobs — a newer release supersedes them (the new job's tasks will
        wipe and rewrite the partition)."""
        with self.db:
            cur = self.db.execute(
                """UPDATE tasks SET status='cancelled',
                       last_error='superseded by newer release'
                   WHERE dataset=? AND freq=? AND reporter=? AND period=?
                     AND status IN ('pending','failed','in_progress')
                     AND job_id != ?
                     AND job_id IN (SELECT job_id FROM jobs WHERE kind='monitor')""",
                (dataset, freq, reporter, period, new_job_id),
            )
        return cur.rowcount

    # -- partition cleanups ------------------------------------------------------------

    def try_record_partition_cleanup(
        self, job_id: str, dataset: str, freq: str, period: str, reporter: str
    ) -> bool:
        """True exactly once per (job, partition): the caller must then wipe the
        partition directory before writing."""
        with self.db:
            cur = self.db.execute(
                """INSERT OR IGNORE INTO partition_cleanups
                   (job_id, dataset, freq, period, reporter, cleaned_at)
                   VALUES (?,?,?,?,?,?)""",
                (job_id, dataset, freq, period, reporter, _now()),
            )
        return cur.rowcount > 0
