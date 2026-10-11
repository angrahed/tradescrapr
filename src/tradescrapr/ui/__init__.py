"""Live terminal dashboard: `tradescrapr ui`.

A full-screen view of the worker and the queue: what is downloading this
second (phase, bytes, server wait, an estimate from similar tasks), per-job
progress with ETAs, throughput history, problems, and the activity feed. It
can start a background worker, stop it, retry dead tasks, and cancel jobs.
`--once` prints one snapshot and exits (pipe-friendly).
"""

from __future__ import annotations

import sys
import time

from rich.console import Console
from rich.segment import SegmentLines

from ..config import Config
from ..livestatus import STATUS_FILE, read_status
from .app import App
from .context import Ctx, UIState, reconcile
from .control import Spawned
from .fmt import Names
from .pages import screen
from .snapshot import Collector, Snapshot
from .term import Terminal
from .theme import Theme, load_theme


def natural_height(snap: Snapshot) -> int:
    """Content-sized height for one-shot output (live mode fills the terminal)."""
    jobs = max(len(snap.jobs), 1) + 3
    activity = min(max(len(snap.events), 1), 14) + 2
    return 1 + 7 + jobs + max(activity, 8) + 1


def render_once(cfg: Config, theme: Theme, console: Console) -> None:
    collector = Collector(cfg)
    snap = collector.prime()
    now = time.time()
    ui = UIState(static=True)
    ctx = Ctx(
        theme=theme,
        names=Names(cfg.paths.refs),
        snap=snap,
        worker=reconcile(snap, read_status(cfg.paths.state_dir / STATUS_FILE), ui, now),
        ui=ui,
        now=now,
        t=0.0,
    )
    block = screen(ctx, console.width, natural_height(snap))
    console.print(SegmentLines(block.lines, new_lines=True), end="")


def cmd_ui(
    cfg: Config,
    *,
    refresh: float = 1.0,
    once: bool = False,
    theme: str | None = None,
    spawned: Spawned | None = None,
) -> int:
    """`spawned`: a worker just launched for this session (start / the job
    builder), so the dashboard can explain it if it dies during startup."""
    chosen = load_theme(cfg.paths.state_dir, theme)
    if once or not sys.stdout.isatty() or not sys.stdin.isatty():
        render_once(cfg, chosen, Console())
        return 0
    app = App(cfg, theme=chosen, refresh=refresh)
    app.ui.spawned = spawned
    app.collector.prime()
    app.collector.start()
    try:
        with Terminal() as term:
            return app.run(term)
    except (KeyboardInterrupt, EOFError):
        return 0
    finally:
        app.collector.stop()


__all__ = ["App", "cmd_ui", "render_once"]
