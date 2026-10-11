"""The worker: claims tasks and executes them until the queue drains.

Robustness model:
- Singleton: an exclusive flock on state/worker.lock; a second worker refuses to start.
- Resume-by-default: on start, all in_progress tasks are reset to pending (safe —
  execution is idempotent, outputs are keyed by task_id and rewritten).
- SIGTERM/SIGINT set a shutdown event checked between tasks and inside every sleep;
  the in-flight request is bounded by the HTTP timeout. A second signal hard-exits,
  returning the in-flight task to the queue.
- Quota exhaustion releases the current task without burning an attempt, then either
  sleeps until the reset (interruptibly) or exits 0 for cron to resume later.
- Transient errors back off exponentially with jitter and re-queue; permanent errors
  and unsplittable truncations dead-letter the task.
- Truncated responses split into children along flow -> partner groups (sized from
  the free availability endpoint's record counts) -> commodity batches, in the same
  transaction that retires the parent.
- Every phase (request, receive, parse, write, backoff, quota sleep) is published
  to state/worker.json through a StatusBoard, for dashboards and the inline display.
"""

from __future__ import annotations

import fcntl
import logging
import os
import random
import signal
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from . import storage
from .client import ComtradeClient, FetchResult
from .config import Config
from .errors import (
    CannotSplit,
    PermanentError,
    QuotaExhausted,
    ShutdownRequested,
    TradescraprError,
    TransientError,
)
from .livestatus import STATUS_FILE, StatusBoard
from .planner import TaskParams, split
from .quota import QuotaLedger
from .refs import Refs
from .state import Store, Task

log = logging.getLogger(__name__)

# Rough inflation of full-breakdown record counts (getDA totalRecords, all flows,
# every partner2/customs/mot cell) over the classic subset one flow-call returns.
# Only steers split sizing; recursion corrects any misestimate.
FULL_TO_CLASSIC_RATIO = 3.0


