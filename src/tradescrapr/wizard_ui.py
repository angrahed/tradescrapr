"""Interactive job builder: `tradescrapr new` (also what bare `tradescrapr` runs).

A full-screen rich form in the dashboard's visual language and colour theme:
rounded panels, quota gauge header, single-line key-hint footer. The form
state lives in wizard.py; this module decodes raw keys, renders the state, and
orchestrates save -> plan -> (optionally) start a background worker and open
the dashboard to watch it.
"""

from __future__ import annotations

import os
import select
import sys
import termios
import tty
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from rich import box
from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.style import Style
from rich.styled import Styled
from rich.table import Table
from rich.text import Text

from . import __version__
from .client import ComtradeClient
from .config import Config
from .errors import ConfigError, TradescraprError
from .jobs.targeted import PlanResult, plan_spec
from .quota import QuotaLedger
from .refs import Refs
from .state import Store
from .ui.fmt import span
from .ui.theme import CLASSIC, Theme, load_theme
from .wizard import (
    FIELDS,
    Action,
    Estimate,
    FieldDef,
    FormState,
    Mode,
    PickerState,
    commodity_items,
    dump_spec_yaml,
    estimate,
    flow_items,
    partner_items,
    partner_label,
    reporter_items,
)

MIN_WIDTH, MIN_HEIGHT = 80, 22

# The active colour theme (set by cmd_new from the dashboard's saved choice).
P: Theme = CLASSIC

_TYPE_LABELS = {"C": "goods", "S": "services"}
_FREQ_LABELS = {"A": "annual", "M": "monthly"}

_HELP = {
    "name": "Slug for jobs/<name>.yaml and the job id.",
    "dataset": "final: published detail data. tariffline: raw tariff-line records.",
    "type": "C: goods. S: services.",
    "freq": "A: annual periods (YYYY). M: monthly periods (YYYYMM).",
    "classification": "Commodity classification, e.g. HS.",
    "reporters": "Countries whose declared trade is fetched. ⏎ opens a searchable picker.",
    "partners": "Counterpart countries. Default all (includes the World aggregate).",
    "flows": "M imports, X exports, RM re-imports, RX re-exports.",
    "commodities": "←/→ cycle TOTAL / aggregation level / explicit codes (⏎ picks codes).",
    "periods": "Ranges with ':' — 2015:2024 or 202001:202412; comma-separate to mix.",
    "breakdown": "classic: partner totals only. full: partner2/customs/transport (~10x rows).",
    "include_desc": "Include human-readable descriptions in downloaded rows.",
    "use_availability": "Only plan reporter-periods that exist per getDA (skips empty calls).",
}


# ---------------------------------------------------------------------------
# Key decoding
# ---------------------------------------------------------------------------


@contextmanager
def _cbreak(stream):
    fd = stream.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _read_key(timeout: float) -> str:
    """One raw byte from stdin, or "" on timeout. Reads the fd directly —
    sys.stdin.read(1) would buffer burst input (escape sequences, pastes)
    where select() can't see it."""
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        return ""
    data = os.read(sys.stdin.fileno(), 1)
    if not data:
        raise EOFError("stdin closed")
    return data.decode(errors="replace")


def decode_key(read: Callable[[float], str]) -> str:
    """Block until one logical key arrives from a ui._read_key-style reader."""
    ch = read(0.5)
    while ch == "":
        ch = read(0.5)
    if ch in ("\r", "\n"):
        return "enter"
    if ch in ("\x7f", "\x08"):
        return "backspace"
    if ch == " ":
        return "space"
    if ch == "\x1b":
        nxt = read(0.01)
        if nxt != "[":
            return "esc"
        final = read(0.01)
        return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(final, "esc")
    return ch


# ---------------------------------------------------------------------------
# Pickers
# ---------------------------------------------------------------------------


def make_picker(field_key: str, values: dict, refs: Refs) -> PickerState | None:
    """Build the overlay picker for a field; None when refs can't be loaded."""
    try:
        if field_key == "reporters":
            cur = values["reporters"]
            return PickerState(
                title="reporters",
                field_key="reporters",
                items=reporter_items(refs),
                selected=set() if cur == "all" else set(cur),
                all_selected=cur == "all",
            )
        if field_key == "partners":
            cur = values["partners"]
            return PickerState(
                title="partners",
                field_key="partners",
                items=partner_items(refs),
                selected=set() if cur == "all" else set(cur),
                all_selected=cur == "all",
            )
        if field_key == "flows":
            return PickerState(
                title="flows",
                field_key="flows",
                items=flow_items(),
                selected=set(values["flows"]),
                allow_all=False,
            )
        if field_key == "commodities":
            cl = values["classification"]
            return PickerState(
                title=f"{cl} codes",
                field_key="commodities",
                items=commodity_items(refs, cl),
                selected=set(values["commodities"]["codes"]),
                allow_all=False,
            )
    except TradescraprError:
        return None
    raise AssertionError(f"no picker for field {field_key!r}")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuotaInfo:
    tier: str
    used: int
    budget: int | None
    reset_s: float


