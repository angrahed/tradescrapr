"""The other screens — job detail with its coverage map, the activity log,
help and confirm dialogs — and `screen`, which picks one and lays out the
whole frame."""

from __future__ import annotations

import textwrap
from collections.abc import Sequence

from rich.style import Style
from rich.text import Text

from . import fmt
from .canvas import Block, blank, hjoin, overlay, panel, restyle, vjoin
from .context import (
    LOG_FILTERS,
    MIN_H,
    MIN_W,
    Ctx,
    cell,
    dots,
    eta_text,
)
from .overview import (
    STATE_STYLE,
    event_rows,
    filter_events,
    footer,
    header,
    job_bar,
    overview,
    problem_line,
    too_small,
)
from .snapshot import Detail, JobView

# ---------------------------------------------------------------------------
# Job detail
# ---------------------------------------------------------------------------

_SPEC_LABELS = {"C": "goods", "S": "services", "A": "annual", "M": "monthly"}


def _spec_summary(c: Ctx, j: JobView) -> list[Text]:
    th = c.theme
    s = j.spec
    if j.kind == "monitor" or not s:
        return [Text("monitor job — new and revised monthly releases", c.s(th.dim))]
    commodities = s.get("commodities") or {}
    if commodities.get("codes"):
        cmd = f"{len(commodities['codes'])} codes"
    elif commodities.get("level") is not None:
        cmd = f"{s.get('classification', 'HS')} level {commodities['level']}"
    else:
        cmd = "TOTAL"
    reporters = s.get("reporters")
    rep = "all reporters" if reporters == "all" else f"{len(reporters or [])} reporters"
    partners = s.get("partners", "all")
    par = "all partners" if partners in ("all", None) else f"{len(partners)} partners"
    periods = s.get("periods")
    per = ", ".join(map(str, periods)) if isinstance(periods, list) else str(periods)
    line1 = dots(
        c,
        (s.get("dataset", "final"), th.text),
        (_SPEC_LABELS.get(s.get("type", "C"), "goods"), th.text),
        (_SPEC_LABELS.get(s.get("freq", "A"), "annual"), th.text),
        (cmd, th.text),
        ("flows " + "+".join(s.get("flows") or []), th.text),
        (f"{s.get('breakdown', 'classic')} breakdown", th.dim),
    )
    line2 = dots(c, (rep, th.text), (par, th.text), (f"periods {per}", th.text))
    return [line1, line2]


def detail_info(c: Ctx, j: JobView, width: int, height: int) -> Block:
    th = c.theme
    inner = width - 4
    body = _spec_summary(c, j)
    created = j.created_at[:16].replace("T", " ")
    body.append(
        dots(
            c,
            (f"id {j.job_id}", th.faint),
            (f"created {created} UTC", th.faint),
            (j.kind, th.faint),
        )
    )
    body.append(Text(""))
    pct = Text(f" {j.frac * 100:5.1f}%", c.s(th.bright, bold=True))
    body.append(job_bar(c, j, inner - pct.cell_len) + pct)
    legend = Text()
    for label, n, color in (
        ("done", j.n("done"), th.ok),
        ("split", j.n("split"), th.ok),
        ("running", j.n("in_progress"), th.accent2),
        ("retrying", j.n("failed"), th.warn),
        ("queued", j.n("pending"), th.faint),
        ("dead", j.n("dead"), th.bad),
        ("cancelled", j.n("cancelled"), th.faint),
    ):
        legend.append("■ ", c.s(color))
        legend.append(f"{label} ", c.s(th.dim))
        legend.append(f"{n:,}   ", c.s(th.bright if n else th.faint, bold=bool(n)))
    body.append(legend)
    eta, bound = eta_text(c, j.left_s, j.remaining)
    body.append(
        dots(
            c,
            Text(f"{j.rows:,} rows", c.s(th.text)),
            Text(fmt.human_bytes(j.size), c.s(th.text)),
            Text(f"avg {fmt.clock(j.pace_s)}/task" if j.pace_s else "no pace yet", c.s(th.dim)),
            Text(f"~{eta} left" + (" (quota-bound)" if bound else ""), c.s(th.accent))
            if j.remaining
            else Text("finished", c.s(th.ok)),
        )
    )
    state_color, state_label = STATE_STYLE[j.state]
    right = Text(state_label, c.s(getattr(th, state_color), bold=True))
    return panel(th, width, height, body, title=f"JOB {j.name}", right=right, focus=True)


