"""Colour themes and glyphs shared by the dashboard, the job builder, and the
inline progress display.

Three palettes: `classic` (cyan on the terminal's own background), and two
phosphor-CRT looks, `phosphor` (P1 green) and `amber` (P3), which paint their
own near-black background so they look the same in any terminal. Colours are
hex; Rich downgrades them on 256- and 16-colour terminals.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from rich.color import Color
from rich.style import Style

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
EIGHTHS = " ▏▎▍▌▋▊▉█"  # left-aligned partial blocks, 0/8 .. 8/8
SPARK = "▁▂▃▄▅▆▇█"


@dataclass(frozen=True)
class Theme:
    name: str
    bg: str | None  # painted background; None keeps the terminal's
    text: str
    bright: str
    dim: str
    faint: str
    border: str
    accent: str
    accent2: str  # in-flight work
    ok: str
    warn: str
    bad: str
    info: str  # quota, monitor
    bar: tuple[str, ...]  # progress fill gradient, dark -> bright
    heat: tuple[str, ...]  # load gradient, calm -> hot
    bar_bg: str  # header strip
    bar_fg: str
    key_bg: str  # footer key caps
    key_fg: str
    select_bg: str

    # Styles are built once per (theme, colour, attrs) — views ask for hundreds a frame.
    def s(self, color: str, *, bold: bool = False, bg: str | None = None) -> Style:
        return _style(color, bold, bg)

    def status_color(self, status: str) -> str:
        return {
            "done": self.ok,
            "split": self.ok,
            "in_progress": self.accent2,
            "failed": self.warn,
            "dead": self.bad,
            "cancelled": self.faint,
            "pending": self.faint,
        }.get(status, self.dim)


@lru_cache(maxsize=4096)
def _style(color: str, bold: bool, bg: str | None) -> Style:
    return Style(color=None if color == "default" else color, bold=bold or None, bgcolor=bg)


@lru_cache(maxsize=256)
def gradient(stops: tuple[str, ...], n: int) -> tuple[str, ...]:
    """n hex colours spread evenly along the gradient through `stops`."""
    if n <= 0:
        return ()
    rgb = [Color.parse(c).get_truecolor() for c in stops]
    if len(rgb) == 1 or n == 1:
        return (stops[-1],) * n
    out = []
    for i in range(n):
        pos = i / (n - 1) * (len(rgb) - 1)
        k = min(int(pos), len(rgb) - 2)
        f = pos - k
        a, b = rgb[k], rgb[k + 1]
        r, g, bl = (round(a[j] + (b[j] - a[j]) * f) for j in range(3))
        out.append(f"#{r:02x}{g:02x}{bl:02x}")
    return tuple(out)


CLASSIC = Theme(
    name="classic",
    bg=None,
    text="default",
    bright="default",
    dim="#8a8a8a",
    faint="#4e4e4e",
    border="#46525e",
    accent="#5fd7ff",
    accent2="#87afff",
    ok="#5fd787",
    warn="#ffd75f",
    bad="#ff5f5f",
    info="#d787ff",
    bar=("#005f87", "#0087af", "#00afd7", "#5fd7ff", "#afffff"),
    heat=("#5fd787", "#afd75f", "#ffd75f", "#ff875f", "#ff5f5f"),
    bar_bg="#0d3b52",
    bar_fg="#e4f4ff",
    key_bg="#5fd7ff",
    key_fg="#001820",
    select_bg="#1b3445",
)

PHOSPHOR = Theme(
    name="phosphor",
    bg="#030b05",
    text="#3be26a",
    bright="#b4ffc6",
    dim="#22a04a",
    faint="#11502a",
    border="#16642f",
    accent="#7dffa0",
    accent2="#c8ff7d",
    ok="#3be26a",
    warn="#e8ff5c",
    bad="#ff6a5c",
    info="#5cffd6",
    bar=("#0f4d22", "#178a3a", "#2ee66b", "#a6ffbc"),
    heat=("#2ee66b", "#c8ff5c", "#ffd25c", "#ff6a5c"),
    bar_bg="#0e3d1d",
    bar_fg="#b4ffc6",
    key_bg="#3be26a",
    key_fg="#030b05",
    select_bg="#0c3318",
)

AMBER = Theme(
    name="amber",
    bg="#0d0800",
    text="#ffb000",
    bright="#ffdb8a",
    dim="#b07a00",
    faint="#523800",
    border="#6b4a00",
    accent="#ffcc4d",
    accent2="#ffe48f",
    ok="#ffb000",
    warn="#fff06b",
    bad="#ff5c3d",
    info="#ffd27a",
    bar=("#4d3500", "#8c6100", "#d99100", "#ffcc4d"),
    heat=("#ffb000", "#ffd000", "#ff8a3d", "#ff5c3d"),
    bar_bg="#3d2a00",
    bar_fg="#ffdb8a",
    key_bg="#ffb000",
    key_fg="#0d0800",
    select_bg="#2e2000",
)

THEMES: dict[str, Theme] = {t.name: t for t in (CLASSIC, PHOSPHOR, AMBER)}


def next_theme(current: Theme) -> Theme:
    names = list(THEMES)
    return THEMES[names[(names.index(current.name) + 1) % len(names)]]


# -- remembered preference ------------------------------------------------------

_PREFS = "ui.json"


def load_theme(state_dir: Path, override: str | None = None) -> Theme:
    name = override or os.getenv("TRADESCRAPR_THEME")
    if not name:
        try:
            name = json.loads((state_dir / _PREFS).read_text()).get("theme")
        except (OSError, ValueError, AttributeError):
            name = None
    return THEMES.get(str(name or "").lower(), CLASSIC)


def save_theme(state_dir: Path, theme: Theme) -> None:
    try:
        (state_dir / _PREFS).write_text(json.dumps({"theme": theme.name}))
    except OSError:
        pass  # a preference is not worth an error
