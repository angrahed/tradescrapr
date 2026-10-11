"""The dashboard application: input handling, worker control, and the frame
loop. Rendering lives in overview.py and pages.py, data gathering in snapshot.py.

Frames are cheap (only changed lines reach the terminal), so the loop runs at
ten frames a second while the worker is busy — spinners, elapsed timers, and
byte counters move smoothly — and drops to one a second, on the second, when
nothing is animating.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

from rich.console import Console

from ..config import Config
from ..errors import TradescraprError
from ..livestatus import STATUS_FILE, StatusReader
from ..state import Store
from . import control
from .canvas import Block, color_system_for
from .context import LOG_FILTERS, WINDOWS, Confirm, Ctx, UIState, Worker, reconcile
from .fmt import Names
from .overview import order_jobs
from .pages import screen
from .snapshot import Collector, Snapshot
from .term import Terminal
from .theme import Theme, next_theme, save_theme

ANIMATED = ("running", "starting", "stopping", "waiting")


class App:
    def __init__(self, cfg: Config, *, theme: Theme, refresh: float = 1.0):
        self.cfg = cfg
        self.theme = theme
        self.names = Names(cfg.paths.refs)
        self.collector = Collector(cfg, interval=refresh)
        self.reader = StatusReader(cfg.paths.state_dir / STATUS_FILE)
        self.ui = UIState()
        self.memo: dict = {}
        self.running = True
        self.worker: Worker | None = None
        self._seen: tuple | None = None  # live-status fingerprint, to spot finished tasks
        self._t0 = time.monotonic()
        self._spawn_seen = False  # the worker we launched has held the lock
        self._invalidate: Callable[[], None] | None = None

    # -- frames -------------------------------------------------------------------

    @property
    def snap(self) -> Snapshot:
        snap = self.collector.latest
        return snap if snap is not None else self.collector.prime()

    def frame(self, width: int, height: int, now: float | None = None) -> Block:
        now = time.time() if now is None else now
        snap = self.snap
        doc, changed = self.reader.poll()
        if changed and doc is not None:
            mark = (doc.get("state"), doc.get("pid"), (doc.get("last") or {}).get("at"))
            if mark != self._seen:
                self._seen = mark
                self.collector.poke()  # a task settled or the worker came/went: refresh now
        worker = reconcile(snap, doc, self.ui, now)
        self.worker = worker
        live = worker.live or {}
        queue = (live.get("job"), "monitor" if live.get("mode") == "monitor" else None)
        if queue != self.collector.queue:
            self.collector.queue = queue
            self.collector.poke(full=False)
        self._housekeeping(worker, now)
        ctx = Ctx(
            theme=self.theme,
            names=self.names,
            snap=snap,
            worker=worker,
            ui=self.ui,
            now=now,
            t=time.monotonic() - self._t0,
            memo=self.memo,
        )
        return screen(ctx, width, height)

    def _housekeeping(self, worker: Worker, now: float) -> None:
        ui = self.ui
        if ui.stop_pid and worker.pid != ui.stop_pid:
            ui.stop_pid = None  # that worker is gone
        sp = ui.spawned
        if sp is not None:
            if worker.pid == sp.pid:
                self._spawn_seen = True
            err = sp.failure()
            if err:
                what = "worker exited" if self._spawn_seen else "worker failed to start"
                ui.spawn_error = f"{what}: {err}"
                ui.say(ui.spawn_error, "bad", 12, now)
                ui.spawned = None
            elif sp.proc.poll() is not None:
                ui.spawned = None  # finished or stopped; poll() reaped it
        if worker.state == "running" and ui.spawn_error:
            ui.spawn_error = None
        if ui.toast and ui.toast.until <= now:
            ui.toast = None

    def animating(self) -> bool:
        w = self.worker
        return bool(w and w.state in ANIMATED) or self.ui.view == "job"

    # -- input ----------------------------------------------------------------------

    def handle(self, key: str) -> None:
        ui = self.ui
        now = time.time()
        if ui.help:
            ui.help = False
            return
        if ui.confirm is not None:
            if key in ("y", "Y"):
                action, ui.confirm = ui.confirm.action, None
                action()
            elif key in ("n", "N", "esc", "q", "enter"):
                ui.confirm = None
            return

        if key in ("q", "Q"):
            self.running = False
            return
        if key in ("?", "h", "f1"):
            ui.help = True
            return
        if key == "t":
            self.theme = next_theme(self.theme)
            save_theme(self.cfg.paths.state_dir, self.theme)
            ui.say(f"theme: {self.theme.name}", "info", 3, now)
            return
        if key in ("r", "redraw"):
            self.collector.poke()
            if self._invalidate:
                self._invalidate()
            return
        if key == "[":
            ui.window = max(ui.window - 1, 0)
            return
        if key == "]":
            ui.window = min(ui.window + 1, len(WINDOWS) - 1)
            return
        if key == "s":
            self.start(None)
            return
        if key == "S":
            self.start(ui.selected)
            return
        if key == "x":
            self.stop()
            return
        if key == "c":
            self.resume()
            return
        if key == "R":
            self.ask_retry()
            return
        if key == "C":
            self.ask_cancel()
            return
        if key == "l" and ui.view != "log":
            ui.view, ui.log_scroll = "log", 0
            return

        if ui.view == "overview":
            self._overview_key(key)
        elif ui.view == "job":
            self._job_key(key)
        elif ui.view == "log":
            self._log_key(key)

    def _move(self, delta: int) -> None:
        jobs = order_jobs(self.snap.jobs)
        if not jobs:
            return
        ids = [j.job_id for j in jobs]
        i = ids.index(self.ui.selected) if self.ui.selected in ids else 0
        self.ui.selected = ids[max(0, min(i + delta, len(ids) - 1))]

    def _overview_key(self, key: str) -> None:
        if key in ("up", "k"):
            self._move(-1)
        elif key in ("down", "j"):
            self._move(1)
        elif key == "pgup":
            self._move(-10)
        elif key == "pgdn":
            self._move(10)
        elif key in ("home", "g"):
            self._move(-(10**6))
        elif key in ("end", "G"):
            self._move(10**6)
        elif key in ("enter", "right") and self.ui.selected:
            self.open_job(self.ui.selected)

    def open_job(self, job_id: str) -> None:
        self.ui.view, self.ui.selected, self.ui.detail_scroll = "job", job_id, 0
        self.collector.focus = job_id
        self.collector.poke(full=False)

    def _back(self) -> None:
        self.ui.view = "overview"
        self.collector.focus = None

    def _job_key(self, key: str) -> None:
        ui = self.ui
        if key in ("esc", "backspace", "enter"):
            self._back()
        elif key in ("up", "k"):
            ui.detail_scroll -= 1
        elif key in ("down", "j"):
            ui.detail_scroll += 1
        elif key == "pgup":
            ui.detail_scroll -= 15
        elif key == "pgdn":
            ui.detail_scroll += 15
        elif key in ("home", "g"):
            ui.detail_scroll = 0
        elif key in ("end", "G"):
            ui.detail_scroll = 10**6
        elif key in ("left", "right"):
            self._move(-1 if key == "left" else 1)
            if ui.selected:
                self.open_job(ui.selected)
        ui.detail_scroll = max(ui.detail_scroll, 0)

    def _log_key(self, key: str) -> None:
        ui = self.ui
        if key in ("esc", "backspace", "l"):
            self._back()
        elif key in ("down", "j"):
            ui.log_scroll += 1
        elif key in ("up", "k"):
            ui.log_scroll -= 1
        elif key == "pgdn":
            ui.log_scroll += 20
        elif key == "pgup":
            ui.log_scroll -= 20
        elif key in ("home", "g"):
            ui.log_scroll = 0
        elif key in ("end", "G"):
            ui.log_scroll = 10**6
        elif key in ("f", "tab", "right", "left"):
            i = LOG_FILTERS.index(ui.log_filter) + (-1 if key == "left" else 1)
            ui.log_filter, ui.log_scroll = LOG_FILTERS[i % len(LOG_FILTERS)], 0
        ui.log_scroll = max(ui.log_scroll, 0)

    # -- actions -----------------------------------------------------------------------

    def start(self, job_id: str | None) -> None:
        ui, now = self.ui, time.time()
        w = self.worker
        if w and w.state != "idle":
            ui.say("a worker is already running", "warn", 4, now)
            return
        snap = self.snap
        pending = snap.job(job_id).remaining if job_id and snap.job(job_id) else snap.remaining
        if not pending:
            ui.say("nothing queued to download", "warn", 4, now)
            return
        try:
            sp = control.start_worker(self.cfg, job_id=job_id)
        except TradescraprError as e:
            ui.spawn_error = str(e).split(". ")[0]
            ui.say(ui.spawn_error, "bad", 10, now)
            return
        ui.spawned, ui.spawn_error = sp, None
        self._spawn_seen = False
        scope = f"job {snap.job(job_id).name}" if job_id and snap.job(job_id) else "all queued work"
        awake = " · keeping the Mac awake" if sp.keep_awake else ""
        ui.say(
            f"worker started on {scope} (pid {sp.pid}) — keeps running after you quit{awake}",
            "ok",
            8,
            now,
        )
        self.collector.poke()

    def stop(self) -> None:
        ui, now = self.ui, time.time()
        w = self.worker
        if not w or not w.pid or w.pid < 0:
            ui.say("no worker is running", "warn", 4, now)
            return
        try:
            if ui.stop_pid == w.pid:
                control.stop_worker(self.cfg, again=True)
                ui.say("stopping now — the current task goes back to the queue", "warn", 6, now)
            else:
                pid = control.stop_worker(self.cfg)
                ui.stop_pid = pid
                ui.say("stopping after the current task — x again to stop now", "warn", 8, now)
        except TradescraprError as e:
            ui.say(str(e), "warn", 5, now)
        self.collector.poke()

    def resume(self) -> None:
        ui, now = self.ui, time.time()
        if not (self.worker and self.worker.state == "stalled"):
            ui.say("the worker is not suspended", "info", 3, now)
            return
        try:
            control.continue_worker(self.cfg)
            ui.say("worker resumed", "ok", 4, now)
        except TradescraprError as e:
            ui.say(str(e), "bad", 6, now)
        self.collector.poke()

    def ask_retry(self) -> None:
        ui, now, snap = self.ui, time.time(), self.snap
        job = snap.job(ui.selected)
        dead_all = sum(j.n("dead") for j in snap.jobs)
        if not dead_all:
            ui.say("no dead tasks to retry", "info", 3, now)
            return
        if job and job.n("dead"):
            target, n, what = job.job_id, job.n("dead"), f"job {job.name}"
        else:
            target, n, what = None, dead_all, "all jobs"

        def act() -> None:
            revived = self._with_store(lambda s: s.retry_dead(job_id=target))
            idle = self.worker is not None and self.worker.state == "idle"
            hint = " — press s to fetch them" if idle else ""
            ui.say(f"revived {revived} dead task(s){hint}", "ok", 6, time.time())

        ui.confirm = Confirm(
            title="RETRY DEAD TASKS",
            lines=[
                f"Put {n:,} dead task(s) from {what} back in the queue?",
                "Their attempt counters reset. Fix the cause first if the error was permanent",
                "(a bad key, an invalid code) or they will die again.",
            ],
            action=act,
        )

    def ask_cancel(self) -> None:
        ui, now, snap = self.ui, time.time(), self.snap
        job = snap.job(ui.selected)
        if job is None:
            return
        if not job.remaining:
            ui.say(f"job {job.name} has nothing left to cancel", "info", 3, now)
            return

        def act() -> None:
            n = self._with_store(lambda s: s.cancel_job(job.job_id))
            ui.say(f"cancelled {n} task(s) of {job.name} — data kept", "warn", 6, time.time())

        ui.confirm = Confirm(
            title=f"CANCEL JOB {job.name}",
            lines=[
                f"Mark the {job.remaining:,} unfinished task(s) of this job cancelled?",
                "Downloaded data stays on disk. A task already in flight may still finish.",
                "There is no undo here; re-planning an edited spec makes a new job.",
            ],
            action=act,
            danger=True,
        )

    def _with_store(self, fn: Callable[[Store], int]) -> int:
        store = Store(self.cfg.paths.state_db)
        try:
            return fn(store)
        finally:
            store.close()
            self.collector.poke()

    # -- loop ---------------------------------------------------------------------------

    def run(self, term: Terminal, console: Console | None = None) -> int:
        cs = None if os.getenv("NO_COLOR") else color_system_for(console or Console())
        self._invalidate = term.invalidate
        while self.running:
            size = term.size()
            block = self.frame(*size)
            term.draw(block.lines, size, cs)
            if self.animating():
                timeout = 0.1
            else:
                timeout = max(1.0 - (time.time() % 1.0), 0.02)  # wake on the second
            key = term.read_key(timeout)
            while key is not None and self.running:
                self.handle(key)
                key = term.read_key(0)
        return 0