_CELL = {
    "running": ("◆", "accent2"),
    "dead": ("✕", "bad"),
    "retry": ("↻", "warn"),
    "done": ("■", "ok"),
    "partial": ("▪", "ok"),
    "queued": ("·", "dim"),
    "cancelled": ("-", "faint"),
}


def cell_state(counts: dict[str, int]) -> str:
    if counts.get("in_progress"):
        return "running"
    if counts.get("dead"):
        return "dead"
    if counts.get("failed"):
        return "retry"
    finished = counts.get("done", 0) + counts.get("split", 0)
    pending = counts.get("pending", 0)
    if finished and not pending:
        return "done"
    if finished:
        return "partial"
    if pending:
        return "queued"
    return "cancelled"


def period_axis(periods: Sequence[str], cw: int) -> str:
    """Column labels for the coverage map: two-digit years (every one when
    cells are wide, every fifth when not), or the year at each January."""
    n = len(periods) * cw
    buf = [" "] * n
    end = -2
    annual = all(len(p) == 4 for p in periods)
    for i, p in enumerate(periods):
        if annual:
            if cw < 3 and i and p[-1] not in "05":
                continue
            lab = p[2:]
        else:
            if i and not p.endswith("01"):
                continue
            lab = p[:4]
        pos = i * cw
        if pos <= end + 1 or pos + len(lab) > n:  # overlaps, or runs off the end
            continue
        buf[pos : pos + len(lab)] = lab
        end = pos + len(lab) - 1
    return "".join(buf)