@contextmanager
def worker_lock(path: Path):
    """Exclusive singleton lock. Held for the whole run; the OS releases it on
    any kind of process death, so no stale-lock handling is needed. The file
    holds the owner's pid (for `stop` and the dashboard), so it is only
    truncated once the lock is ours — a refused second worker must not erase
    the running one's pid."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o644), "r+")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fd.close()
        raise TradescraprError(
            f"another tradescrapr worker or monitor is already running ({path} is locked)"
        ) from None
    fd.truncate(0)
    fd.write(f"{os.getpid()}\n")
    fd.flush()
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


class Worker:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        client: ComtradeClient,
        quota: QuotaLedger,
        refs: Refs,
        board: StatusBoard | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.client = client
        self.quota = quota
        self.refs = refs
        self.board = board or StatusBoard(None)
        self.shutdown = threading.Event()
        self.calls_made = 0
        self.tasks_done = 0
        self.tasks_split = 0
        self.tasks_failed = 0
        # (dataset, type, freq, cl, reporter, period) -> totalRecords from getDA
        self._avail_cache: dict[tuple[str, ...], int | None] = {}
        self._job_kind_cache: dict[str, str] = {}

    # -- signals & sleeps ---------------------------------------------------

    def install_signal_handlers(self) -> None:
        def handler(signum: int, _frame) -> None:
            name = signal.Signals(signum).name
            if self.shutdown.is_set():
                log.warning("second %s — hard exit", name)
                raise SystemExit(130)
            log.warning("%s received — finishing current task, then stopping", name)
            self.shutdown.set()
            self.board.stopping()

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    def _sleep(self, seconds: float) -> None:
        """Sleep, waking the moment shutdown is requested."""
        self.shutdown.wait(max(seconds, 0.0))

    def _backoff(self, attempts: int, retry_after: float | None, reason: str = "") -> None:
        r = self.cfg.retry
        delay = min(r.backoff_cap_s, r.backoff_base_s * 2 ** max(attempts - 1, 0))
        delay *= random.uniform(0.5, 1.5)
        if retry_after:
            delay = max(delay, retry_after)
        log.info("backing off %.0fs", delay)
        self.board.waiting("backoff", delay, reason)
        self._sleep(delay)

    def _sleep_until_quota_reset(self) -> None:
        secs = self.quota.seconds_until_reset() + 60  # headroom past the boundary
        used = self.quota.used_today()
        log.info("quota exhausted (used=%d); sleeping %.0f min until reset", used, secs / 60)
        self.board.waiting("quota", secs, f"{used} calls used today")
        self._sleep(secs)

    def replaces_whole_partition(self, job_id: str) -> bool:
        if job_id not in self._job_kind_cache:
            row = self.store.get_job(job_id)
            self._job_kind_cache[job_id] = str(row["kind"]) if row is not None else ""
        return self._job_kind_cache[job_id] == "monitor"

    # -- task execution -------------------------------------------------------

    def _execute(self, task: Task) -> None:
        params = TaskParams.from_dict(task.params)
        if task.kind == "bulk":
            from .bulk import execute_bulk_task

            execute_bulk_task(self, task, params)
            return

        part_dir = storage.partition_dir(
            self.cfg.paths.data, params.dataset, params.freq, params.period, params.reporter
        )
        # Monitor revision jobs fully replace a published partition exactly once.
        # Targeted jobs coexist, so separate product baskets do not wipe each other.
        if self.replaces_whole_partition(task.job_id) and self.store.try_record_partition_cleanup(
            task.job_id, params.dataset, params.freq, params.period, params.reporter
        ):
            storage.clean_partition(part_dir)
        storage.remove_task_outputs(part_dir, task.task_id)

        if not self.quota.try_reserve():
            raise QuotaExhausted(f"daily budget spent ({self.quota.used_today()} calls)")
        self.calls_made += 1
        self.board.call()
        self.board.phase("request")
        try:
            result = self.client.get_data(
                dataset=params.dataset,
                type_code=params.type_code,
                freq=params.freq,
                cl_code=params.cl_code,
                query=params.api_query(),
                on_progress=self.board.transfer,
                on_retry=self.board.note,
            )
        except QuotaExhausted:
            self.quota.force_exhaust()
            raise

        if result.truncated:
            self._split(task, params, result)
            return

        paths: list[str] = []
        rows = 0
        self.board.phase("write", rows=len(result.records))
        if result.records:
            wr = storage.write_chunk(
                result.records,
                part_dir=part_dir,
                task_id=task.task_id,
                manifest_meta={
                    "dataset": params.dataset,
                    "source_url": result.url,
                    "params": params.to_dict(),
                    "job_id": task.job_id,
                    "api_truncated": False,
                },
            )
            paths = [str(wr.path.relative_to(self.cfg.paths.root))]
            rows = wr.rows
        self.store.finish_task(task.task_id, records=rows, paths=paths)
        self.tasks_done += 1
        self.board.task_end("done", rows=rows, wire=result.wire_bytes)
        log.info(
            "task=%s job=%s %s status=done records=%d dur=%.1fs ttfb=%.1fs bytes=%d",
            task.task_id,
            task.job_id,
            params.describe(),
            rows,
            result.elapsed_s,
            result.ttfb_s,
            result.wire_bytes,
        )

    def _estimate_rows(self, params: TaskParams) -> float | None:
        """Expected rows for this task's query, derived from the free availability
        endpoint's totalRecords (throttled, never quota-debited). None if unknown.
        totalRecords counts the full breakdown over all flows; scale down for the
        classic subset and the task's flow share. The recursive split is the
        backstop when the estimate is off."""
        key = (
            params.dataset,
            params.type_code,
            params.freq,
            params.cl_code,
            params.reporter,
            params.period,
        )
        if key not in self._avail_cache:
            total: int | None = None
            try:
                rows = self.client.get_availability(
                    dataset=params.dataset,
                    type_code=params.type_code,
                    freq=params.freq,
                    cl_code=params.cl_code,
                    period=params.period,
                    reporter_code=params.reporter,
                )
                totals = [r.get("totalRecords") for r in rows if r.get("totalRecords")]
                total = max(totals) if totals else None
            except TradescraprError as e:
                log.debug("availability estimate failed for %s: %s", key, e)
            self._avail_cache[key] = total
        total = self._avail_cache[key]
        if not total:
            return None
        classic = params.partner2 is not None
        est = total / (FULL_TO_CLASSIC_RATIO if classic else 1.0)
        return est * min(len(params.flows) / 2, 1.0)

    def _split(self, task: Task, params: TaskParams, result: FetchResult) -> None:
        if task.depth >= self.cfg.retry.max_split_depth:
            raise PermanentError(
                f"still truncated at max split depth {task.depth}: {params.describe()}"
            )
        # sizing only helps the first, all-partner split; deeper levels bisect
        self.board.phase("split", rows=len(result.records))
        est = self._estimate_rows(params) if params.partners is None else None
        children = split(
            params,
            self.refs,
            max_cmd_codes=self.cfg.planner.max_cmd_codes_per_call,
            est_rows=est,
            cap=result.cap,
        )
        n = self.store.split_task(task, children)
        self.tasks_split += 1
        self.board.task_end("split", wire=result.wire_bytes, note=f"{n} parts")
        log.info(
            "task=%s truncated at cap=%d -> split into %d children (depth %d%s) %s",
            task.task_id,
            result.cap,
            n,
            task.depth + 1,
            f", est≈{est / 1000:.0f}k rows" if est else "",
            params.describe(),
        )

    # -- main loop --------------------------------------------------------------

    def _attempt(self, task: Task, wait_quota: bool) -> bool:
        """Execute one claimed task and settle it. False means stop the run."""
        try:
            self._execute(task)
        except ShutdownRequested:
            self.store.release_task(task.task_id)
            self.board.task_end("released")
            log.info("task=%s released (shutdown mid-task)", task.task_id)
            return False
        except QuotaExhausted as e:
            self.store.release_task(task.task_id)
            self.board.task_end("released", note=str(e))
            if not wait_quota:
                log.info("%s — exiting for the next scheduled run to resume", e)
                return False
            self._sleep_until_quota_reset()
        except TransientError as e:
            status = self.store.fail_task(
                task.task_id,
                str(e),
                permanent=False,
                max_attempts=self.cfg.retry.task_max_attempts,
            )
            self.tasks_failed += 1
            self.board.task_end(status, note=str(e))
            log.warning("task=%s transient failure (-> %s): %s", task.task_id, status, e)
            if status == "failed":  # not dead, and not cancelled mid-flight
                self._backoff(task.attempts, e.retry_after, str(e))
        except (PermanentError, CannotSplit) as e:
            self.store.fail_task(
                task.task_id,
                str(e),
                permanent=True,
                max_attempts=self.cfg.retry.task_max_attempts,
            )
            self.tasks_failed += 1
            self.board.task_end("dead", note=str(e))
            log.error("task=%s DEAD (permanent): %s", task.task_id, e)
        except Exception as e:
            # A bug must not lose the task or kill a weeks-long run.
            status = self.store.fail_task(
                task.task_id,
                f"unexpected: {e!r}",
                permanent=False,
                max_attempts=self.cfg.retry.task_max_attempts,
            )
            self.tasks_failed += 1
            self.board.task_end(status, note=f"unexpected: {e!r}")
            log.exception("task=%s unexpected error (-> %s)", task.task_id, status)
            self._backoff(task.attempts, None, f"unexpected: {e!r}")
        return True

    def run(
        self,
        job_id: str | None = None,
        job_kind: str | None = None,
        max_calls: int | None = None,
        stop_after_hours: float | None = None,
        wait_quota: bool = True,
    ) -> int:
        deadline = time.monotonic() + stop_after_hours * 3600 if stop_after_hours else None
        self.store.reset_stale_in_progress(None)  # sole worker: reclaim everything
        log.info("worker start pid=%d job=%s kind=%s", os.getpid(), job_id or "*", job_kind or "*")
        self.board.started(mode=job_kind or "run", job_id=job_id)
        reason = "signal"
        task = None
        try:
            while not self.shutdown.is_set():
                if deadline and time.monotonic() >= deadline:
                    log.info("--stop-after reached")
                    reason = "stop-after"
                    break
                if max_calls is not None and self.calls_made >= max_calls:
                    log.info("--max-calls reached (%d)", self.calls_made)
                    reason = "max-calls"
                    break

                task = self.store.claim_next_task(job_id, kind=job_kind)
                if task is None:
                    log.info("queue drained")
                    reason = "drained"
                    break
                self.board.task_begin(task)
                if not self._attempt(task, wait_quota):
                    reason = "signal" if self.shutdown.is_set() else "quota"
                    break
                task = None
        except BaseException:
            # A second signal (or a crash) lands here mid-task: hand the task back
            # instead of leaving it in_progress until the next worker reclaims it.
            if task is not None:
                self.store.release_task(task.task_id)
            reason = "aborted"
            raise
        finally:
            self.board.stopped(reason)

        interrupted = self.shutdown.is_set()
        log.info(
            "worker stop reason=%s calls=%d done=%d split=%d failed=%d quota_used=%d",
            "signal" if interrupted else "normal",
            self.calls_made,
            self.tasks_done,
            self.tasks_split,
            self.tasks_failed,
            self.quota.used_today(),
        )
        return 130 if interrupted else 0


def cmd_run(
    cfg: Config,
    *,
    job_id: str | None = None,
    max_calls: int | None = None,
    stop_after_hours: float | None = None,
    wait_quota: bool = True,
    live: bool = False,
) -> int:
    """live=True draws the inline progress display (an interactive terminal)."""
    cfg.require_key()  # fail fast before touching state
    store = Store(cfg.paths.state_db)
    try:
        with worker_lock(cfg.paths.state_dir / "worker.lock"):
            client = ComtradeClient(cfg)
            quota = QuotaLedger(store, cfg.quota)
            refs = Refs(client, cfg.paths.refs)
            board = StatusBoard(cfg.paths.state_dir / STATUS_FILE)
            worker = Worker(cfg, store, client, quota, refs, board)
            worker.install_signal_handlers()
            with ExitStack() as stack:
                if live:
                    from .ui.inline import InlineProgress

                    stack.enter_context(InlineProgress(cfg, store, board, job_id=job_id))
                return worker.run(
                    job_id=job_id,
                    max_calls=max_calls,
                    stop_after_hours=stop_after_hours,
                    wait_quota=wait_quota,
                )
    finally:
        store.close()
