"""Everything the dashboard shows, gathered off the UI thread.

A Gatherer turns the SQLite store, the worker lock, the log, and the files on
disk into an immutable Snapshot. Cheap facts (worker, quota, job counts, the
log tail) refresh every tick; the heavier pace/ETA/throughput queries every few
seconds or whenever the caller pokes (a task just finished); per-job disk usage
is sized incrementally. The Collector runs a Gatherer on its own thread with
its own SQLite connection and publishes each Snapshot by swapping a reference,
so the render loop never waits on I/O.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..config import Config
from ..quota import QuotaLedger
from ..state import Store
from .control import is_stopped, worker_pid
from .events import Event, LogTail
from .fmt import job_name
from .metrics import Finish, Pace, build_pace, finishes, rates, work_left

log = logging.getLogger(__name__)

HISTORY_S = 24 * 3600  # throughput history kept for the graph


@dataclass
class JobView:
    job_id: str
    kind: str
    status: str  # jobs.status: active | done | cancelled
    created_at: str
    name: str
    spec: dict[str, Any]
    counts: dict[str, int]
    rows: int
    size: int = 0  # bytes of parquet this job wrote
    left_s: float | None = None  # busy seconds of work left
    pace_s: float | None = None  # mean seconds per task so far

    def n(self, status: str) -> int:
        return self.counts.get(status, 0)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def remaining(self) -> int:
        return self.n("pending") + self.n("in_progress") + self.n("failed")

    @property
    def settled(self) -> int:
        return self.total - self.remaining

    @property
    def frac(self) -> float:
        return self.settled / self.total if self.total else 0.0

    @property
    def state(self) -> str:
        if self.status == "cancelled":
            return "cancelled"
        if self.n("in_progress"):
            return "running"
        if self.remaining:
            return "queued"
        if self.n("dead"):
            return "attention"
        return "complete"


@dataclass(frozen=True)
class Problem:
    task_id: str
    job_id: str
    status: str  # dead | failed
    reporter: str
    period: str
    flows: tuple[str, ...]
    attempts: int
    error: str


@dataclass(frozen=True)
class Flight:
    task_id: str
    job_id: str
    reporter: str
    period: str
    dataset: str
    freq: str
    flows: tuple[str, ...]
    claimed: float | None
    attempts: int
    depth: int
    expect_s: float | None
    basis: str


@dataclass(frozen=True)
class Upcoming:
    task_id: str
    job_id: str
    reporter: str
    period: str
    flows: tuple[str, ...]
    attempts: int
    expect_s: float | None


@dataclass
class Detail:
    """Per-job drill-down: task states per reporter x period."""

    job_id: str
    reporters: list[str]
    periods: list[str]
    cells: dict[tuple[str, str], dict[str, int]]
    rows_by_reporter: dict[str, int]
    tasks_by_reporter: dict[str, tuple[int, int]]  # (settled ok, total)
    problems: list[Problem]


@dataclass
class Snapshot:
    version: int
    taken: float
    tier: str
    budget: int | None
    quota_used: int = 0
    quota_reset_s: float = 0.0
    quota_history: list[tuple[str, int]] = field(default_factory=list)
    worker_pid: int | None = None
    worker_stalled: bool = False
    jobs: list[JobView] = field(default_factory=list)
    flights: list[Flight] = field(default_factory=list)
    upcoming: list[Upcoming] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    releases: dict[str, int] = field(default_factory=dict)
    release_periods: list[tuple[str, int, int]] = field(default_factory=list)
    recent: list[Finish] = field(default_factory=list)
    tasks_h: float = 0.0
    rows_h: float = 0.0
    events: list[Event] = field(default_factory=list)
    events_version: int = 0
    data_bytes: int = 0
    data_files: int = 0
    detail: Detail | None = None
    error: str | None = None

    @property
    def worker_alive(self) -> bool:
        return bool(self.worker_pid)

    @property
    def active_jobs(self) -> list[JobView]:
        return [j for j in self.jobs if j.remaining]

    @property
    def remaining(self) -> int:
        return sum(j.remaining for j in self.jobs)

    def job(self, job_id: str | None) -> JobView | None:
        return next((j for j in self.jobs if j.job_id == job_id), None)


def _flows(params_json: str | None) -> tuple[str, ...]:
    try:
        return tuple(json.loads(params_json or "{}").get("flows") or ())
    except ValueError:
        return ()


def _problem(r) -> Problem:
    return Problem(
        task_id=r["task_id"],
        job_id=r["job_id"],
        status=r["status"],
        reporter=str(r["reporter"]),
        period=str(r["period"]),
        flows=_flows(r["params_json"]),
        attempts=int(r["attempts"] or 0),
        error=" ".join((r["last_error"] or "").split()),
    )


class _Sizer:
    """Per-job bytes on disk, from the output paths each task recorded. New
    paths are stat'ed as tasks finish; everything is re-checked occasionally
    (a retried task rewrites its files)."""

    def __init__(self, root: Path):
        self.root = root
        self._sizes: dict[str, int] = {}  # path -> bytes
        self._job_of: dict[str, str] = {}
        self._since: str | None = None
        self._full_at = 0.0

    def refresh(self, store: Store, now: float) -> None:
        full = now - self._full_at > 600
        since = None if full else self._since
        stamp = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        if full:
            self._sizes.clear()
            self._job_of.clear()
            self._full_at = now
        for r in store.outputs_since(since):
            try:
                paths = json.loads(r["output_paths"])
            except ValueError:
                continue
            for p in paths:
                try:
                    self._sizes[p] = (self.root / p).stat().st_size
                    self._job_of[p] = r["job_id"]
                except OSError:
                    self._sizes.pop(p, None)
        # a little overlap: finished_at has one-second resolution
        self._since = (dt.datetime.fromisoformat(stamp) - dt.timedelta(seconds=5)).isoformat()

    def by_job(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p, size in self._sizes.items():
            job = self._job_of.get(p, "")
            out[job] = out.get(job, 0) + size
        return out

    @property
    def files(self) -> int:
        return len(self._sizes)


class Gatherer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tail = LogTail(cfg.paths.logs / "tradescrapr.log")
        self.sizer = _Sizer(cfg.paths.root)
        self.version = 0
        self._slow_at = 0.0
        self._size_at = 0.0
        self._pace = Pace()
        self._left: dict[str, float | None] = {}
        self._recent: list[Finish] = []
        self._quota_history: list[tuple[str, int]] = []
        self._release_periods: list[tuple[str, int, int]] = []

    def gather(
        self,
        store: Store,
        *,
        force: bool = False,
        focus: str | None = None,
        queue: tuple[str | None, str | None] = (None, None),
    ) -> Snapshot:
        """`queue` is the running worker's (job, kind) filter, so "up next"
        lists what that worker will actually claim."""
        now = time.time()
        self.version += 1
        cfg = self.cfg
        quota = QuotaLedger(store, cfg.quota)
        pid = worker_pid(cfg)
        snap = Snapshot(
            version=self.version, taken=now, tier=cfg.tier, budget=cfg.quota.daily_budget
        )
        snap.worker_pid = pid
        snap.worker_stalled = is_stopped(pid)
        snap.quota_used = quota.used_today()
        snap.quota_reset_s = quota.seconds_until_reset()

        progress = {jp.job_id: jp for jp in store.job_progress()}
        jobs: list[JobView] = []
        for r in store.jobs():
            jp = progress.get(r["job_id"])
            try:
                spec = json.loads(r["spec_json"] or "{}")
            except ValueError:
                spec = {}
            jobs.append(
                JobView(
                    job_id=r["job_id"],
                    kind=r["kind"],
                    status=r["status"],
                    created_at=r["created_at"],
                    name=job_name(r["job_id"], spec.get("name")),
                    spec=spec,
                    counts=dict(jp.counts) if jp else {},
                    rows=jp.records if jp else 0,
                )
            )

        if force or now - self._slow_at > 5:
            self._slow(store, jobs, now)
        if force or now - self._size_at > 30:
            self.sizer.refresh(store, now)
            self._size_at = now
        sizes = self.sizer.by_job()
        for j in jobs:
            j.left_s = self._left.get(j.job_id) if j.remaining else 0.0
            j.pace_s = self._pace.by_job.get(j.job_id)
            j.size = sizes.get(j.job_id, 0)
        snap.jobs = jobs
        snap.data_bytes = sum(sizes.values())
        snap.data_files = self.sizer.files

        flights = []
        for r in store.in_progress_tasks(limit=8):
            exp, basis = self._pace.expect(r["job_id"], str(r["reporter"]))
            claimed = r["claimed_at"]
            flights.append(
                Flight(
                    task_id=r["task_id"],
                    job_id=r["job_id"],
                    reporter=str(r["reporter"]),
                    period=str(r["period"]),
                    dataset=r["dataset"],
                    freq=r["freq"],
                    flows=_flows(r["params_json"]),
                    claimed=dt.datetime.fromisoformat(claimed).timestamp() if claimed else None,
                    attempts=int(r["attempts"] or 0),
                    depth=int(r["depth"] or 0),
                    expect_s=exp,
                    basis=basis,
                )
            )
        snap.flights = flights
        snap.upcoming = [
            Upcoming(
                task_id=r["task_id"],
                job_id=r["job_id"],
                reporter=str(r["reporter"]),
                period=str(r["period"]),
                flows=_flows(r["params_json"]),
                attempts=int(r["attempts"] or 0),
                expect_s=self._pace.expect(r["job_id"], str(r["reporter"]))[0],
            )
            for r in store.next_tasks(limit=16, job_id=queue[0], kind=queue[1])
        ]
        snap.problems = [_problem(r) for r in store.problem_tasks(limit=60)]
        snap.releases = store.release_summary()
        snap.release_periods = self._release_periods
        snap.quota_history = self._quota_history
        snap.recent = self._recent
        snap.tasks_h, snap.rows_h = rates(self._recent, now)

        self.tail.poll()
        snap.events = list(self.tail.events)
        snap.events_version = self.tail.version

        if focus:
            snap.detail = self._detail(store, focus)
        return snap

    def _slow(self, store: Store, jobs: list[JobView], now: float) -> None:
        since = dt.datetime.fromtimestamp(now - HISTORY_S, dt.UTC).isoformat(timespec="seconds")
        self._recent = finishes(store.finished_since(since))
        active = [j.job_id for j in jobs if j.remaining]
        self._pace = build_pace(store.pace(active), self._recent)
        self._left = work_left(self._pace, store.backlog())
        self._quota_history = [(r["quota_date"], r["calls_used"]) for r in store.quota_history()]
        self._release_periods = [
            (str(r["period"]), int(r["done"] or 0), int(r["pending"] or 0))
            for r in store.release_periods(limit=8)
        ]
        self._slow_at = now

    def _detail(self, store: Store, job_id: str) -> Detail:
        cells: dict[tuple[str, str], dict[str, int]] = {}
        rows: dict[str, int] = {}
        tasks: dict[str, tuple[int, int]] = {}
        for r in store.coverage(job_id):
            key = (str(r["reporter"]), str(r["period"]))
            cells.setdefault(key, {})[r["status"]] = r["n"]
            rows[key[0]] = rows.get(key[0], 0) + int(r["recs"] or 0)
            ok, tot = tasks.get(key[0], (0, 0))
            ok += r["n"] if r["status"] in ("done", "split") else 0
            tasks[key[0]] = (ok, tot + r["n"])
        return Detail(
            job_id=job_id,
            reporters=sorted({k[0] for k in cells}, key=lambda c: (not c.isdigit(), len(c), c)),
            periods=sorted({k[1] for k in cells}),
            cells=cells,
            rows_by_reporter=rows,
            tasks_by_reporter=tasks,
            problems=[_problem(r) for r in store.problem_tasks(job_id=job_id, limit=200)],
        )


class Collector:
    """Runs a Gatherer every `interval` seconds on a background thread."""

    def __init__(self, cfg: Config, *, interval: float = 1.0):
        self.cfg = cfg
        self.interval = interval
        self.gatherer = Gatherer(cfg)
        self.focus: str | None = None  # job whose detail to gather
        self.queue: tuple[str | None, str | None] = (None, None)  # worker's job/kind filter
        self._latest: Snapshot | None = None
        self._wake = threading.Event()
        self._halt = threading.Event()
        self._force = False
        self._thread: threading.Thread | None = None

    def prime(self) -> Snapshot:
        """One synchronous pass on the calling thread (first frame, --once)."""
        store = Store(self.cfg.paths.state_db)
        try:
            self._latest = self.gatherer.gather(
                store, force=True, focus=self.focus, queue=self.queue
            )
        finally:
            store.close()
        return self._latest

    @property
    def latest(self) -> Snapshot | None:
        return self._latest

    def poke(self, *, full: bool = True) -> None:
        self._force = self._force or full
        self._wake.set()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="dashboard-collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        store = Store(self.cfg.paths.state_db)
        try:
            while not self._halt.is_set():
                force, self._force = self._force, False
                try:
                    self._latest = self.gatherer.gather(
                        store, force=force, focus=self.focus, queue=self.queue
                    )
                except Exception as e:  # keep the last good snapshot on screen
                    log.debug("dashboard gather failed: %r", e)
                    if self._latest is not None:
                        self._latest = replace(self._latest, error=f"{type(e).__name__}: {e}")
                self._wake.wait(self.interval)
                self._wake.clear()
        finally:
            store.close()
