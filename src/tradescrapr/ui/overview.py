"""The overview screen: header and footer bars, the NOW panel (what the worker
is doing this second), throughput, jobs, activity, attention, and up next."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

from rich.style import Style
from rich.text import Text

from .. import __version__
from . import fmt
from .canvas import Block, hjoin, lines_block, panel, text_line, vjoin
from .context import (
    MIN_H,
    MIN_W,
    WINDOWS,
    Ctx,
    cell,
    dots,
    eta_seconds,
    eta_text,
    kv,
    spread,
    task_scope,
)
from .events import Event
from .fmt import Names
from .metrics import series
from .snapshot import JobView, Problem, Upcoming
from .theme import Theme
from .widgets import (
    bar,
    columns,
    keycap,
    pipeline,
    ratio_color,
    scanner,
    sparkline,
    spinner,
    stacked,
)

# Header and footer
# ---------------------------------------------------------------------------

_BADGES = {
    "running": ("●", "running"),
    "waiting": ("◔", "waiting"),
    "stopping": ("■", "stopping"),
    "stalled": ("‖", "suspended"),
    "starting": ("◌", "starting"),
    "idle": ("○", "idle"),
}


def header(c: Ctx, width: int) -> Block:
    th = c.theme
    base = c.s(th.bar_fg, bg=th.bar_bg)
    sep = Text("  │  ", c.s(th.bar_fg, bg=th.bar_bg))
    w = c.worker
    glyph, label = _BADGES[w.state]
    if w.state in ("running", "starting"):
        glyph = spinner(c.t)
    badge_color = {
        "running": th.ok,
        "waiting": th.info,
        "stopping": th.warn,
        "stalled": th.warn,
        "starting": th.accent,
        "idle": th.bar_fg,
    }[w.state]
    left = Text(" ", base)
    left.append("▌", c.s(th.accent, bg=th.bar_bg))
    left.append("tradescrapr", c.s(th.bar_fg, bold=True, bg=th.bar_bg))
    left.append(f" {__version__}", base)
    left.append_text(sep)
    left.append(c.snap.tier, c.s(th.bar_fg, bold=True, bg=th.bar_bg))
    left.append_text(sep)
    left.append(f"{glyph} worker {label}", c.s(badge_color, bold=True, bg=th.bar_bg))
    if w.pid and w.pid > 0:
        left.append(f" · pid {w.pid}", base)
    if w.up_s is not None:
        left.append(f" · up {fmt.clock(w.up_s)}", base)
    if w.live and w.live.get("job"):
        job = c.snap.job(w.live["job"])
        left.append(f" · {job.name if job else w.live['job']} only", base)
    if w.live and w.live.get("mode") == "monitor":
        left.append(" · monitor", base)
    stamp = dt.datetime.fromtimestamp(c.now).strftime(
        "%a %d %b  %H:%M:%S " if width >= 110 else "%H:%M:%S "
    )
    right = Text(stamp, base)
    line = spread(left, right, width)
    line.stylize(base)
    return Block(width, [text_line(line, width, style=base)])


_FOOTER_KEYS = {
    "overview": (
        ("?", "help"),
        ("s", "start"),
        ("x", "stop"),
        ("⏎", "open"),
        ("l", "log"),
        ("R", "retry dead"),
        ("C", "cancel job"),
        ("t", "theme"),
        ("q", "quit"),
    ),
    "job": (
        ("esc", "back"),
        ("↑↓", "scroll"),
        ("s", "start"),
        ("x", "stop"),
        ("R", "retry dead"),
        ("C", "cancel job"),
        ("q", "quit"),
    ),
    "log": (
        ("esc", "back"),
        ("↑↓", "scroll"),
        ("f", "filter"),
        ("home", "newest"),
        ("q", "quit"),
    ),
}


def footer(c: Ctx, width: int) -> Block:
    th = c.theme
    w = c.worker
    keys = []
    for key, label in () if c.ui.static else _FOOTER_KEYS[c.ui.view]:
        if key == "s" and w.state not in ("idle",):
            continue
        if key == "x" and w.state in ("idle", "starting"):
            continue
        if key == "x" and w.state == "stopping":
            label = "stop now"
        if key == "x" and w.state == "stalled":
            keys.append(keycap(th, "c", "resume"))
        keys.append(keycap(th, key, label))
    right: Text
    toast = c.ui.toast
    if toast and toast.until > c.now:
        color = {"ok": th.ok, "warn": th.warn, "bad": th.bad}.get(toast.kind, th.accent)
        right = Text(f" {toast.text} ", c.s(color, bold=True))
    else:
        age = max(c.now - c.snap.taken, 0.0)
        right = Text.assemble(
            ("data ", c.s(th.faint)),
            (fmt.human_bytes(c.snap.data_bytes), c.s(th.dim)),
            (f" · {c.snap.data_files:,} files", c.s(th.faint)),
            ("  ↻ ", c.s(th.faint)),
            (f"{age:.1f}s ", c.s(th.faint)),
        )
    left = Text()
    for k in keys:
        if left.cell_len + k.cell_len > width - right.cell_len - 1:
            break
        left.append_text(k)
    return Block(width, [text_line(spread(left, right, width), width)])


# ---------------------------------------------------------------------------
# Transfer: what the worker is doing this second
# ---------------------------------------------------------------------------

PHASE_TITLES = {
    "prepare": "PREPARING",
    "claim": "NEXT TASK",
    "request": "WAITING ON SERVER",
    "receive": "RECEIVING",
    "parse": "PARSING",
    "write": "WRITING",
    "split": "SPLITTING",
    "list": "LISTING FILES",
    "download": "DOWNLOADING",
    "convert": "CONVERTING",
}


def _task_line(c: Ctx, task: dict[str, Any] | None, width: int, title: str, color: str) -> Text:
    th = c.theme
    head = Text.assemble(
        (f"{spinner(c.t)} ", c.s(color, bold=True)), (title, c.s(color, bold=True))
    )
    if not task:
        return head
    parts = [
        Text(c.names.iso(task.get("reporter")), c.s(th.bright, bold=True)),
        Text(str(task.get("period", "")), c.s(th.bright)),
        *task_scope(c, task.get("flows") or (), task.get("cmd", ""), task.get("partners") or "all"),
    ]
    left = head + Text("   ") + dots(c, *parts)
    meta = Text(f"#{str(task.get('task_id', ''))[:8]}", c.s(th.faint))
    attempt = task.get("attempt") or 1
    if attempt > 1:
        meta = Text(f"attempt {attempt} · ", c.s(th.warn)) + meta
    return spread(left, meta, width)


def _transfer_body(c: Ctx, width: int, rows: int) -> list[Text]:
    th, w, snap = c.theme, c.worker, c.snap
    live = w.live
    lines: list[Text] = []

    if w.state == "stalled":
        lines.append(Text("‖ SUSPENDED", c.s(th.warn, bold=True)))
        lines.append(
            Text(f"worker pid {w.pid} is stopped (Ctrl+Z) and still holds the lock.", c.s(th.text))
        )
        lines.append(Text("press c to resume it, or x to stop it.", c.s(th.dim)))
        return lines

    if live is None and w.state in ("running", "stopping"):
        # A worker without a status file (an older version, or a monitor run
        # started before this one): fall back to what the database says.
        title = "STOPPING" if w.state == "stopping" else "RUNNING"
        if not snap.flights:
            lines.append(Text(f"{spinner(c.t)} {title}", c.s(th.ok, bold=True)))
            lines.append(Text("between tasks", c.s(th.dim)))
            return lines
        f = snap.flights[0]
        task = {
            "reporter": f.reporter,
            "period": f.period,
            "flows": f.flows,
            "task_id": f.task_id,
            "attempt": f.attempts,
            "partners": "all",
        }
        lines.append(_task_line(c, task, width, title, th.accent))
        elapsed = c.now - f.claimed if f.claimed else 0.0
        lines.append(Text(""))
        lines.append(_time_bar(c, width, elapsed, f.expect_s))
        lines.append(_timing(c, elapsed, f.expect_s, f.basis, f.reporter, None))
        return lines

    if live is None:
        return _idle_body(c, width)

    phase = live.get("phase") or ""
    task = live.get("task")
    if w.state == "waiting":
        return _waiting_body(c, live, width)

    title = PHASE_TITLES.get(phase, phase.upper() or "WORKING")
    color = th.accent
    if w.state == "stopping":
        title, color = f"STOPPING · {title.lower()}", th.warn
    lines.append(_task_line(c, task, width, title, color))
    steps = (
        ("list", "download", "convert", "write")
        if task and task.get("kind") == "bulk"
        else (
            "request",
            "receive",
            "parse",
            "write",
        )
    )
    shown = {"prepare": steps[0], "claim": steps[0], "split": "parse"}.get(phase, phase)
    job = snap.job(task.get("job_id")) if task else None
    right = Text(job.name if job else "", c.s(th.dim))
    lines.append(spread(pipeline(th, steps, shown, c.t), right, width))

    xfer = live.get("xfer") or {}
    started = float(task.get("claimed") or live.get("phase_since") or c.now) if task else c.now
    elapsed = c.now - started
    exp, basis = None, ""
    if task:
        flight = next((f for f in snap.flights if f.task_id == task.get("task_id")), None)
        if flight:
            exp, basis = flight.expect_s, flight.basis
    total = xfer.get("total")
    wire = int(xfer.get("wire") or 0)
    if phase in ("receive", "download") and total:
        lines.append(_byte_bar(c, width, wire, int(total)))
    elif phase in ("parse", "write"):
        rows_n = (task or {}).get("rows")
        label = (
            "parsing response"
            if phase == "parse"
            else (f"writing {rows_n:,} rows → parquet" if rows_n else "writing parquet")
        )
        lines.append(_full_bar(c, width, label))
    elif phase == "split":
        rows_n = (task or {}).get("rows") or 0
        lines.append(_full_bar(c, width, f"hit the {rows_n:,}-row cap → splitting", th.warn))
    elif phase == "convert":
        lines.append(scanner(th, width, c.t))
    else:
        lines.append(_time_bar(c, width, elapsed, exp))
    lines.append(_timing(c, elapsed, exp, basis, (task or {}).get("reporter"), live))
    lines.append(_session(c, live))
    if w.state == "stopping":
        lines[-1] = Text(
            "finishing this task, then the worker exits — press x again to stop now",
            c.s(th.warn),
        )
    return lines[:rows]


def _byte_bar(c: Ctx, width: int, wire: int, total: int) -> Text:
    th = c.theme
    frac = wire / total if total else 0.0
    label = Text.assemble(
        (f" {frac * 100:3.0f}% ", c.s(th.bright, bold=True)),
        (f"{fmt.human_bytes(wire)} of {fmt.human_bytes(total)}", c.s(th.dim)),
    )
    return bar(th, max(width - label.cell_len, 4), frac) + label


def _full_bar(c: Ctx, width: int, label: str, color: str | None = None) -> Text:
    th = c.theme
    lab = Text(f"  {label}", c.s(color or th.dim))
    return bar(th, max(width - lab.cell_len, 4), 1.0) + lab


def _time_bar(c: Ctx, width: int, elapsed: float, expect: float | None) -> Text:
    """Progress estimated from how long similar tasks took; a sweeping scanner
    when nothing similar has finished yet."""
    th = c.theme
    if not expect:
        lab = Text("  waiting for the first response…", c.s(th.dim))
        return scanner(th, max(width - lab.cell_len, 4), c.t) + lab
    frac = elapsed / expect
    if frac <= 0.97:
        lab = Text.assemble(
            (f" {'~' + format(frac * 100, '.0f') + '%':>5} ", c.s(th.bright, bold=True)),
            ("est.", c.s(th.faint)),
        )
        return bar(th, max(width - lab.cell_len, 4), frac) + lab
    over = elapsed - expect
    lab = Text(f"  +{fmt.clock(over)} longer than usual", c.s(th.warn))
    return scanner(th, max(width - lab.cell_len, 4), c.t, th.warn) + lab


def _timing(
    c: Ctx,
    elapsed: float,
    expect: float | None,
    basis: str,
    reporter: str | None,
    live: dict[str, Any] | None,
) -> Text:
    th = c.theme
    items: list[Text] = [kv(c, "elapsed", fmt.clock(elapsed), th.bright, bold=True)]
    task = (live or {}).get("task") or {}
    xfer = (live or {}).get("xfer") or {}
    if xfer.get("since") and task.get("claimed"):
        wait = float(xfer["since"]) - float(task["claimed"])
        items.append(kv(c, "server", f"{wait:.1f}s"))
    if xfer:
        wire = int(xfer.get("wire") or 0)
        span = max(float(xfer.get("at") or c.now) - float(xfer.get("since") or c.now), 0.0)
        items.append(kv(c, "received", fmt.human_bytes(wire)))
        if span > 0.5 and wire:
            items.append(Text(fmt.rate(wire / span), c.s(th.dim)))
    if expect:
        who = {"reporter": c.names.iso(reporter), "job": "job", "recent": "recent"}.get(
            basis, basis
        )
        items.append(kv(c, f"typical {who}", fmt.clock(expect)))
        left = expect - elapsed
        if left > 1:
            items.append(kv(c, "left", f"~{fmt.clock(left)}", th.accent))
    if live and live.get("note"):
        items.append(Text(str(live["note"]), c.s(th.dim)))
    return dots(c, *items)


def _session(c: Ctx, live: dict[str, Any]) -> Text:
    th = c.theme
    s = live.get("session") or {}
    items = [kv(c, "this run", fmt.clock(c.now - float(live.get("started") or c.now)))]
    items.append(Text(f"{s.get('calls', 0):,} calls", c.s(th.dim)))
    items.append(Text(f"{s.get('done', 0):,} done", c.s(th.ok)))
    if s.get("split"):
        items.append(Text(f"{s['split']:,} split", c.s(th.ok)))
    if s.get("failed"):
        items.append(Text(f"{s['failed']:,} retried", c.s(th.warn)))
    if s.get("dead"):
        items.append(Text(f"{s['dead']:,} dead", c.s(th.bad)))
    items.append(Text(f"{fmt.human_count(s.get('rows', 0))} rows", c.s(th.text)))
    if s.get("wire"):
        items.append(Text(fmt.human_bytes(s["wire"]), c.s(th.dim)))
    return dots(c, *items)


def _waiting_body(c: Ctx, live: dict[str, Any], width: int) -> list[Text]:
    th = c.theme
    phase = live.get("phase")
    since = float(live.get("phase_since") or c.now)
    until = float(live.get("until") or c.now)
    total = max(until - since, 1.0)
    left = max(until - c.now, 0.0)
    if phase == "quota":
        title, color = "◔ QUOTA SPENT", th.info
        resume = dt.datetime.fromtimestamp(until).strftime("%H:%M")
        what = f"daily call budget used up — resumes at {resume} (in {fmt.span(left)})"
        frac = 1 - left / total
    else:
        title, color = "◔ BACKING OFF", th.warn
        what = f"retrying in {fmt.clock(left)} after a failed call"
        frac = left / total
    lab = Text(f"  {fmt.clock(left)}", c.s(th.bright, bold=True))
    lines = [
        Text(title, c.s(color, bold=True)),
        Text(what, c.s(th.text)),
        bar(th, max(width - lab.cell_len, 4), frac, (th.faint, color)) + lab,
    ]
    if live.get("note"):
        lines.append(Text(str(live["note"]), c.s(th.dim)))
    lines.append(_session(c, live))
    return lines


def _idle_body(c: Ctx, width: int) -> list[Text]:
    th, snap, w = c.theme, c.snap, c.worker
    lines: list[Text] = []
    remaining = snap.remaining
    if w.state == "starting":
        lines.append(Text(f"{spinner(c.t)} STARTING", c.s(th.accent, bold=True)))
        lines.append(Text("launching a background worker…", c.s(th.dim)))
        return lines
    lines.append(Text("○ IDLE", c.s(th.dim, bold=True)))
    if remaining:
        left = sum(j.left_s or 0 for j in snap.active_jobs)
        known = all(j.left_s is not None for j in snap.active_jobs)
        work = f" (~{fmt.span(left)} of work)" if known and left else ""
        lines.append(
            Text.assemble(
                (f"{remaining:,} task(s) queued{work}. ", c.s(th.text)),
                ("press ", c.s(th.dim)),
                (" s ", c.s(th.key_fg, bold=True, bg=th.key_bg)),
                (" to start downloading in the background", c.s(th.dim)),
            )
        )
    else:
        lines.append(Text("nothing queued — plan a job with `tradescrapr new`", c.s(th.text)))
    if snap.jobs:
        lines.append(
            dots(
                c,
                Text(f"{len(snap.jobs)} job(s)", c.s(th.dim)),
                Text(f"{sum(j.total for j in snap.jobs):,} tasks", c.s(th.dim)),
                Text(f"{fmt.human_count(sum(j.rows for j in snap.jobs))} rows", c.s(th.dim)),
                Text(f"{fmt.human_bytes(snap.data_bytes)} on disk", c.s(th.dim)),
            )
        )
    last = w.last
    if last and last.get("reason"):
        s = last.get("session") or {}
        when = dt.datetime.fromtimestamp(float(last.get("updated") or 0)).strftime("%d %b %H:%M")
        reason = {
            "drained": "queue drained",
            "signal": "stopped",
            "aborted": "stopped immediately",
            "quota": "quota spent",
            "max-calls": "--max-calls reached",
            "stop-after": "--stop-after reached",
        }.get(last["reason"], last["reason"])
        lines.append(Text(""))
        lines.append(
            dots(
                c,
                Text(f"last run ended {when}", c.s(th.dim)),
                Text(reason, c.s(th.text)),
                Text(f"{s.get('calls', 0):,} calls", c.s(th.dim)),
                Text(f"{s.get('done', 0):,} done", c.s(th.ok)),
                Text(f"{fmt.human_count(s.get('rows', 0))} rows", c.s(th.dim)),
            )
        )
    return lines


def transfer_panel(c: Ctx, width: int, height: int) -> Block:
    th, w = c.theme, c.worker
    right = None
    if w.live and w.live.get("task"):
        claimed = float(w.live["task"].get("claimed") or c.now)
        right = Text(fmt.clock(c.now - claimed), c.s(th.dim))
    color = {
        "running": th.ok,
        "waiting": th.info,
        "stopping": th.warn,
        "stalled": th.warn,
        "starting": th.accent,
    }.get(w.state)
    return panel(
        th,
        width,
        height,
        _transfer_body(c, width - 4, height - 2),
        title="NOW",
        right=right,
        title_color=color,
    )


# ---------------------------------------------------------------------------
# Throughput and quota
# ---------------------------------------------------------------------------


def throughput_panel(c: Ctx, width: int, height: int) -> Block:
    th, snap = c.theme, c.snap
    inner = width - 4
    window, wlabel = WINDOWS[c.ui.window]
    graph_h = max(height - 4, 1)
    key = ("series", snap.version, window, inner)
    if key not in c.memo:
        c.memo.clear()
        c.memo[key] = series(snap.recent, snap.taken, window, inner)
    vals = c.memo[key]
    peak = max(vals) if vals else 0.0
    body: list[Text] = []
    if peak <= 0:
        msg = Text(f"no rows fetched in the last {wlabel}", c.s(th.faint))
        body.extend([Text("")] * max(graph_h // 2, 0))
        body.append(cell(msg, inner, align="l"))
        body.extend([Text("")] * (graph_h - len(body)))
    else:
        body.extend(columns(vals, inner, graph_h, th.bar))
    if snap.tasks_h:
        rate = dots(
            c,
            Text(f"{snap.tasks_h:,.0f} tasks/h", c.s(th.text)),
            Text(f"{fmt.human_count(snap.rows_h)} rows/h", c.s(th.text)),
            Text(f"peak {fmt.human_count(peak)}/min", c.s(th.dim)),
        )
    else:
        rate = Text("no tasks finished in the last hour", c.s(th.faint))
    body.append(rate)
    body.append(_quota_line(c, inner))
    right = Text.assemble(("rows/min · ", c.s(th.faint)), (wlabel, c.s(th.accent, bold=True)))
    return panel(th, width, height, body, title="THROUGHPUT", right=right, footer="[ ] zoom")


def _quota_line(c: Ctx, width: int) -> Text:
    th, snap = c.theme, c.snap
    used, budget = snap.quota_used, snap.budget
    hist = sparkline([v for _, v in snap.quota_history]) or "▁"
    if budget:
        frac = used / budget
        label = Text.assemble(
            (f" {used}/{budget}", c.s(ratio_color(th, frac), bold=True)),
            (f" · resets {fmt.span(snap.quota_reset_s)}", c.s(th.dim)),
        )
        head = Text("quota ", c.s(th.dim))
        room = max(width - head.cell_len - label.cell_len, 4)
        return head + bar(th, room, frac, th.heat) + label
    return dots(
        c,
        kv(c, "calls today", f"{used:,}", th.bright, bold=True),
        Text.assemble(("7d ", c.s(th.dim)), (hist, c.s(th.info))),
        Text("unlimited", c.s(th.dim)),
    )


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def job_bar(c: Ctx, j: JobView, width: int) -> Text:
    th = c.theme
    parts = [
        (j.n("done") + j.n("split"), th.ok),
        (j.n("dead"), th.bad),
        (j.n("cancelled"), th.faint),
        (j.n("in_progress"), th.accent2),
        (j.n("failed"), th.warn),
    ]
    return stacked(th, width, parts, j.total)


STATE_STYLE = {
    "running": ("accent2", "running"),
    "queued": ("dim", "queued"),
    "complete": ("ok", "✔ complete"),
    "attention": ("bad", "✘ needs fix"),
    "cancelled": ("faint", "cancelled"),
}

# (key, header, width, align, priority) — lower priority columns drop first
_JOB_COLS = (
    ("pct", "", 5, "r", 9),
    ("done", "done", 7, "r", 8),
    ("queue", "queue", 6, "r", 7),
    ("retry", "retry", 5, "r", 2),
    ("dead", "dead", 5, "r", 4),
    ("rows", "rows", 7, "r", 3),
    ("size", "size", 9, "r", 1),
    ("left", "left", 8, "r", 6),
    ("state", "state", 11, "l", 5),
)


def jobs_panel(c: Ctx, width: int, height: int) -> Block:
    th, snap, ui = c.theme, c.snap, c.ui
    inner = width - 4
    jobs = order_jobs(snap.jobs)
    body: list[Text] = []
    active = snap.active_jobs
    # pick columns that fit: name >= 14, bar >= 12
    cols = list(_JOB_COLS)
    while cols and inner - 2 - 14 - 12 - sum(w + 1 for _, _, w, _, _ in cols) < 0:
        cols.remove(min(cols, key=lambda col: col[4]))
    fixed = sum(w + 1 for _, _, w, _, _ in cols)
    flex = inner - 2 - fixed
    name_w = max(14, min(34, flex * 2 // 5))
    bar_w = max(flex - name_w - 1, 6)

    head = (
        Text("  ")
        + cell("job", name_w, c.s(th.faint))
        + Text(" ")
        + cell("progress", bar_w, c.s(th.faint))
    )
    for _, title, w, align, _ in cols:
        head += Text(" ") + cell(title, w, c.s(th.faint), align)
    body.append(head)

    visible = height - 2 - len(body)
    if not jobs:
        body.append(Text("  no jobs yet — plan one with `tradescrapr new`", c.s(th.dim)))
    if ui.selected not in {j.job_id for j in jobs} and jobs:
        ui.selected = jobs[0].job_id
    idx = next((i for i, j in enumerate(jobs) if j.job_id == ui.selected), 0)
    if idx < ui.job_scroll:
        ui.job_scroll = idx
    elif idx >= ui.job_scroll + visible:
        ui.job_scroll = idx - visible + 1
    ui.job_scroll = max(0, min(ui.job_scroll, max(len(jobs) - visible, 0)))

    for j in jobs[ui.job_scroll : ui.job_scroll + visible]:
        sel = j.job_id == ui.selected
        glyph = "⟳" if j.kind == "monitor" else "▶"
        name_style = c.s(th.bright, bold=True) if j.remaining else c.s(th.dim)
        name = Text.assemble(
            (f"{glyph} ", c.s(th.info if j.kind == "monitor" else th.accent)), (j.name, name_style)
        )
        row = Text("▸ " if sel else "  ", c.s(th.accent, bold=True))
        row += cell(name, name_w) + Text(" ") + job_bar(c, j, bar_w)
        state = j.state
        if state == "running" and not c.worker.pid:
            state = "queued"  # an in_progress row left by a worker that is gone
        for key, _title, w, align, _ in cols:
            row += Text(" ")
            if key == "pct":
                row += cell(
                    f"{j.frac * 100:.0f}%", w, c.s(th.bright if j.remaining else th.dim), align
                )
            elif key == "done":
                row += cell(f"{j.n('done'):,}", w, c.s(th.text), align)
            elif key == "queue":
                q = j.n("pending") + j.n("in_progress")
                row += cell(f"{q:,}" if q else "·", w, c.s(th.text if q else th.faint), align)
            elif key == "retry":
                n = j.n("failed")
                row += cell(f"{n:,}" if n else "·", w, c.s(th.warn if n else th.faint), align)
            elif key == "dead":
                n = j.n("dead")
                row += cell(f"{n:,}" if n else "·", w, c.s(th.bad if n else th.faint), align)
            elif key == "rows":
                row += cell(fmt.human_count(j.rows), w, c.s(th.text), align)
            elif key == "size":
                row += cell(fmt.human_bytes(j.size) if j.size else "·", w, c.s(th.dim), align)
            elif key == "left":
                txt, bound = eta_text(c, j.left_s, j.remaining)
                color = th.info if bound else (th.accent if j.remaining else th.faint)
                row += cell(txt, w, c.s(color), align)
            elif key == "state":
                color_key, label = STATE_STYLE[state]
                if state == "running":
                    label = f"{spinner(c.t)} running"
                row += cell(label, w, c.s(getattr(th, color_key)), align)
        if sel:
            row = cell(row, inner)
            row.stylize(Style(bgcolor=th.select_bg))
        body.append(row)

    total = sum(j.total for j in snap.jobs)
    settled = sum(j.settled for j in snap.jobs)
    if active:
        left_all = (
            None if any(j.left_s is None for j in active) else sum(j.left_s or 0 for j in active)
        )
        remaining = sum(j.remaining for j in active)
        eta, bound = eta_text(c, left_all, remaining)
        rest = Text.assemble(
            (f"{settled:,}/{total:,} tasks", c.s(th.text)),
            (f" · {settled / total * 100 if total else 0:.1f}%", c.s(th.dim)),
            (" · ETA " if c.worker.state != "idle" else " · work left ", c.s(th.dim)),
            (eta, c.s(th.info if bound else th.accent, bold=True)),
        )
        secs, _ = eta_seconds(c, left_all, remaining)
        if c.worker.state in ("running", "waiting") and secs:
            done_at = dt.datetime.fromtimestamp(c.now + secs)
            rest.append(
                f" → {done_at.strftime('%a %H:%M' if secs > 20 * 3600 else '%H:%M')}", c.s(th.dim)
            )
        if bound:
            rest.append(" (quota-bound)", c.s(th.info))
    else:
        rest = Text(f"{total:,} tasks · all settled", c.s(th.dim))
    more = len(jobs) - visible
    foot = (
        Text(
            f"{ui.job_scroll + 1}-{min(ui.job_scroll + visible, len(jobs))} of {len(jobs)}",
            c.s(th.dim),
        )
        if more > 0
        else None
    )
    return panel(
        th,
        width,
        height,
        body,
        title=f"JOBS ({len(jobs)})",
        right=rest,
        footer=foot,
        focus=c.ui.view == "overview",
    )


def order_jobs(jobs: Sequence[JobView]) -> list[JobView]:
    """Unfinished work first, then finished jobs; newest first within each."""
    newest = sorted(jobs, key=lambda j: j.created_at, reverse=True)
    return [j for j in newest if j.remaining] + [j for j in newest if not j.remaining]


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------

_EVENT_GLYPHS = {
    "done": ("✔", "ok"),
    "split": ("⑃", "warn"),
    "retry": ("↻", "warn"),
    "dead": ("✘", "bad"),
    "error": ("✘", "bad"),
    "warn": ("!", "warn"),
    "backoff": ("…", "warn"),
    "quota": ("◔", "info"),
    "start": ("▶", "accent"),
    "stop": ("■", "accent"),
    "monitor": ("⟳", "info"),
    "info": ("·", "dim"),
}

_SYSTEM = ("start", "stop", "info", "monitor", "quota", "backoff", "warn", "error")


def event_line(
    th: Theme, names: Names, ev: Event, lookup: dict[str, Problem] | None = None
) -> Text:
    """One activity row; shared with the inline display of a foreground run."""
    lookup = lookup or {}
    glyph, color_key = _EVENT_GLYPHS.get(ev.kind, ("·", "dim"))
    color = getattr(th, color_key)
    out = Text.assemble((f"{ev.when} ", th.s(th.faint)), (f"{glyph} ", th.s(color, bold=True)))
    reporter, period, flows = ev.reporter, ev.period, ev.flows
    if not reporter and ev.task_id in lookup:
        p = lookup[ev.task_id]
        reporter, period, flows = p.reporter, p.period, p.flows
    if reporter:
        out += cell(names.iso(reporter), 4, th.s(th.bright, bold=True)) + Text(" ")
        out += cell(period, 6, th.s(th.text)) + Text(" ")
        out += cell("+".join(flows) or "·", 4, th.s(th.dim)) + Text(" ")
    if ev.kind == "done":
        scope = []
        if ev.cmd and ev.cmd not in ("AG6", "bulk"):
            scope.append(ev.cmd)
        if ev.partners and ev.partners != "all":
            scope.append(ev.partners.replace(" partners", "p"))
        out += cell(" ".join(scope), 9, th.s(th.faint)) + Text(" ")
        out += cell(f"{ev.rows:,} rows" if ev.rows is not None else "", 13, th.s(th.text), "r")
        if ev.wire:
            out += Text(" ") + cell(fmt.human_bytes(ev.wire), 9, th.s(th.dim), "r")
        if ev.secs is not None:
            out += Text(" ") + cell(fmt.clock(ev.secs), 6, th.s(th.dim), "r")
        return out
    if ev.kind == "split":
        cap = ev.extra.get("cap", "")
        cap_t = fmt.human_count(int(cap)) if cap.isdigit() else cap
        out.append(f"over the {cap_t}-row cap → {ev.parts} parts", th.s(color))
        return out
    if ev.task_id and not reporter:
        out.append(f"#{ev.task_id[:8]} ", th.s(th.faint))
    out.append(ev.text, th.s(color if ev.kind not in ("info",) else th.dim))
    return out


def filter_events(events: Sequence[Event], which: str) -> list[Event]:
    if which == "done":
        return [e for e in events if e.kind in ("done", "split")]
    if which == "problems":
        return [e for e in events if e.problem]
    if which == "system":
        return [e for e in events if e.kind in _SYSTEM]
    return list(events)


def event_rows(c: Ctx, events: Sequence[Event], width: int, start: int, count: int) -> list[Text]:
    """Newest first, with a rule wherever the day changes."""
    lookup = {p.task_id: p for p in c.snap.problems}
    today = dt.datetime.fromtimestamp(c.now).strftime("%Y-%m-%d")
    rows: list[Text] = []
    newest = list(reversed(events))
    prev_day = today
    skipped = 0
    for ev in newest:
        if ev.day and ev.day != prev_day:
            if skipped >= start:
                label = f" {ev.day} "
                rows.append(
                    Text("─" * 2, c.s(c.theme.faint))
                    + Text(label, c.s(c.theme.dim))
                    + Text("─" * max(width - len(label) - 2, 0), c.s(c.theme.faint))
                )
            prev_day = ev.day
        if skipped < start:
            skipped += 1
            continue
        rows.append(event_line(c.theme, c.names, ev, lookup))
        if len(rows) >= count:
            break
    return rows[:count]


def activity_panel(c: Ctx, width: int, height: int) -> Block:
    th = c.theme
    events = c.snap.events
    body = event_rows(c, events, width - 4, 0, height - 2)
    if not body:
        body = [Text("no activity yet — events appear here as the worker runs", c.s(th.faint))]
    return panel(
        th,
        width,
        height,
        body,
        title="ACTIVITY",
        right=Text("newest first", c.s(th.faint)),
        footer="l full log",
    )


# ---------------------------------------------------------------------------
# Attention: problems, health, monitor
# ---------------------------------------------------------------------------


def attention_panel(c: Ctx, width: int, height: int) -> Block:
    th, snap, ui = c.theme, c.snap, c.ui
    inner = width - 4
    body: list[Text] = []
    if ui.spawn_error:
        body.append(Text(f"✘ {ui.spawn_error}", c.s(th.bad, bold=True)))
    if snap.error:
        body.append(Text(f"! can't read state: {snap.error}", c.s(th.bad)))
    if c.worker.state == "stalled":
        body.append(Text("‖ worker suspended — c resumes it", c.s(th.warn, bold=True)))
    dead = sum(j.n("dead") for j in snap.jobs)
    retry = sum(j.n("failed") for j in snap.jobs)
    if snap.budget and snap.quota_used >= snap.budget:
        body.append(
            Text(f"◔ daily quota spent — resets in {fmt.span(snap.quota_reset_s)}", c.s(th.info))
        )
    if dead:
        body.append(
            Text.assemble(
                (f"✘ {dead:,} dead task(s)", c.s(th.bad, bold=True)),
                ("  R retries them", c.s(th.dim)),
            )
        )
    if retry:
        body.append(Text(f"↻ {retry:,} task(s) waiting to retry", c.s(th.warn)))
    monitor_rows = 2 + min(len(snap.release_periods), 6) if snap.releases else 0
    room = (
        height - 2 - len(body) - (monitor_rows if height - 2 - len(body) > monitor_rows + 2 else 0)
    )
    for p in snap.problems[: max(room, 0)]:
        body.append(problem_line(c, p, inner))
    if not body:
        body.append(Text("✔ all clear", c.s(th.ok, bold=True)))
        body.append(Text("no failed or dead tasks", c.s(th.dim)))
    if snap.releases and len(body) < height - 4:
        body.append(Text(""))
        body.extend(_monitor_lines(c, inner, height - 2 - len(body)))
    title = "ATTENTION" if (dead or retry or ui.spawn_error or snap.error) else "HEALTH"
    return panel(th, width, height, body, title=title)


def attention_rows(c: Ctx) -> int:
    """Body lines the attention panel wants (it shrinks to make room below)."""
    snap, ui = c.snap, c.ui
    n = sum(bool(x) for x in (ui.spawn_error, snap.error, c.worker.state == "stalled"))
    dead = sum(j.n("dead") for j in snap.jobs)
    retry = sum(j.n("failed") for j in snap.jobs)
    n += bool(dead) + bool(retry) + bool(snap.budget and snap.quota_used >= snap.budget)
    n += min(len(snap.problems), 6)
    if snap.releases:
        n += 2 + min(len(snap.release_periods), 6)
    return max(n, 2)


def upnext_panel(c: Ctx, width: int, height: int) -> Block:
    """The queue head in claim order, with rough start times from the pace."""
    th, snap, w = c.theme, c.snap, c.worker
    only = (w.live or {}).get("job")
    if (w.live or {}).get("mode") == "monitor":
        mon = {j.job_id for j in snap.jobs if j.kind == "monitor"}
        items = [u for u in snap.upcoming if u.job_id in mon]
    else:
        items = [u for u in snap.upcoming if not only or u.job_id == only]
    body: list[Text] = []
    if not items:
        body.append(Text("queue empty", c.s(th.faint)))
    # time until the in-flight task is done, then accumulate
    clock_s: float | None = 0.0
    f = snap.flights[0] if snap.flights else None
    if f and f.expect_s and f.claimed:
        clock_s = max(f.expect_s - (c.now - f.claimed), 0.0)
    running = w.state in ("running", "waiting", "stopping")
    for i, u in enumerate(items[: height - 2]):
        when = None if not running else (0.0 if i == 0 else clock_s)
        body.append(_upcoming_line(c, u, width - 4, when))
        if clock_s is not None:
            clock_s = clock_s + u.expect_s if u.expect_s else None
    queued = snap.remaining - len(snap.flights)
    right = Text(f"{max(queued, 0):,} queued", c.s(th.dim))
    return panel(th, width, height, body, title="UP NEXT", right=right)


def _upcoming_line(c: Ctx, u: Upcoming, width: int, starts_in: float | None) -> Text:
    th = c.theme
    job = c.snap.job(u.job_id)
    left = Text.assemble(
        ("↻ " if u.attempts else "· ", c.s(th.warn if u.attempts else th.faint)),
        (f"{c.names.iso(u.reporter):<4}", c.s(th.text, bold=True)),
        (f" {u.period:<6} ", c.s(th.text)),
        (f"{'+'.join(u.flows) or '·':<4} ", c.s(th.dim)),
        (job.name if job else u.job_id, c.s(th.faint)),
    )
    if starts_in is not None:
        when = "next" if starts_in <= 0 else f"in ~{fmt.span(max(starts_in, 1))}"
        right = Text(when, c.s(th.dim))
    elif u.expect_s:
        right = Text(f"~{fmt.clock(u.expect_s)}", c.s(th.faint))
    else:
        right = Text("")
    return spread(left, right, width)


def problem_line(c: Ctx, p: Problem, width: int) -> Text:
    th = c.theme
    color = th.bad if p.status == "dead" else th.warn
    glyph = "✘" if p.status == "dead" else "↻"
    out = Text.assemble(
        (f"{glyph} ", c.s(color, bold=True)),
        (c.names.iso(p.reporter), c.s(th.bright, bold=True)),
        (f" {p.period} ", c.s(th.text)),
        ("+".join(p.flows) + " " if p.flows else "", c.s(th.dim)),
        (f"×{p.attempts} " if p.attempts > 1 else "", c.s(th.faint)),
        (p.error or "(no message)", c.s(th.dim)),
    )
    return out


def _monitor_lines(c: Ctx, width: int, rows: int) -> list[Text]:
    th, snap = c.theme, c.snap
    done, pending = snap.releases.get("done", 0), snap.releases.get("pending", 0)
    out = [
        Text.assemble(
            ("MONITOR ", c.s(th.info, bold=True)),
            (f"{done:,} releases done", c.s(th.ok if done else th.dim)),
            (" · ", c.s(th.faint)),
            (f"{pending:,} pending", c.s(th.warn if pending else th.dim)),
        )
    ]
    for period, d, p in snap.release_periods[: max(rows - 1, 0)]:
        tot = d + p
        label = Text(f"{period:<8}", c.s(th.text))
        nums = Text(f" {d:>4}/{tot:<4}", c.s(th.dim))
        out.append(
            label
            + stacked(th, max(width - label.cell_len - nums.cell_len, 4), [(d, th.ok)], tot)
            + nums
        )
    return out


# ---------------------------------------------------------------------------
# Overview layout
# ---------------------------------------------------------------------------


def too_small(c: Ctx, width: int, height: int) -> Block:
    msg = [
        Text("terminal too small", c.s(c.theme.warn, bold=True)),
        Text(f"need {MIN_W}x{MIN_H}, have {width}x{height}", c.s(c.theme.dim)),
        Text("resize, or q to quit", c.s(c.theme.dim)),
    ]
    top = max((height - len(msg)) // 2, 0)
    return lines_block(width, height, [Text("")] * top + [cell(m, width) for m in msg])


def overview(c: Ctx, width: int, height: int) -> Block:
    body_h = height - 2
    wide = width >= 110
    side_w = max(38, min(56, int(width * 0.34))) if wide else 0
    main_w = width - side_w
    top_h = 7 if body_h >= 24 else 5
    n_jobs = max(len(c.snap.jobs), 1)
    min_bottom = 6 if body_h >= 24 else 0
    jobs_h = max(min(n_jobs + 3, body_h - top_h - min_bottom), 4)
    bottom_h = body_h - top_h - jobs_h

    if wide:
        top = hjoin(transfer_panel(c, main_w, top_h), throughput_panel(c, side_w, top_h))
    else:
        top = transfer_panel(c, width, top_h)
    blocks = [top, jobs_panel(c, width, jobs_h)]
    if bottom_h >= 3:
        if wide:
            att_h = min(attention_rows(c) + 2, bottom_h)
            side = attention_panel(c, side_w, att_h)
            if bottom_h - att_h >= 4:
                side = vjoin(side, upnext_panel(c, side_w, bottom_h - att_h))
            else:
                side = attention_panel(c, side_w, bottom_h)
            blocks.append(hjoin(activity_panel(c, main_w, bottom_h), side))
        else:
            problems = bool(c.snap.problems or c.ui.spawn_error or c.snap.error)
            att_h = min(6, bottom_h // 2) if problems and bottom_h >= 8 else 0
            if att_h:
                blocks.append(attention_panel(c, width, att_h))
            blocks.append(activity_panel(c, width, bottom_h - att_h))
    return vjoin(*blocks)