def _wiz_header(q: QuotaInfo) -> Panel:
    title = Text.assemble(
        ("tradescrapr ", f"bold {P.accent}"),
        (f"v{__version__}", P.dim),
        ("   new job", "bold"),
        ("   tier ", P.dim),
        (q.tier, "bold magenta" if q.tier == "premium" else "bold"),
    )
    grid = Table.grid(padding=(0, 1))
    grid.add_column(justify="left")
    grid.add_column(width=30)
    grid.add_column(justify="left")
    grid.add_column(justify="right")
    if q.budget:
        ratio = q.used / q.budget
        bar_style = P.ok if ratio < 0.7 else (P.warn if ratio < 0.95 else P.bad)
        bar = ProgressBar(
            total=q.budget,
            completed=q.used,
            width=30,
            complete_style=bar_style,
            finished_style=P.bad,
        )
        detail = Text(f"{q.used}/{q.budget} calls", style="bold")
    else:
        bar = ProgressBar(total=1, completed=0, width=30, style=P.dim)
        detail = Text(f"{q.used} calls (unlimited)", style="bold")
    grid.add_row(
        Text("quota", style=P.dim), bar, detail, Text(f"resets in {span(q.reset_s)}", style=P.dim)
    )
    return Panel(Group(title, grid), box=box.ROUNDED, border_style=P.accent, padding=(0, 1))


def _codes_summary(labels: list[str], limit: int = 6) -> Text:
    shown = ", ".join(labels[:limit])
    extra = f" +{len(labels) - limit}" if len(labels) > limit else ""
    return Text.assemble((f"{len(labels)} selected: ", P.dim), (shown, "bold"), (extra, P.dim))


def _value_text(state: FormState, refs: Refs, fdef: FieldDef) -> Text:
    v = state.values[fdef.key]
    key = fdef.key
    if key == "name":
        if not v:
            return Text("(required)", style=P.warn)
        t = Text(str(v), style="bold")
        if (state.jobs_dir / f"{v}.yaml").exists():
            t.append(f"  ⚠ overwrites jobs/{v}.yaml", style=P.warn)
        return t
    if key == "type":
        return Text.assemble((v, "bold"), (f" · {_TYPE_LABELS.get(v, '')}", P.dim))
    if key == "freq":
        return Text.assemble((v, "bold"), (f" · {_FREQ_LABELS.get(v, '')}", P.dim))
    if key == "reporters":
        if v == "all":
            return Text.assemble(("all", "bold"), (f" ({len(refs.all_reporter_codes())})", P.dim))
        if not v:
            return Text("(required — ⏎ to pick)", style=P.warn)
        return _codes_summary([refs.reporter_iso3(c) or str(c) for c in sorted(v, key=int)])
    if key == "partners":
        if v == "all":
            return Text.assemble(("all", "bold"), (f" ({len(refs.all_partner_codes())})", P.dim))
        return _codes_summary([partner_label(refs, c) for c in sorted(v, key=int)])
    if key == "flows":
        return Text(", ".join(v), style="bold")
    if key == "commodities":
        mode = v["mode"]
        if mode == "total":
            return Text.assemble(("TOTAL", "bold"), (" · no commodity detail", P.dim))
        if mode == "codes":
            if not v["codes"]:
                return Text("(⏎ to pick codes)", style=P.warn)
            return _codes_summary(sorted(v["codes"]))
        level = mode.removeprefix("level")
        return Text.assemble(
            (f"{state.values['classification']} level {level}", "bold"),
            (" · ←/→ to change", P.dim),
        )
    if key == "periods":
        return Text(str(v), style="bold") if str(v).strip() else Text("(required)", style=P.warn)
    if fdef.kind == "bool":
        return Text("yes", style=f"bold {P.ok}") if v else Text("no", style=P.dim)
    return Text(str(v), style="bold")


