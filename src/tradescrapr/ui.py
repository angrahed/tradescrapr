"""Live terminal dashboard: `tradescrapr ui`.

A read-only observer meant to run beside a worker (in tmux, another SSH session,
or over the finished state after a run). It reads the WAL SQLite store, probes
the worker lock, tails the log, and repaints every few seconds. `--once` prints
a single snapshot and exits (works over plain pipes too).

Keys: q quit, r refresh now.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import re
import select
import sys
import termios
import time
import tty
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from rich import box
from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from . import __version__
from .config import Config
from .quota import QuotaLedger
from .state import JobProgress, Store

SPARK_BLOCKS = "▁▂▃▄▅▆▇█"

ACCENT = "cyan"
OK = "green"
WARN = "yellow"
BAD = "red"
DIM = "grey58"


# ---------------------------------------------------------------------------
# Snapshot: everything the dashboard shows, gathered in one pass
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    now: dt.datetime
    tier: str
    worker_pid: int | None  # None = no worker holds the lock
    quota_used: int
    quota_budget: int | None
    quota_reset_s: float
    quota_history: list[tuple[str, int]]  # (date, calls), oldest first
    jobs: list[JobProgress] = field(default_factory=list)
    in_progress: list[dict] = field(default_factory=list)
    dead: list[dict] = field(default_factory=list)
    releases: dict[str, int] = field(default_factory=dict)
    release_periods: list[dict] = field(default_factory=list)
    activity: list[str] = field(default_factory=list)  # raw log lines, oldest first
    data_files: int = 0
    data_bytes: int = 0


def probe_worker(lock_path: Path) -> int | None:
    """PID of the worker holding the lock, or None. Probing briefly acquires a
    free lock, which is harmless — the real worker holds it for its whole run."""
    if not lock_path.exists():
        return None
    try:
        with open(lock_path) as fd:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                txt = fd.read().strip()
                return int(txt) if txt.isdigit() else -1
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None
    except OSError:
        return None


_INTERESTING = (
    "task=",
    "quota",
    "monitor run=",
    "worker stop",
    "cleaned partition",
    "backing off",
    "reclaimed",
)


def tail_log(path: Path, keep: int = 50, max_bytes: int = 1 << 16) -> list[str]:
    if not path.exists():
        return []
    size = path.stat().st_size
    with open(path, "rb") as f:
        f.seek(max(0, size - max_bytes))
        text = f.read().decode(errors="replace")
    lines = [ln for ln in text.splitlines() if any(k in ln for k in _INTERESTING)]
    return lines[-keep:]


def data_stats(data_root: Path) -> tuple[int, int]:
    files = total = 0
    if data_root.is_dir():
        for p in data_root.rglob("*.parquet"):
            files += 1
            total += p.stat().st_size
    return files, total


def gather(cfg: Config, store: Store, stats: tuple[int, int]) -> Snapshot:
    quota = QuotaLedger(store, cfg.quota)
    return Snapshot(
        now=dt.datetime.now(),
        tier=cfg.tier,
        worker_pid=probe_worker(cfg.paths.state_dir / "worker.lock"),
        quota_used=quota.used_today(),
        quota_budget=cfg.quota.daily_budget,
        quota_reset_s=quota.seconds_until_reset(),
        quota_history=[(r["quota_date"], r["calls_used"]) for r in store.quota_history()],
        jobs=store.job_progress(),
        in_progress=[dict(r) for r in store.in_progress_tasks()],
        dead=[dict(r) for r in store.dead_tasks()[:5]],
        releases=store.release_summary(),
        release_periods=[dict(r) for r in store.release_periods()],
        activity=tail_log(cfg.paths.logs / "tradescrapr.log"),
        data_files=stats[0],
        data_bytes=stats[1],
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def sparkline(values: list[int]) -> str:
    if not values:
        return ""
    peak = max(values) or 1
    return "".join(SPARK_BLOCKS[min(7, int(v / peak * 7))] for v in values)


def human_bytes(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or unit == "TB":
            return f"{x:,.0f} {unit}" if unit == "B" else f"{x:,.1f} {unit}"
        x /= 1024
    return f"{x:,.1f} TB"


def human_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.0f}k"
    return f"{n:,}"


def _fmt_reset(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    return f"{h}h {rem // 60:02d}m"


def _header(snap: Snapshot) -> Panel:
    title = Text.assemble(
        ("tradescrapr ", f"bold {ACCENT}"),
        (f"v{__version__}", DIM),
        ("   tier ", DIM),
        (snap.tier, "bold magenta" if snap.tier == "premium" else "bold"),
        ("   worker ", DIM),
        (("● running", f"bold {OK}") if snap.worker_pid else ("○ idle", DIM)),
        ((f" pid {snap.worker_pid}", DIM) if snap.worker_pid and snap.worker_pid > 0 else ("", "")),
        ("   ", ""),
        (snap.now.strftime("%Y-%m-%d %H:%M:%S"), DIM),
    )

    used, budget = snap.quota_used, snap.quota_budget
    quota = Table.grid(padding=(0, 1))
    quota.add_column(justify="left")
    quota.add_column(width=30)
    quota.add_column(justify="left")
    quota.add_column(justify="right")
    if budget:
        ratio = used / budget
        bar_style = OK if ratio < 0.7 else (WARN if ratio < 0.95 else BAD)
        bar = ProgressBar(
            total=budget, completed=used, width=30, complete_style=bar_style, finished_style=BAD
        )
        detail = Text(f"{used}/{budget} calls", style="bold")
    else:
        bar = ProgressBar(total=1, completed=0, width=30, style=DIM)
        detail = Text(f"{used} calls (unlimited)", style="bold")
    history = Text.assemble(
        ("7d ", DIM),
        (sparkline([v for _, v in snap.quota_history]) or "▁" * 7, ACCENT),
        (f"  resets in {_fmt_reset(snap.quota_reset_s)}", DIM),
    )
    quota.add_row(Text("quota", DIM), bar, detail, history)

    return Panel(Group(title, quota), box=box.ROUNDED, border_style=ACCENT, padding=(0, 1))


def _eta(snap: Snapshot, jp: JobProgress) -> str:
    if not jp.remaining:
        return "—"
    if not snap.quota_budget:
        return "…"
    days = jp.remaining / snap.quota_budget
    return "<1d" if days < 1 else f"{days:.1f}d"


def _jobs_panel(snap: Snapshot) -> Panel:
    table = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False, header_style=DIM)
    table.add_column("job", overflow="ellipsis", no_wrap=True, ratio=1)
    table.add_column("progress", width=20)
    table.add_column("", justify="right", width=4)
    table.add_column("done", justify="right", width=6)
    table.add_column("pend", justify="right", width=5)
    table.add_column("fail", justify="right", width=4)
    table.add_column("dead", justify="right", width=4)
    table.add_column("records", justify="right", width=8)
    table.add_column("eta", justify="right", width=4)

    for jp in sorted(snap.jobs, key=lambda j: j.created_at, reverse=True):
        settled = sum(jp.counts.get(s, 0) for s in ("done", "split", "dead", "cancelled"))
        total = jp.total or 1
        pct = 100 * settled / total
        badge = ("⟳ ", "magenta") if jp.kind == "monitor" else ("▶ ", ACCENT)
        name = Text.assemble(badge, (jp.job_id, "bold" if jp.remaining else DIM))
        bar = ProgressBar(
            total=total,
            completed=settled,
            width=20,
            complete_style=OK if pct == 100 else ACCENT,
            finished_style=OK,
        )
        failed = jp.counts.get("failed", 0)
        dead = jp.counts.get("dead", 0)
        table.add_row(
            name,
            bar,
            f"{pct:.0f}%",
            f"{jp.counts.get('done', 0):,}",
            Text(str(jp.remaining), "bold" if jp.remaining else DIM),
            Text(str(failed), WARN if failed else DIM),
            Text(str(dead), BAD if dead else DIM),
            human_count(jp.records),
            Text(_eta(snap, jp), DIM),
        )

    if not snap.jobs:
        empty = Text("no jobs yet — try: tradescrapr plan jobs/smoke.yaml", DIM)
        return Panel(empty, title="jobs", box=box.ROUNDED, border_style=DIM, padding=(0, 1))
    return Panel(
        table,
        title=f"jobs ({len(snap.jobs)})",
        title_align="left",
        box=box.ROUNDED,
        border_style=DIM,
        padding=(0, 1),
    )


def _now_panel(snap: Snapshot) -> Panel:
    if snap.in_progress:
        lines = []
        for t in snap.in_progress:
            line = Text.assemble(
                ("⚙ ", WARN),
                (f"{t['dataset']}/{t['freq']} ", ""),
                (f"reporter={t['reporter']} period={t['period']} ", "bold"),
                (f"({t['task_id'][:8]})", DIM),
            )
            line.no_wrap = True
            line.overflow = "ellipsis"
            lines.append(line)
        body: RenderableType = Group(*lines)
    else:
        body = Text("nothing in flight", DIM)
    return Panel(
        body,
        title="fetching now",
        title_align="left",
        box=box.ROUNDED,
        border_style=DIM,
        padding=(0, 1),
    )


_NOISE = re.compile(r"job=\S+ |flows=|cmd=AG\d partners=all ?|partners=all ?")


def _style_activity(line: str) -> Text:
    # file log format: "YYYY-mm-dd HH:MM:SS,ms LEVEL logger message..."
    parts = line.split(" ", 3)
    when = parts[1][:8] if len(parts) > 3 and ":" in parts[1] else ""
    msg = parts[3].split(" ", 1)[-1] if len(parts) > 3 else line
    msg = _NOISE.sub("", msg)
    if "status=done" in msg:
        style, icon = OK, "✔"
    elif "split into" in msg:
        style, icon = WARN, "⑃"
    elif "DEAD" in msg:
        style, icon = BAD, "✘"
    elif "quota" in msg:
        style, icon = "magenta", "◔"
    elif "backing off" in msg or "transient" in msg:
        style, icon = WARN, "…"
    elif "worker stop" in msg or "monitor run=" in msg:
        style, icon = ACCENT, "■"
    else:
        style, icon = DIM, "·"
    out = Text.assemble((f"{when} ", DIM), (f"{icon} ", style), (msg, style))
    out.no_wrap = True
    out.overflow = "ellipsis"
    return out


def _activity_panel(snap: Snapshot, height: int) -> Panel:
    newest_first = list(reversed(snap.activity))[: max(height - 2, 1)]
    body: RenderableType = (
        Group(*(_style_activity(ln) for ln in newest_first))
        if newest_first
        else Text("no activity yet", DIM)
    )
    return Panel(
        body,
        title="activity (newest first)",
        title_align="left",
        box=box.ROUNDED,
        border_style=DIM,
        padding=(0, 1),
    )


def _monitor_panel(snap: Snapshot) -> Panel:
    done = snap.releases.get("done", 0)
    pending = snap.releases.get("pending", 0)
    header = Text.assemble(
        ("releases  ", DIM),
        (f"{done:,} done", OK if done else DIM),
        ("  ·  ", DIM),
        (f"{pending:,} pending", WARN if pending else DIM),
    )
    if snap.release_periods:
        table = Table(box=None, header_style=DIM, pad_edge=False, padding=(0, 2))
        table.add_column("period", width=8)
        table.add_column("done", justify="right", width=5)
        table.add_column("pending", justify="right", width=7)
        for r in snap.release_periods:
            table.add_row(
                str(r["period"]),
                Text(f"{r['done'] or 0}", OK),
                Text(f"{r['pending'] or 0}", WARN if r["pending"] else DIM),
            )
        body: RenderableType = Group(header, table)
    else:
        body = Group(header, Text("monitor has not run yet", DIM))
    return Panel(
        body, title="monitor", title_align="left", box=box.ROUNDED, border_style=DIM, padding=(0, 1)
    )


def _dead_panel(snap: Snapshot) -> Panel:
    lines = [
        Text.assemble(
            ("✘ ", BAD),
            (f"{d['task_id'][:8]} ", "bold"),
            (f"{d['dataset']}/{d['freq']} r={d['reporter']} p={d['period']}  ", ""),
            ((d["last_error"] or "")[:60], DIM),
        )
        for d in snap.dead
    ]
    body = Group(*lines) if lines else Text("none 🎉", DIM)
    return Panel(
        body,
        title="dead tasks",
        title_align="left",
        box=box.ROUNDED,
        border_style=BAD if snap.dead else DIM,
        padding=(0, 1),
    )


def _footer(snap: Snapshot) -> Text:
    return Text.assemble(
        (" q ", f"bold {ACCENT}"),
        ("quit  ", DIM),
        (" r ", f"bold {ACCENT}"),
        ("refresh  ", DIM),
        ("│  ", DIM),
        (f"data: {snap.data_files:,} parquet · {human_bytes(snap.data_bytes)}", DIM),
    )


def auto_height(snap: Snapshot) -> int:
    """Content-sized height for one-shot snapshots (live mode fills the terminal)."""
    jobs_h = min(max(len(snap.jobs), 1) + 5, 11)
    left_h = (min(len(snap.in_progress), 5) + 2) + (min(len(snap.release_periods), 6) + 5)
    right_h = min(len(snap.activity), 25) + 2 + min(len(snap.dead), 5) + 2
    return 5 + jobs_h + max(left_h, right_h, 10)


def render(snap: Snapshot, height: int = 40) -> Layout:
    layout = Layout()
    jobs_h = min(max(len(snap.jobs), 1) + 5, 11)
    body_h = max(height - 5 - jobs_h, 10)
    layout.split_column(
        Layout(_header(snap), name="header", size=4),
        Layout(_jobs_panel(snap), name="jobs", size=jobs_h),
        Layout(name="body", size=body_h),
        Layout(_footer(snap), name="footer", size=1),
    )
    layout["body"].split_row(
        Layout(name="left", ratio=2),
        Layout(name="right", ratio=3),
    )
    layout["left"].split_column(
        Layout(_now_panel(snap), name="now", size=min(len(snap.in_progress), 5) + 2),
        Layout(_monitor_panel(snap), name="monitor"),
    )
    layout["right"].split_column(
        Layout(_activity_panel(snap, body_h - (min(len(snap.dead), 5) + 2)), name="activity"),
        Layout(_dead_panel(snap), name="dead", size=min(len(snap.dead), 5) + 2),
    )
    return layout


# ---------------------------------------------------------------------------
# Live loop
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
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    return sys.stdin.read(1) if ready else ""


def cmd_ui(cfg: Config, *, refresh: float = 2.0, once: bool = False) -> int:
    from rich.live import Live

    store = Store(cfg.paths.state_db)
    console = Console()
    stats = data_stats(cfg.paths.data)
    stats_at = time.monotonic()
    try:
        if once or not sys.stdout.isatty() or not sys.stdin.isatty():
            snap = gather(cfg, store, stats)
            console.print(render(snap, height=auto_height(snap)))
            return 0
        with (
            Live(console=console, screen=True, auto_refresh=False) as live,
            _cbreak(sys.stdin),
        ):
            while True:
                if time.monotonic() - stats_at > 30:  # rescanning data/ is the slow bit
                    stats = data_stats(cfg.paths.data)
                    stats_at = time.monotonic()
                snap = gather(cfg, store, stats)
                live.update(render(snap, height=console.size.height), refresh=True)
                key = _read_key(refresh)
                if key in ("q", "Q"):
                    return 0
    except KeyboardInterrupt:
        return 0
    finally:
        store.close()
