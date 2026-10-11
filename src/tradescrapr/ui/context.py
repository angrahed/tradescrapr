"""Dashboard state and shared rendering helpers: the UI state machine's data,
the worker's reconciled status, and the small Text builders every panel uses."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from rich.style import Style
from rich.text import Text

from . import fmt
from .control import Spawned
from .fmt import Names
from .metrics import quota_bound
from .snapshot import Snapshot
from .theme import Theme

WINDOWS = ((15 * 60, "15m"), (3600, "1h"), (6 * 3600, "6h"), (24 * 3600, "24h"))
LOG_FILTERS = ("all", "done", "problems", "system")
MIN_W, MIN_H = 60, 16


# ---------------------------------------------------------------------------
# UI state
# ---------------------------------------------------------------------------


@dataclass
class Toast:
    text: str
    kind: str  # ok | warn | bad | info
    until: float


@dataclass
class Confirm:
    title: str
    lines: list[str]
    action: Callable[[], None]
    danger: bool = False


@dataclass
class UIState:
    view: str = "overview"  # overview | job | log
    selected: str | None = None  # job id
    job_scroll: int = 0
    detail_scroll: int = 0
    log_scroll: int = 0
    log_filter: str = "all"
    window: int = 1  # index into WINDOWS
    help: bool = False
    confirm: Confirm | None = None
    toast: Toast | None = None
    stop_pid: int | None = None  # worker we sent SIGTERM to
    spawned: Spawned | None = None
    spawn_error: str | None = None
    static: bool = False  # one-shot snapshot: no key hints

    def say(self, text: str, kind: str = "info", secs: float = 6.0, now: float = 0.0) -> None:
        self.toast = Toast(text, kind, now + secs)


# ---------------------------------------------------------------------------
# Worker state, reconciled from the lock probe and the live status file
# ---------------------------------------------------------------------------


@dataclass
class Worker:
    state: str  # idle | starting | running | waiting | stopping | stalled
    pid: int | None
    live: dict[str, Any] | None  # status doc of the running worker
    last: dict[str, Any] | None  # status doc of the previous run
    up_s: float | None = None


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reconcile(snap: Snapshot, doc: dict[str, Any] | None, ui: UIState, now: float) -> Worker:
    pid = snap.worker_pid
    live = None
    if doc and doc.get("state") != "stopped":
        dpid = doc.get("pid")
        fresh = now - float(doc.get("updated") or 0) < 5
        if (pid and dpid == pid) or (fresh and isinstance(dpid, int) and _pid_exists(dpid)):
            live = doc
            pid = pid or dpid
    last = doc if doc and live is None else None
    up = now - float(live["started"]) if live and live.get("started") else None
    if snap.worker_stalled:
        state = "stalled"
    elif (live and live.get("stopping")) or (pid and pid == ui.stop_pid):
        state = "stopping"
    elif live and live.get("state") == "waiting":
        state = "waiting"
    elif pid:
        state = "running"
    elif ui.spawned and ui.spawned.proc.poll() is None and now - ui.spawned.started < 15:
        state = "starting"
    else:
        state = "idle"
    return Worker(state, pid, live, last, up)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


@dataclass
class Ctx:
    theme: Theme
    names: Names
    snap: Snapshot
    worker: Worker
    ui: UIState
    now: float  # wall clock, epoch seconds
    t: float  # animation clock
    memo: dict = field(default_factory=dict)

    def s(self, color: str, bold: bool = False, bg: str | None = None) -> Style:
        return self.theme.s(color, bold=bold, bg=bg)


def spread(left: Text, right: Text, width: int) -> Text:
    """left ... right on one line of `width` cells (left gives way first)."""
    room = width - right.cell_len - 1
    if room < 1:
        out = right.copy()
        out.truncate(width, overflow="ellipsis")
        return out
    left = left.copy()
    if left.cell_len > room:
        left.truncate(room, overflow="ellipsis")
    return left + Text(" " * (width - left.cell_len - right.cell_len)) + right


def cell(text: str | Text, width: int, style: Style | str = "", align: str = "l") -> Text:
    t = text.copy() if isinstance(text, Text) else Text(str(text), style)
    if t.cell_len > width:
        t.truncate(width, overflow="ellipsis")
    pad = " " * (width - t.cell_len)
    return (Text(pad) + t) if align == "r" else (t + Text(pad))


def dots(c: Ctx, *items: tuple[str, str] | Text) -> Text:
    """'a · b · c' with dim separators; items are (text, color) or Text."""
    out = Text()
    for i, it in enumerate(items):
        if i:
            out.append(" · ", c.s(c.theme.faint))
        if isinstance(it, Text):
            out.append_text(it)
        else:
            out.append(it[0], c.s(it[1]))
    return out


def kv(c: Ctx, label: str, value: str, color: str | None = None, bold: bool = False) -> Text:
    return Text.assemble((f"{label} ", c.s(c.theme.dim)), (value, c.s(color or c.theme.text, bold)))


def task_scope(c: Ctx, flows: Sequence[str], cmd: str = "", partners: Any = None) -> list[Text]:
    th = c.theme
    out = [Text(fmt.flows_label(list(flows)), c.s(th.text))]
    if cmd:
        out.append(Text(cmd, c.s(th.dim)))
    if partners is None or partners == "all":
        out.append(Text("all partners", c.s(th.dim)))
    else:
        n = str(partners).split()[0]
        out.append(Text(f"{n} partners", c.s(th.dim)))
    return out


def eta_seconds(c: Ctx, left_s: float | None, remaining: int) -> tuple[float | None, bool]:
    """Wall-clock seconds to finish `remaining` tasks needing `left_s` of busy
    time, and whether the daily quota (not speed) is the limit."""
    if remaining <= 0:
        return 0.0, False
    if left_s is None:
        return None, False
    snap = c.snap
    return quota_bound(
        left_s,
        remaining,
        budget=snap.budget,
        used_today=snap.quota_used,
        reset_s=snap.quota_reset_s,
    )


def eta_text(c: Ctx, left_s: float | None, remaining: int) -> tuple[str, bool]:
    """('1h 52m', quota_bound), '—' when nothing is left, '?' before any pace."""
    if remaining <= 0:
        return "—", False
    secs, bound = eta_seconds(c, left_s, remaining)
    return ("?" if secs is None else fmt.span(secs)), bound


# ---------------------------------------------------------------------------