def _form_panel(state: FormState, refs: Refs) -> Panel:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=1)
    grid.add_column(width=16)
    grid.add_column(overflow="ellipsis", no_wrap=True)
    for i, fdef in enumerate(FIELDS):
        current = i == state.cursor and state.mode is not Mode.DONE
        marker = Text("▸" if current else " ", style=f"bold {P.accent}")
        label = Text(fdef.label, style=f"bold {P.accent}" if current else P.dim)
        if current and state.mode is Mode.EDIT_TEXT:
            value = Text.assemble((state.edit_buffer, f"bold {P.accent}"), ("▊", P.accent))
        else:
            value = _value_text(state, refs, fdef)
        grid.add_row(marker, label, value)
    return Panel(
        grid,
        title="selection",
        title_align="left",
        box=box.ROUNDED,
        border_style=P.accent,
        padding=(0, 1),
    )


def _estimate_panel(est: Estimate, q: QuotaInfo) -> Panel:
    if est.error:
        body: RenderableType = Text(est.error, style=P.warn)
    else:
        lines = [Text(f"~{est.tasks:,} tasks", style=f"bold {P.accent}")]
        if est.days is not None:
            lines.append(
                Text.assemble((f"~{est.days} day(s)", "bold"), (f" at {q.budget} calls/day", P.dim))
            )
        else:
            lines.append(Text("no daily budget (premium)", style=P.dim))
        lines.append(
            Text(
                f"{est.n_reporters} reporters × {est.n_periods} periods "
                f"× {est.n_flow_groups} flow group(s)",
                style=P.dim,
            )
        )
        body = Group(*lines)
    caveat = Text("availability filter may reduce;\ntruncation splits may add", style=P.dim)
    return Panel(
        Group(body, caveat),
        title="estimate",
        title_align="left",
        box=box.ROUNDED,
        border_style=P.dim,
        padding=(0, 1),
    )


def _help_panel(state: FormState) -> Panel:
    fdef = state.current_field()
    return Panel(
        Text(_HELP.get(fdef.key, ""), style=P.dim),
        title=fdef.label,
        title_align="left",
        box=box.ROUNDED,
        border_style=P.dim,
        padding=(0, 1),
    )