def coverage_panel(c: Ctx, d: Detail | None, width: int, height: int) -> Block:
    th, ui = c.theme, c.ui
    inner = width - 4
    if d is None or not d.reporters:
        return panel(
            th,
            width,
            height,
            [Text("loading…" if d is None else "no tasks", c.s(th.dim))],
            title="COVERAGE",
        )
    periods = d.periods
    label_w = 5
    stats_w = 22
    avail = max(inner - label_w - stats_w - 2, 4)
    cw = 3 if len(periods) * 3 <= avail else 2 if len(periods) * 2 <= avail else 1
    hidden = max(len(periods) - max(avail // cw, 1), 0)
    shown = periods[hidden:]

    head = Text(" " * (label_w - 1) + ("‹ " if hidden else "  "), c.s(th.faint))
    head.append(period_axis(shown, cw), c.s(th.dim))
    stats_at = min(label_w + 2 + len(shown) * cw + 2, inner - stats_w)  # stats hug the cells
    head.append(" " * max(stats_at - head.cell_len, 1))
    head.append(cell("tasks", 8, c.s(th.faint), "r") + cell("rows", 13, c.s(th.faint), "r"))

    rows_avail = height - 3
    reporters = sorted(d.reporters, key=c.names.iso)  # alphabetical by ISO3
    n = len(reporters)
    ui.detail_scroll = max(0, min(ui.detail_scroll, max(n - rows_avail, 0)))
    body = [head]
    pulse = int(c.t * 2) % 2 == 0
    for rep in reporters[ui.detail_scroll : ui.detail_scroll + rows_avail]:
        row = cell(c.names.iso(rep), label_w, c.s(th.bright, bold=True)) + Text(" ")
        done_n, tot_n = d.tasks_by_reporter.get(rep, (0, 0))
        for p in shown:
            counts = d.cells.get((rep, p))
            if counts:
                st = cell_state(counts)
                glyph, color_key = _CELL[st]
                color = getattr(th, color_key)
                if st == "running" and pulse:
                    color = th.bright
                row.append(glyph.ljust(cw), c.s(color, bold=st in ("running", "dead")))
            else:
                row.append(" " * cw)
        row.append(" " * max(stats_at - row.cell_len, 1))
        row += cell(f"{done_n}/{tot_n}", 8, c.s(th.text if done_n < tot_n else th.dim), "r")
        row += cell(f"{d.rows_by_reporter.get(rep, 0):,}", 13, c.s(th.dim), "r")
        body.append(row)

    legend = Text()
    for st in ("done", "partial", "running", "queued", "retry", "dead"):
        glyph, color_key = _CELL[st]
        legend.append(f"{glyph} ", c.s(getattr(th, color_key)))
        legend.append(f"{st}  ", c.s(th.faint))
    right = Text(
        f"{ui.detail_scroll + 1}-{min(ui.detail_scroll + rows_avail, n)} of {n} reporters",
        c.s(th.dim),
    )
    return panel(
        th, width, height, body, title="COVERAGE  reporter × period", right=right, footer=legend
    )


def problems_panel(c: Ctx, d: Detail | None, width: int, height: int) -> Block:
    th = c.theme
    probs = d.problems if d else []
    dead = sum(1 for p in probs if p.status == "dead")
    retry = len(probs) - dead
    body: list[Text] = []
    if not probs:
        body = [Text("✔ no failed or dead tasks", c.s(th.ok))]
    for p in probs[: height - 2]:
        body.append(problem_line(c, p, width - 4))
    right = Text.assemble(
        (f"{dead} dead", c.s(th.bad if dead else th.faint)),
        (" · ", c.s(th.faint)),
        (f"{retry} retrying", c.s(th.warn if retry else th.faint)),
    )
    return panel(
        th,
        width,
        height,
        body,
        title="PROBLEMS",
        right=right,
        footer="R retry dead" if dead else None,
    )


def job_detail(c: Ctx, width: int, height: int) -> Block:
    j = c.snap.job(c.ui.selected)
    body_h = height - 2
    if j is None:
        return panel(c.theme, width, body_h, [Text("job not found", c.s(c.theme.dim))], title="JOB")
    info_h = min(9, body_h)
    rest = body_h - info_h
    d = c.snap.detail if c.snap.detail and c.snap.detail.job_id == j.job_id else None
    info = detail_info(c, j, width, info_h)
    if rest < 4:
        return info if rest <= 0 else vjoin(info, blank(width, rest))
    if width >= 110:
        side = max(40, min(64, int(width * 0.38)))
        lower = hjoin(coverage_panel(c, d, width - side, rest), problems_panel(c, d, side, rest))
    else:
        prob_h = min(8, rest // 3) if d and d.problems else 0
        prob_h = prob_h if prob_h >= 3 else 0
        parts = [coverage_panel(c, d, width, rest - prob_h)]
        if prob_h:
            parts.append(problems_panel(c, d, width, prob_h))
        lower = vjoin(*parts)
    return vjoin(info, lower)


# ---------------------------------------------------------------------------
# Log view
# ---------------------------------------------------------------------------


def log_view(c: Ctx, width: int, height: int) -> Block:
    th, ui = c.theme, c.ui
    body_h = height - 2
    events = filter_events(c.snap.events, ui.log_filter)
    rows = body_h - 2
    ui.log_scroll = max(0, min(ui.log_scroll, max(len(events) - rows, 0)))
    body = event_rows(c, events, width - 4, ui.log_scroll, rows)
    if not body:
        body = [Text("nothing here yet", c.s(th.faint))]
    tabs = Text()
    for f in LOG_FILTERS:
        if f == ui.log_filter:
            tabs.append(f" {f} ", c.s(th.key_fg, bold=True, bg=th.key_bg))
        else:
            tabs.append(f" {f} ", c.s(th.dim))
    pos = Text(
        f"{ui.log_scroll + 1}-{min(ui.log_scroll + rows, len(events))} of {len(events):,}",
        c.s(th.dim),
    )
    return panel(th, width, body_h, body, title="ACTIVITY LOG", right=tabs, footer=pos, focus=True)


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------

_HELP = (
    ("overview", ""),
    ("↑ ↓  j k", "select a job"),
    ("⏎", "open the job: coverage map and problems"),
    ("s / S", "start a background worker (S: selected job only)"),
    ("x", "stop after the current task · again: stop now"),
    ("c", "resume a suspended (Ctrl+Z) worker"),
    ("R", "retry dead tasks (selected job, else every job)"),
    ("C", "cancel the selected job's remaining tasks"),
    ("l", "full activity log"),
    ("[ ]", "throughput window: 15m 1h 6h 24h"),
    ("t", "theme: classic · phosphor · amber"),
    ("r", "refresh now"),
    ("q  esc", "quit — a background worker keeps running"),
    ("", ""),
    ("glyphs", ""),
    ("■ ▪ ◆ ·", "done · partly done · running · queued"),
    ("↻ ✘ ⑃", "retrying · dead · split at the record cap"),
)


def help_dialog(c: Ctx, width: int) -> Block:
    th = c.theme
    w = min(width - 4, 70)
    body: list[Text] = []
    for key, text in _HELP:
        if key and not text:
            body.append(Text(key.upper(), c.s(th.accent, bold=True)))
        elif not key:
            body.append(Text(""))
        else:
            body.append(cell(key, 12, c.s(th.bright, bold=True)) + Text(text, c.s(th.text)))
    body.append(Text(""))
    note = "the worker runs in its own session: quitting this screen leaves it running"
    body.extend(Text(ln, c.s(th.faint)) for ln in textwrap.wrap(note, w - 4))
    return panel(
        th,
        w,
        len(body) + 2,
        body,
        title="HELP",
        right=Text("any key closes", c.s(th.dim)),
        focus=True,
    )


def confirm_dialog(c: Ctx, width: int) -> Block:
    th = c.theme
    conf = c.ui.confirm
    assert conf is not None
    w = min(width - 4, 66)
    body = [
        Text(ln, c.s(th.text)) for line in conf.lines for ln in textwrap.wrap(line, w - 4) or [""]
    ]
    body.append(Text(""))
    body.append(
        Text.assemble(
            (" y ", c.s(th.key_fg, bold=True, bg=th.bad if conf.danger else th.key_bg)),
            (" yes    ", c.s(th.dim)),
            (" n ", c.s(th.key_fg, bold=True, bg=th.key_bg)),
            (" no", c.s(th.dim)),
        )
    )
    return panel(th, w, len(body) + 2, body, title=conf.title, focus=True)


# ---------------------------------------------------------------------------
# Whole screen
# ---------------------------------------------------------------------------


def screen(c: Ctx, width: int, height: int) -> Block:
    if width < MIN_W or height < MIN_H:
        out = too_small(c, width, height)
    else:
        if c.ui.view == "job":
            body = job_detail(c, width, height)
        elif c.ui.view == "log":
            body = log_view(c, width, height)
        else:
            body = overview(c, width, height)
        out = vjoin(header(c, width), body, footer(c, width))
        dialog = None
        if c.ui.help:
            dialog = help_dialog(c, width)
        elif c.ui.confirm:
            dialog = confirm_dialog(c, width)
        if dialog is not None:
            out = restyle(out, post=Style(dim=True))
            x = max((width - dialog.width) // 2, 0)
            y = max((height - dialog.height) // 2, 0)
            out = overlay(out, dialog, x, y)
    if c.theme.bg:
        out = restyle(out, style=Style(color=c.theme.text, bgcolor=c.theme.bg))
    return out
