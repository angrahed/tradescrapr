"""Structured events from tradescrapr.log, for the activity feed and log view.

The worker's log lines are the event stream (they survive restarts and are
written by every process that shares the state dir). Each interesting line is
parsed into an Event with typed fields; everything else is skipped. LogTail
follows the file incrementally — after the first read only appended bytes are
parsed — and copes with rotation.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .fmt import human_bytes


@dataclass(frozen=True)
class Event:
    when: str  # "HH:MM:SS"
    day: str  # "YYYY-MM-DD"
    kind: str  # done split retry dead error backoff quota start stop info warn monitor
    text: str = ""  # message for kinds without structured fields
    task_id: str = ""
    job_id: str = ""
    reporter: str = ""
    period: str = ""
    flows: tuple[str, ...] = ()
    cmd: str = ""
    partners: str = ""
    rows: int | None = None
    secs: float | None = None
    wire: int | None = None
    parts: int | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def problem(self) -> bool:
        return self.kind in ("retry", "dead", "error", "warn", "quota")


_HEAD = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})[,.]\d+ (\w+) (\S+) (.*)$")
_DESC = {
    "reporter": re.compile(r"reporter=(\S+)"),
    "period": re.compile(r"period=(\S+)"),
    "flows": re.compile(r"flows=(\S*)"),
    "cmd": re.compile(r"cmd=(\d+ codes|\S+)"),
    "partners": re.compile(r"partners=(\d+ partners|\S+)"),
}
_KV = re.compile(r"(\w+)=([^\s]+)")

_DONE = re.compile(r"task=(\w+) job=(\S+) (.*?)status=done records=(\d+)(.*)$")
_SPLIT = re.compile(
    r"task=(\w+) truncated at cap=(\d+) -> split into (\d+) children \(depth (\d+)[^)]*\)\s*(.*)$"
)
_RETRY = re.compile(r"task=(\w+) transient failure \(-> (\w+)\): (.*)$")
_DEAD = re.compile(r"task=(\w+) DEAD \(permanent\): (.*)$")
_UNEXPECTED = re.compile(r"task=(\w+) unexpected error \(-> (\w+)\)")
_RELEASED = re.compile(r"task=(\w+) released \(shutdown mid-task\)")
_BULK_DL = re.compile(r"task=(\w+) bulk file downloaded \((\d+) bytes\)")
_BACKOFF = re.compile(r"backing off (\d+)s")
_QUOTA = re.compile(r"quota exhausted \(used=(\d+)\); sleeping (\d+) min")
_START = re.compile(r"worker start pid=(\d+) job=(\S+) kind=(\S+)")
_STOP = re.compile(r"worker stop reason=(\w+) calls=(\d+) done=(\d+) split=(\d+) failed=(\d+)")
_SIGNAL = re.compile(r"(SIG\w+) received")
_RECLAIM = re.compile(r"reclaimed (\d+) in_progress task")
_MONITOR = re.compile(r"monitor (mon-\S+): (\d+) new \+ (\d+) revised release\(s\) -> (\d+) task")


def _describe(rest: str) -> dict:
    out: dict = {}
    for key, rx in _DESC.items():
        m = rx.search(rest)
        if m:
            out[key] = m.group(1)
    if "flows" in out:
        out["flows"] = tuple(f for f in out["flows"].split(",") if f)
    return out


def parse_line(line: str) -> Event | None:
    m = _HEAD.match(line)
    if not m:
        return None
    day, when, level, _logger, msg = m.groups()
    return parse_message(msg, level=level, day=day, when=when)


def parse_message(msg: str, *, level: str = "INFO", day: str = "", when: str = "") -> Event | None:
    """Classify one log message. Also used live by the inline display, which
    sees LogRecords rather than file lines."""
    base = {"day": day, "when": when}
    if m := _DONE.match(msg):
        task_id, job_id, desc, rows, tail = m.groups()
        kv = dict(_KV.findall(tail))
        d = _describe(desc)
        bulk = desc.startswith("bulk ")
        return Event(
            kind="done",
            task_id=task_id,
            job_id=job_id,
            rows=int(rows),
            secs=float(kv["dur"].rstrip("s")) if "dur" in kv else None,
            wire=int(kv["bytes"]) if kv.get("bytes", "").isdigit() else None,
            cmd="bulk" if bulk else d.get("cmd", ""),
            reporter=d.get("reporter", ""),
            period=d.get("period", ""),
            flows=d.get("flows", ()),
            partners=d.get("partners", ""),
            **base,
        )
    if m := _SPLIT.match(msg):
        task_id, cap, parts, depth, desc = m.groups()
        d = _describe(desc)
        return Event(
            kind="split",
            task_id=task_id,
            parts=int(parts),
            reporter=d.get("reporter", ""),
            period=d.get("period", ""),
            flows=d.get("flows", ()),
            cmd=d.get("cmd", ""),
            partners=d.get("partners", ""),
            extra={"cap": cap, "depth": depth},
            **base,
        )
    if m := _RETRY.match(msg):
        task_id, status, err = m.groups()
        return Event(
            kind="dead" if status == "dead" else "retry", task_id=task_id, text=err, **base
        )
    if m := _DEAD.match(msg):
        return Event(kind="dead", task_id=m.group(1), text=m.group(2), **base)
    if m := _UNEXPECTED.match(msg):
        return Event(
            kind="error", task_id=m.group(1), text=f"unexpected error -> {m.group(2)}", **base
        )
    if m := _RELEASED.match(msg):
        return Event(kind="info", task_id=m.group(1), text="released back to the queue", **base)
    if m := _BULK_DL.match(msg):
        return Event(
            kind="info",
            task_id=m.group(1),
            wire=int(m.group(2)),
            text=f"bulk file downloaded ({human_bytes(int(m.group(2)))})",
            **base,
        )
    if msg.startswith("transport retry "):
        return Event(kind="backoff", text=f"server trouble: {msg}", **base)
    if m := _BACKOFF.search(msg):
        return Event(
            kind="backoff", secs=float(m.group(1)), text=f"backing off {m.group(1)}s", **base
        )
    if m := _QUOTA.search(msg):
        return Event(
            kind="quota",
            text=f"quota spent ({m.group(1)} calls) — sleeping {m.group(2)} min until reset",
            **base,
        )
    if "server-side exhaustion" in msg:
        return Event(kind="quota", text="server says the quota is spent", **base)
    if m := _START.search(msg):
        pid, job, kind = m.groups()
        scope = "all queued work" if job == "*" else f"job {job}"
        if kind == "monitor":
            scope = "monitor queue"
        return Event(kind="start", text=f"worker started (pid {pid}) on {scope}", **base)
    if m := _STOP.search(msg):
        reason, calls, done, split_, failed = m.groups()
        return Event(
            kind="stop",
            text=f"worker stopped ({reason}): {calls} calls · {done} done · {split_} split"
            + (f" · {failed} failed" if failed != "0" else ""),
            extra={"reason": reason},
            **base,
        )
    if "hard exit" in msg:
        return Event(
            kind="stop", text="second signal — stopped immediately, task returned to queue", **base
        )
    if m := _SIGNAL.search(msg):
        return Event(
            kind="stop", text=f"{m.group(1)} — finishing the current task, then stopping", **base
        )
    if msg.startswith("queue drained"):
        return Event(kind="info", text="queue drained — nothing left to fetch", **base)
    if m := _RECLAIM.search(msg):
        return Event(kind="info", text=f"reclaimed {m.group(1)} interrupted task(s)", **base)
    if "--max-calls reached" in msg or "--stop-after reached" in msg:
        return Event(kind="info", text=msg.strip(), **base)
    if m := _MONITOR.search(msg):
        _job, new, revised, n = m.groups()
        return Event(
            kind="monitor",
            text=f"monitor: {new} new + {revised} revised release(s) → {n} task(s)",
            **base,
        )
    if msg.startswith("monitor run=") or msg.startswith("monitor: no new releases"):
        return Event(kind="monitor", text=msg.strip(), **base)
    if msg.startswith("superseded "):
        return Event(kind="info", text=msg.strip(), **base)
    if level in ("WARNING", "ERROR", "CRITICAL"):
        return Event(kind="error" if level != "WARNING" else "warn", text=msg.strip(), **base)
    return None


class LogTail:
    """Incremental reader of the rotating log file."""

    def __init__(self, path: Path, *, keep: int = 3000, backfill: int = 2 << 20):
        self.path = path
        self.events: deque[Event] = deque(maxlen=keep)
        self._backfill = backfill
        self._ino: int | None = None
        self._pos = 0
        self._partial = b""
        self.version = 0  # bumps whenever new events arrive

    def poll(self) -> bool:
        """Read whatever was appended since the last poll. True if events were added."""
        try:
            st = os.stat(self.path)
        except OSError:
            return False
        if self._ino != st.st_ino or st.st_size < self._pos:  # first read or rotated
            first = self._ino is None
            self._ino, self._partial = st.st_ino, b""
            self._pos = max(0, st.st_size - self._backfill) if first else 0
            skip_partial_first_line = first and self._pos > 0
        else:
            skip_partial_first_line = False
        if st.st_size == self._pos:
            return False
        try:
            with open(self.path, "rb") as f:
                f.seek(self._pos)
                data = f.read(st.st_size - self._pos)
        except OSError:
            return False
        self._pos += len(data)
        data = self._partial + data
        lines = data.split(b"\n")
        self._partial = lines.pop()  # incomplete last line, if any
        if skip_partial_first_line and lines:
            lines = lines[1:]
        added = False
        for raw in lines:
            ev = parse_line(raw.decode("utf-8", errors="replace"))
            if ev is not None:
                self.events.append(ev)
                added = True
        if added:
            self.version += 1
        return added


def event_age(ev: Event, now: dt.datetime) -> float | None:
    try:
        ts = dt.datetime.fromisoformat(f"{ev.day} {ev.when}")
    except ValueError:
        return None
    return (now - ts).total_seconds()