def _picker_panel(picker: PickerState, avail_height: int) -> Panel:
    vis = picker.visible()
    rows = max(3, avail_height - 4)  # borders + title padding + query line
    start = max(0, min(picker.cursor - rows // 2, len(vis) - rows))

    lines: list[Text] = [
        Text.assemble(("/ ", P.dim), (picker.query, f"bold {P.accent}"), ("▊", P.accent))
    ]
    if not vis:
        lines.append(Text("no matches", style=P.dim))
    for i, item in enumerate(vis[start : start + rows], start=start):
        picked = picker.all_selected or item.code in picker.selected
        line = Text.assemble(
            ("▸ " if i == picker.cursor else "  ", f"bold {P.accent}"),
            ("✔ " if picked else "· ", P.ok if picked else P.dim),
            (item.label, "bold" if i == picker.cursor else ""),
        )
        if i == picker.cursor:
            line.stylize("reverse")
        lines.append(line)

    if picker.all_selected:
        count = Text(f"ALL ({len(picker.items)})", style=f"bold {P.ok}")
    else:
        count = Text(f"{len(picker.selected)} selected", style=P.dim)
    title = Text.assemble((picker.title, f"bold {P.accent}"), (" — ", P.dim), count)
    return Panel(
        Group(*lines),
        title=title,
        title_align="left",
        box=box.ROUNDED,
        border_style=P.accent,
        padding=(0, 1),
    )


def _result_panel(
    result: PlanResult | None, plan_error: str | None, saved_path: Path | None, has_key: bool
) -> Panel:
    lines: list[Text] = []
    border = P.ok
    if saved_path is not None:
        lines.append(Text.assemble(("✔ saved ", P.ok), (str(saved_path), "bold")))
    if plan_error:
        border = P.warn
        lines.append(Text(f"✘ planning failed: {plan_error}", style=P.bad))
        lines.append(Text(f"  retry later: uv run tradescrapr plan {saved_path}", style=P.dim))
    elif result is not None:
        lines.append(Text.assemble(("job    ", P.dim), (result.job_id, f"bold {P.accent}")))
        lines.append(
            Text.assemble(
                ("tasks  ", P.dim),
                (str(result.n_tasks), "bold"),
                (" (>= as many API calls; truncation splits add more)", P.dim),
            )
        )
        lines.append(Text.assemble(("quota  ", P.dim), (result.est_days, "bold")))
        if result.sample:
            lines.append(Text("sample", style=P.dim))
            lines.extend(Text(f"  - {s}", style=P.dim) for s in result.sample)
        if result.created:
            lines.append(Text(f"created job with {result.added} task(s)", style=P.ok))
        else:
            lines.append(
                Text(f"job already existed; {result.added} new task(s) added", style=P.warn)
            )
    lines.append(Text())
    can_run = has_key and result is not None
    lines.append(
        Text.assemble(
            (" w ", f"bold {P.accent}" if can_run else P.dim),
            (
                "download now: background worker + live dashboard"
                + ("" if has_key else " (no API key)"),
                P.dim,
            ),
        )
    )
    lines.append(
        Text.assemble(
            (" d ", f"bold {P.accent}"),
            ("open dashboard", P.dim),
            ("    q ", f"bold {P.accent}"),
            ("quit", P.dim),
        )
    )
    return Panel(
        Group(*lines),
        title="job planned",
        title_align="left",
        box=box.ROUNDED,
        border_style=border,
        padding=(0, 1),
    )


def _footer(state: FormState) -> Text:
    def hints(*pairs: tuple[str, str]) -> Text:
        t = Text()
        for k, label in pairs:
            t.append(f" {k} ", style=f"bold {P.accent}")
            t.append(f"{label}  ", style=P.dim)
        return t

    if state.mode is Mode.IMPORT_PATH:
        t = Text()
        if state.error:
            t.append(f" ✘ {state.error}  ", style=f"bold {P.bad}")
        t.append(" path ", style=f"bold {P.accent}")
        t.append(state.edit_buffer, style=f"bold {P.accent}")
        t.append("▊", style=P.accent)
        t.append("  ")
        t.append(hints(("⏎", "import"), ("esc", "cancel")))
        return t
    if state.error:
        return Text(f" ✘ {state.error}", style=f"bold {P.bad}")
    if state.mode is Mode.CONFIRM_OVERWRITE:
        name = str(state.values["name"]).strip()
        return Text(f" overwrite jobs/{name}.yaml? y / n", style=f"bold {P.warn}")
    if state.mode is Mode.EDIT_TEXT:
        return hints(("type", "to edit"), ("⏎", "commit"), ("esc", "cancel"))
    if state.mode is Mode.PICKER:
        if state.picker is not None and state.picker.can_import():
            return hints(
                ("↑↓", "move"),
                ("space", "toggle"),
                ("*", "all"),
                ("I", "import"),
                ("⏎", "done"),
                ("esc", "cancel"),
                ("type", "to search"),
            )
        return hints(
            ("↑↓", "move"),
            ("space", "toggle"),
            ("*", "all"),
            ("⏎", "done"),
            ("esc", "cancel"),
            ("type", "to search"),
        )
    if state.mode is Mode.DONE:
        return hints(("w", "download now"), ("d", "dashboard"), ("q", "quit"))
    return hints(
        ("↑↓", "move"), ("⏎", "edit"), ("←→", "cycle"), ("s", "save + plan"), ("q", "quit")
    )


def render_wizard(
    state: FormState,
    est: Estimate,
    quota: QuotaInfo,
    refs: Refs,
    width: int,
    height: int,
    *,
    result: PlanResult | None = None,
    plan_error: str | None = None,
    saved_path: Path | None = None,
    has_key: bool = False,
) -> RenderableType:
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        return Panel(
            Text(
                f"terminal too small — need at least {MIN_WIDTH}x{MIN_HEIGHT}, "
                f"got {width}x{height}. Resize to continue (q quits).",
                style=P.warn,
            ),
            box=box.ROUNDED,
            border_style=P.warn,
        )

    layout = Layout()
    layout.split_column(
        Layout(_wiz_header(quota), name="header", size=4),
        Layout(name="body"),
        Layout(_footer(state), name="footer", size=1),
    )
    body_h = height - 5
    if state.mode is Mode.DONE:
        layout["body"].update(_result_panel(result, plan_error, saved_path, has_key))
    elif state.mode in (Mode.PICKER, Mode.IMPORT_PATH) and state.picker is not None:
        layout["body"].update(_picker_panel(state.picker, body_h))
    else:
        layout["body"].split_row(
            Layout(_form_panel(state, refs), name="form", ratio=3),
            Layout(name="right", ratio=2),
        )
        layout["right"].split_column(
            Layout(_estimate_panel(est, quota), name="estimate", size=8),
            Layout(_help_panel(state), name="help"),
        )
    if P.bg:  # phosphor/amber paint their own background
        return Styled(layout, Style(color=P.text, bgcolor=P.bg))
    return layout


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def _prewarm(refs: Refs, classification: str = "HS") -> None:
    refs.reporters()
    refs.partners()
    refs.classification(classification)


def cmd_new(
    cfg: Config,
    *,
    key_source: Callable[[], str] | None = None,
    console: Console | None = None,
) -> int:
    from rich.live import Live

    global P
    P = load_theme(cfg.paths.state_dir)
    console = console or Console()
    client = ComtradeClient(cfg)
    refs = Refs(client, cfg.paths.refs)
    try:
        with console.status("loading reference data…"):
            _prewarm(refs)
    except TradescraprError as e:
        console.print(
            f"[{P.bad}]error:[/{P.bad}] reference data unavailable ({e}) — "
            "run `tradescrapr refs refresh` when back online"
        )
        return 1

    store = Store(cfg.paths.state_db)
    quota = QuotaLedger(store, cfg.quota)
    jobs_dir = cfg.paths.root / "jobs"
    state = FormState.initial(jobs_dir)
    if key_source is not None:
        read_key = key_source
    else:

        def read_key() -> str:
            return decode_key(_read_key)

    result: PlanResult | None = None
    plan_error: str | None = None
    saved_display: Path | None = None  # relative to root where possible, for display
    handoff: tuple[str, str | None] | None = None

    try:
        with ExitStack() as stack:
            live = stack.enter_context(Live(console=console, screen=True, auto_refresh=False))
            if key_source is None:
                stack.enter_context(_cbreak(sys.stdin))
            while True:
                est = estimate(
                    state.values,
                    refs,
                    combine_flows=cfg.planner.combine_flows,
                    daily_budget=cfg.quota.daily_budget,
                )
                q = QuotaInfo(
                    tier=cfg.tier,
                    used=quota.used_today(),
                    budget=cfg.quota.daily_budget,
                    reset_s=quota.seconds_until_reset(),
                )
                live.update(
                    render_wizard(
                        state,
                        est,
                        q,
                        refs,
                        console.size.width,
                        console.size.height,
                        result=result,
                        plan_error=plan_error,
                        saved_path=saved_display,
                        has_key=bool(cfg.api_key),
                    ),
                    refresh=True,
                )
                key = read_key()

                if state.mode is Mode.DONE:
                    if key in ("q", "Q"):
                        return 0
                    if key in ("w", "W") and result is not None and cfg.api_key:
                        handoff = ("run", result.job_id)
                        break
                    if key in ("d", "D"):
                        handoff = ("ui", None)
                        break
                    continue

                action = state.handle_key(key, lambda fk: make_picker(fk, state.values, refs))
                if action is Action.QUIT:
                    return 0
                if action is Action.IMPORT:
                    raw_path = state.edit_buffer.strip()
                    if not raw_path:
                        state.error = "import path is required"
                        continue
                    try:
                        state.import_picker_file(Path(raw_path), refs)
                    except ConfigError as e:
                        state.error = str(e)
                    continue
                if action is Action.SAVE:
                    jobs_dir.mkdir(parents=True, exist_ok=True)
                    saved_path = jobs_dir / f"{state.values['name'].strip()}.yaml"
                    saved_path.write_text(dump_spec_yaml(state.values, refs))
                    try:
                        saved_display = saved_path.relative_to(cfg.paths.root)
                    except ValueError:
                        saved_display = saved_path
                    live.update(
                        Panel(
                            Text(
                                f"saved {saved_display} — planning… "
                                "(fetching availability may take a moment)",
                                style=P.accent,
                            ),
                            box=box.ROUNDED,
                            border_style=P.accent,
                        ),
                        refresh=True,
                    )
                    try:
                        result = plan_spec(cfg, saved_path)
                    except TradescraprError as e:
                        plan_error = str(e)
                    state.mode = Mode.DONE
    except (KeyboardInterrupt, EOFError):
        return 0
    finally:
        store.close()

    if handoff is None:
        return 0
    from .ui import cmd_ui

    spawned = None
    if handoff[0] == "run":
        from .ui.control import start_worker

        try:
            spawned = start_worker(cfg, job_id=handoff[1])
        except TradescraprError as e:
            # Most likely a worker is already running; the dashboard shows it.
            console.print(f"[{P.warn}]{e}[/{P.warn}]")
    return cmd_ui(cfg, spawned=spawned)
