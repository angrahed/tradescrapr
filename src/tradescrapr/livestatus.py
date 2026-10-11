"""Live worker status: a small JSON document the worker rewrites as it works.

The SQLite store records what has settled; this file records what is happening
right now — the phase of the in-flight call, bytes received so far, a backoff or
quota-sleep countdown, this run's counters — so a dashboard in another process
(and the inline display of a foreground run) can show a call that takes minutes
without polling the database. Writes are atomic (tmp + os.replace) and
throttled, so readers never see a torn document and a fast stream of progress
callbacks costs a few small writes per second.

The worker lock stays the source of truth for liveness: a status file whose pid
no longer holds state/worker.lock describes the last run, not a running one.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .state import Task

log = logging.getLogger(__name__)

STATUS_FILE = "worker.json"
VERSION = 1

# Phases of one task, in order. Bulk tasks (premium) list/download/convert instead
# of request/receive/parse; waits between tasks are "backoff" and "quota".
GET_PHASES = ("request", "receive", "parse", "write")
BULK_PHASES = ("list", "download", "convert", "write")

Listener = Callable[[str], None]


def _task_info(task: Task, now: float) -> dict[str, Any]:
    p = task.params
    if task.kind == "bulk":
        cmd = "bulk file"
    elif p.get("cmd_codes") is not None:
        cmd = f"{len(p['cmd_codes'])} codes"
    elif p.get("cmd_level") is not None:
        cmd = f"AG{p['cmd_level']}"
    else:
        cmd = "TOTAL"
    partners = p.get("partners")
    return {
        "task_id": task.task_id,
        "job_id": task.job_id,
        "kind": task.kind,
        "dataset": task.dataset,
        "freq": task.freq,
        "period": task.period,
        "reporter": task.reporter,
        "flows": list(p.get("flows") or []),
        "cmd": cmd,
        "cl": p.get("cl_code", ""),
        "partners": None if partners is None else len(partners),
        "full": p.get("partner2", "0") is None,
        "depth": task.depth,
        "attempt": task.attempts,
        "claimed": now,
        "rows": None,
    }


class StatusBoard:
    """Worker side. Every mutation goes through the lock; publishing is forced on
    state changes and throttled to `min_interval` for byte-progress updates.
    With path=None nothing is written (tests, or callers that only want the
    in-process snapshot)."""

    def __init__(
        self,
        path: Path | None,
        *,
        clock: Callable[[], float] = time.time,
        min_interval: float = 0.2,
    ):
        self.path = path
        self._clock = clock
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last_write = 0.0
        self._stop_flag = False  # set from the signal handler, folded in on publish
        self._listeners: list[Listener] = []
        now = clock()
        self._doc: dict[str, Any] = {
            "v": VERSION,
            "pid": os.getpid(),
            "mode": None,
            "job": None,
            "started": now,
            "updated": now,
            "state": "starting",
            "phase": None,
            "phase_since": now,
            "until": None,
            "note": "",
            "stopping": False,
            "reason": None,
            "task": None,
            "xfer": None,
            "session": {
                "calls": 0,
                "done": 0,
                "split": 0,
                "failed": 0,
                "dead": 0,
                "rows": 0,
                "wire": 0,
            },
            "last": None,
        }

    # -- in-process consumers ------------------------------------------------

    def subscribe(self, fn: Listener) -> None:
        """fn(event) runs on the worker thread after task_end/started/stopped,
        outside the lock — the inline display refreshes its queue numbers there."""
        self._listeners.append(fn)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            doc = copy.deepcopy(self._doc)
        doc["stopping"] = doc["stopping"] or self._stop_flag
        return doc

    def _emit(self, event: str) -> None:
        # Display trouble must never reach the worker's error handling (which
        # would mark an already-finished task as failed).
        for fn in self._listeners:
            try:
                fn(event)
            except Exception:
                log.debug("status listener failed on %s", event, exc_info=True)

    # -- lifecycle -------------------------------------------------------------

    def started(self, *, mode: str, job_id: str | None) -> None:
        with self._lock:
            now = self._clock()
            self._doc.update(
                mode=mode,
                job=job_id,
                started=now,
                state="working",
                phase="claim",
                phase_since=now,
                until=None,
                note="",
                reason=None,
            )
            self._publish(force=True)
        self._emit("started")

    def stopping(self) -> None:
        """Safe to call from a signal handler: never blocks on the lock."""
        self._stop_flag = True
        if self._lock.acquire(blocking=False):
            try:
                self._publish(force=True)
            finally:
                self._lock.release()

    def stopped(self, reason: str) -> None:
        with self._lock:
            now = self._clock()
            self._doc.update(
                state="stopped",
                reason=reason,
                phase=None,
                phase_since=now,
                until=None,
                task=None,
                xfer=None,
            )
            self._publish(force=True)
        self._emit("stopped")

    # -- task progress -------------------------------------------------------------

    def task_begin(self, task: Task) -> None:
        with self._lock:
            now = self._clock()
            self._doc.update(
                state="working",
                task=_task_info(task, now),
                xfer=None,
                phase="prepare",
                phase_since=now,
                until=None,
                note="",
            )
            self._publish(force=True)

    def call(self) -> None:
        """One quota-debited API call was made."""
        with self._lock:
            self._doc["session"]["calls"] += 1

    def phase(self, name: str, *, note: str = "", rows: int | None = None) -> None:
        with self._lock:
            d = self._doc
            d.update(phase=name, phase_since=self._clock(), until=None, note=note)
            if rows is not None and d["task"] is not None:
                d["task"]["rows"] = rows
            self._publish(force=True)

    def note(self, text: str) -> None:
        """A short remark on the current phase (a transport retry, chunks written)."""
        with self._lock:
            self._doc["note"] = text
            self._publish(force=True)

    def transfer(self, stage: str, wire: int, total: int | None) -> None:
        """Client progress callback. stage: 'headers' (response began),
        'body' (more bytes), 'done' (body complete)."""
        with self._lock:
            now = self._clock()
            d = self._doc
            if stage == "headers":
                if d["phase"] == "request":
                    d["phase"], d["phase_since"] = "receive", now
                d["xfer"] = {"wire": wire, "total": total, "since": now, "at": now}
                self._publish(force=True)
                return
            xfer = d["xfer"]
            if xfer is None:  # tolerate a client that skipped 'headers'
                xfer = d["xfer"] = {"wire": 0, "total": total, "since": now, "at": now}
            xfer["wire"], xfer["at"] = wire, now
            if total is not None:
                xfer["total"] = total
            if stage == "done":
                if d["phase"] == "receive":
                    d["phase"], d["phase_since"] = "parse", now
                self._publish(force=True)
            else:
                self._publish()

    def task_end(self, outcome: str, *, rows: int = 0, wire: int = 0, note: str = "") -> None:
        """outcome: done | split | failed | dead | released."""
        with self._lock:
            now = self._clock()
            d = self._doc
            s = d["session"]
            if outcome in ("done", "split", "failed", "dead"):
                s[outcome] += 1
            s["rows"] += rows
            s["wire"] += wire
            task = d["task"] or {}
            d["last"] = {
                "task_id": task.get("task_id"),
                "job_id": task.get("job_id"),
                "reporter": task.get("reporter"),
                "period": task.get("period"),
                "flows": task.get("flows"),
                "outcome": outcome,
                "rows": rows,
                "wire": wire,
                "secs": now - task["claimed"] if task else None,
                "at": now,
                "note": note[:300],
            }
            d.update(task=None, xfer=None, phase="claim", phase_since=now, until=None, note="")
            self._publish(force=True)
        self._emit("task_end")

    def waiting(self, kind: str, seconds: float, note: str = "") -> None:
        """A sleep between tasks: kind is 'backoff' or 'quota'."""
        with self._lock:
            now = self._clock()
            self._doc.update(
                state="waiting",
                phase=kind,
                phase_since=now,
                until=now + max(seconds, 0.0),
                note=note[:300],
                task=None,
                xfer=None,
            )
            self._publish(force=True)

    # -- persistence -------------------------------------------------------------

    def _publish(self, force: bool = False) -> None:
        """Caller holds the lock."""
        now = self._clock()
        d = self._doc
        d["updated"] = now
        d["stopping"] = d["stopping"] or self._stop_flag
        if self.path is None:
            return
        if not force and now - self._last_write < self._min_interval:
            return
        self._last_write = now
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(d, separators=(",", ":")))
            os.replace(tmp, self.path)
        except OSError:
            # Status is best-effort: a full disk must not fail a download.
            with contextlib.suppress(OSError):
                tmp.unlink()


def read_status(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("v") != VERSION:
        return None
    return doc


class StatusReader:
    """Dashboard side: re-parses only when the file actually changed."""

    def __init__(self, path: Path):
        self.path = path
        self._sig: tuple[int, int] | None = None
        self._doc: dict[str, Any] | None = None

    def poll(self) -> tuple[dict[str, Any] | None, bool]:
        try:
            st = self.path.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            changed = self._doc is not None
            self._sig, self._doc = None, None
            return None, changed
        if sig == self._sig:
            return self._doc, False
        self._sig = sig
        self._doc = read_status(self.path)
        return self._doc, True
