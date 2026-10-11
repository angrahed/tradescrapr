"""Inline progress for a foreground `tradescrapr run` (or `monitor`) in a terminal.

The worker's log lines keep scrolling, rendered as the same events the
dashboard's activity feed shows; below them a three-line display tracks the
call in flight (phase, bytes, an estimate from similar tasks), backoff and
quota-sleep countdowns, and the queue with its ETA. Queue numbers refresh on
the worker thread whenever a task settles; the display itself repaints eight
times a second from the in-process StatusBoard, so it never touches SQLite.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from ..config import Config
from ..livestatus import StatusBoard
from ..quota import QuotaLedger
from ..state import Store
from . import fmt
from .events import parse_message
from .fmt import Names
from .metrics import build_pace, finishes, quota_bound, rates, work_left
from .overview import PHASE_TITLES, event_line
from .theme import Theme, load_theme
from .widgets import bar, scanner, spinner, stacked


class _LiveHandler(logging.Handler):
    """Prints log records above the live display, styled as activity events."""

    def __init__(self, live: Live, theme: Theme, names: Names, level: int):
        super().__init__(level)
        self.live, self.theme, self.names = live, theme, names

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            when = dt.datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            ev = parse_message(msg, level=record.levelname, when=when)
            th = self.theme
            if ev is not None:
                line = event_line(th, self.names, ev)
            else:
                color = {"WARNING": th.warn, "ERROR": th.bad, "CRITICAL": th.bad}.get(
                    record.levelname, th.dim
                )
                line = Text.assemble(
                    (f"{when} ", th.s(th.faint)), ("· ", th.s(th.faint)), (msg, th.s(color))
                )
            line.no_wrap, line.overflow = True, "ellipsis"
            self.live.console.print(line)
            if record.exc_info:
                self.live.console.print(Text(self.format(record).split("\n", 1)[-1], th.s(th.bad)))
        except Exception:  # never let display trouble break the worker
            self.handleError(record)


class InlineProgress:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        board: StatusBoard,
        *,
        job_id: str | None = None,
        kind: str | None = None,
        console: Console | None = None,
    ):
        self.cfg, self.store, self.board = cfg, store, board
        self.job_id, self.kind = job_id, kind
        self.theme = load_theme(cfg.paths.state_dir)
        self.names = Names(cfg.paths.refs)
        self.console = console or Console(stderr=True)
        self.live: Live | None = None
        self._queue: dict[str, Any] = {}
        self._swapped: list[logging.Handler] = []
        self._handler: _LiveHandler | None = None
        self._t0 = time.monotonic()

    # -- lifecycle ---------------------------------------------------------------

    def __enter__(self) -> InlineProgress:
        self._refresh_queue("init")
        self.board.subscribe(self._refresh_queue)
        root = logging.getLogger()
        level = logging.INFO
        for h in list(root.handlers):
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                level = h.level
                root.removeHandler(h)
                self._swapped.append(h)
        self.live = Live(
            get_renderable=self._render,
            console=self.console,
            refresh_per_second=8,
            transient=True,
            redirect_stdout=True,
            redirect_stderr=True,
        )
        self.live.start()
        self._handler = _LiveHandler(self.live, self.theme, self.names, level)
        root.addHandler(self._handler)
        return self

    def __exit__(self, *exc) -> None:
        root = logging.getLogger()
        if self._handler is not None:
            root.removeHandler(self._handler)
        if self.live is not None:
            self.live.stop()
        for h in self._swapped:
            root.addHandler(h)

    # -- queue numbers (worker thread) ----------------------------------------------

    def _refresh_queue(self, _event: str) -> None:
        store = self.store
        jobs = [
            jp
            for jp in store.job_progress(self.job_id)
            if self.kind is None or jp.kind == self.kind
        ]
        total = sum(jp.total for jp in jobs)
        remaining = sum(jp.remaining for jp in jobs)
        done = sum(jp.counts.get("done", 0) + jp.counts.get("split", 0) for jp in jobs)
        dead = sum(jp.counts.get("dead", 0) for jp in jobs)
        ids = {jp.job_id for jp in jobs}
        since = dt.datetime.fromtimestamp(time.time() - 24 * 3600, dt.UTC).isoformat(
            timespec="seconds"
        )
        recent = finishes(store.finished_since(since))
        pace = build_pace(store.pace([j for j in ids if j]), recent)
        backlog = [r for r in store.backlog() if r["job_id"] in ids]
        left = work_left(pace, backlog)
        busy = None if any(v is None for v in left.values()) else sum(v or 0 for v in left.values())
        quota = QuotaLedger(store, self.cfg.quota)
        used = quota.used_today()
        eta, bound = (None, False)
        if busy is not None:
            eta, bound = quota_bound(
                busy,
                remaining,
                budget=self.cfg.quota.daily_budget,
                used_today=used,
                reset_s=quota.seconds_until_reset(),
            )
        self._queue = {
            "total": total,
            "remaining": remaining,
            "done": done,
            "dead": dead,
            "eta": eta,
            "bound": bound,
            "at": time.time(),
            "tasks_h": rates(recent, time.time())[0],
            "used": used,
            "pace": pace,
        }

    # -- render (display thread) ------------------------------------------------------

    def _render(self) -> Group:
        th = self.theme
        now = time.time()
        t = time.monotonic() - self._t0
        width = max(self.console.width, 40)
        doc = self.board.snapshot()
        q = self._queue
        lines: list[Text] = [Text("─" * width, th.s(th.faint))]
        phase = doc.get("phase") or ""
        task = doc.get("task")
        stopping = doc.get("stopping")

        if doc.get("state") == "waiting":
            until = float(doc.get("until") or now)
            since = float(doc.get("phase_since") or now)
            left = max(until - now, 0.0)
            frac = 1 - left / max(until - since, 1.0)
            title = (
                "◔ QUOTA SPENT — sleeping until the reset" if phase == "quota" else "◔ BACKING OFF"
            )
            color = th.info if phase == "quota" else th.warn
            head = Text.assemble(
                (title, th.s(color, bold=True)), ("  ", ""), (doc.get("note") or "", th.s(th.dim))
            )
            lines.append(head)
            lab = Text(f"  {fmt.clock(left)}", th.s(th.bright, bold=True))
            lines.append(
                Text("  ") + bar(th, width - 2 - lab.cell_len, frac, (th.faint, color)) + lab
            )
        elif task:
            title = PHASE_TITLES.get(phase, phase.upper() or "WORKING").lower()
            elapsed = now - float(task.get("claimed") or now)
            head = Text.assemble(
                (f"{spinner(t)} ", th.s(th.warn if stopping else th.accent, bold=True)),
                (f"{title:<18}", th.s(th.warn if stopping else th.accent, bold=True)),
                (self.names.iso(task.get("reporter")), th.s(th.bright, bold=True)),
                (f" {task.get('period', '')}", th.s(th.bright)),
                (
                    f" · {fmt.flows_label(task.get('flows') or [])} · {task.get('cmd', '')}",
                    th.s(th.dim),
                ),
                (f"   {fmt.clock(elapsed)}", th.s(th.text)),
            )
            if stopping:
                head.append("   stopping after this task — Ctrl+C again stops now", th.s(th.warn))
            lines.append(head)
            lines.append(Text("  ") + self._task_bar(doc, task, width - 2, elapsed, t))
        else:
            label = "stopping…" if stopping else "next task…"
            lines.append(Text.assemble((f"{spinner(t)} ", th.s(th.accent)), (label, th.s(th.dim))))
            lines.append(Text(""))
        lines.append(self._queue_line(q, doc, width, now))
        for ln in lines:
            ln.no_wrap, ln.overflow = True, "ellipsis"
        return Group(*lines)

    def _task_bar(self, doc: dict, task: dict, width: int, elapsed: float, t: float) -> Text:
        th = self.theme
        xfer = doc.get("xfer") or {}
        phase = doc.get("phase")
        total, wire = xfer.get("total"), int(xfer.get("wire") or 0)
        if phase in ("receive", "download") and total:
            frac = wire / total
            span = max(float(xfer.get("at") or 0) - float(xfer.get("since") or 0), 0.0)
            rate = f" · {fmt.rate(wire / span)}" if span > 0.5 and wire else ""
            lab = Text(
                f" {frac * 100:3.0f}%  {fmt.human_bytes(wire)} of {fmt.human_bytes(total)}{rate}",
                th.s(th.dim),
            )
            return bar(th, max(width - lab.cell_len, 8), frac) + lab
        if phase in ("parse", "write", "split", "convert"):
            rows = task.get("rows")
            what = {
                "parse": "parsing the response",
                "write": f"writing {rows:,} rows" if rows else "writing parquet",
                "split": "over the record cap — splitting",
                "convert": doc.get("note") or "converting the bulk file",
            }[phase]
            lab = Text(f"  {what}", th.s(th.dim))
            return bar(th, max(width - lab.cell_len, 8), 1.0) + lab
        pace = self._queue.get("pace")
        expect = pace.expect(task.get("job_id", ""), str(task.get("reporter")))[0] if pace else None
        if expect and elapsed < expect * 0.97:
            lab = Text(
                f"  ~{elapsed / expect * 100:.0f}% est. · typical {fmt.clock(expect)}", th.s(th.dim)
            )
            return bar(th, max(width - lab.cell_len, 8), elapsed / expect) + lab
        note = (
            "waiting for the server…"
            if not expect
            else f"slower than the usual {fmt.clock(expect)}"
        )
        lab = Text(f"  {note}", th.s(th.dim))
        return scanner(th, max(width - lab.cell_len, 8), t) + lab

    def _queue_line(self, q: dict, doc: dict, width: int, now: float) -> Text:
        th = self.theme
        if not q:
            return Text("")
        total = q["total"] or 1
        settled = q["total"] - q["remaining"]
        tail = Text.assemble(
            (f" {settled:,}/{q['total']:,}", th.s(th.bright, bold=True)),
            (f" · {settled / total * 100:.0f}%", th.s(th.dim)),
        )
        if q["remaining"] and q.get("eta") is not None:
            eta = max(q["eta"] - (now - q["at"]), 0.0)
            tail.append(" · ETA ", th.s(th.dim))
            tail.append(fmt.span(eta), th.s(th.info if q["bound"] else th.accent, bold=True))
            if q["bound"]:
                tail.append(" (quota-bound)", th.s(th.info))
        if q.get("tasks_h"):
            tail.append(f" · {q['tasks_h']:.0f} tasks/h", th.s(th.dim))
        s = doc.get("session") or {}
        tail.append(
            f" · this run {s.get('done', 0):,} done, {fmt.human_count(s.get('rows', 0))} rows",
            th.s(th.dim),
        )
        budget = self.cfg.quota.daily_budget
        if budget:
            tail.append(f" · quota {q['used']}/{budget}", th.s(th.dim))
        head = Text("queue ", th.s(th.dim))
        room = max(min(width - head.cell_len - tail.cell_len, 40), 10)
        parts = [(q["done"], th.ok), (q["dead"], th.bad)]
        return head + stacked(th, room, parts, total) + tail
