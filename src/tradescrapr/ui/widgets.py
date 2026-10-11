"""Text-level widgets: smooth and stacked bars, an indeterminate scanner, braille
area graphs, sparklines, the phase pipeline, and footer key caps.

Bars resolve to eighths of a cell with the partial-block glyphs, so a 40-cell
bar moves in 320 steps; stacked bars blend two segments inside one cell by
drawing the left part as the glyph and the right part as its background.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from rich.text import Text

from .theme import EIGHTHS, SPARK, SPINNER, Theme, gradient


def spinner(t: float, speed: float = 10.0) -> str:
    return SPINNER[int(t * speed) % len(SPINNER)]


def bar(
    theme: Theme,
    width: int,
    frac: float,
    colors: Sequence[str] | None = None,
    *,
    track: str = "─",
) -> Text:
    """A smooth left-to-right fill coloured along a gradient."""
    out = Text()
    if width <= 0:
        return out
    frac = min(max(frac, 0.0), 1.0)
    eighths = round(frac * width * 8)
    full, rem = divmod(eighths, 8)
    grad = gradient(tuple(colors or theme.bar), width)
    for i in range(full):
        out.append("█", theme.s(grad[i]))
    used = full
    if rem and full < width:
        out.append(EIGHTHS[rem], theme.s(grad[full]))
        used += 1
    if used < width:
        out.append(track * (width - used), theme.s(theme.faint))
    return out


def stacked(
    theme: Theme,
    width: int,
    parts: Sequence[tuple[float, str]],
    total: float | None = None,
    *,
    track: str = "─",
    min_eighths: int = 3,
) -> Text:
    """Consecutive coloured segments, e.g. done | in flight | retrying | dead.
    Space beyond the parts is drawn as track. Non-zero parts get at least
    `min_eighths` so two dead tasks out of ten thousand still show."""
    out = Text()
    if width <= 0:
        return out
    total = total if total is not None else sum(v for v, _ in parts)
    if total <= 0:
        out.append(track * width, theme.s(theme.faint))
        return out
    cap = width * 8
    sizes = [v / total * cap for v, _ in parts]
    sizes = [max(s, min_eighths) if v > 0 else 0.0 for s, (v, _) in zip(sizes, parts, strict=True)]
    excess = sum(sizes) - min(sum(v for v, _ in parts) / total * cap, cap)
    if excess > 0 and sizes:  # pay for the minimums out of the largest part
        big = max(range(len(sizes)), key=sizes.__getitem__)
        sizes[big] = max(sizes[big] - excess, 0.0)
    # integer eighth boundaries
    bounds, acc = [], 0.0
    for s in sizes:
        acc += s
        bounds.append(min(round(acc), cap))
    colors = [c for _, c in parts]

    def color_at(e: int) -> str | None:  # colour covering eighth e, None = track
        for b, c in zip(bounds, colors, strict=True):
            if e < b:
                return c
        return None

    for cell in range(width):
        lo = cell * 8
        left = color_at(lo)
        right = color_at(lo + 7)
        if left == right:
            if left is None:
                out.append(track, theme.s(theme.faint))
            else:
                out.append("█", theme.s(left))
            continue
        split = next(e for e in range(lo, lo + 8) if color_at(e) != left)
        k = split - lo
        if left is None:  # cannot happen with ordered parts; fall back to track
            out.append(track, theme.s(theme.faint))
        elif right is None:
            out.append(EIGHTHS[k], theme.s(left))
        else:
            out.append(EIGHTHS[k], theme.s(left, bg=right))
    return out


def scanner(theme: Theme, width: int, t: float, color: str | None = None) -> Text:
    """Indeterminate progress: a block sweeping back and forth with soft edges."""
    out = Text()
    if width <= 0:
        return out
    block = max(3, width // 7)
    travel = max(width - block, 1)
    pos = (t * travel / 1.6) % (2 * travel)
    start = int(pos if pos <= travel else 2 * travel - pos)
    color = color or theme.accent
    for i in range(width):
        d = i - start
        if 0 <= d < block:
            ch = "▓" if d in (0, block - 1) else "█"
            out.append(ch, theme.s(color))
        elif d in (-1, block):
            out.append("░", theme.s(color))
        else:
            out.append("─", theme.s(theme.faint))
    return out


def columns(values: Sequence[float], width: int, height: int, colors: Sequence[str]) -> list[Text]:
    """Solid column graph, one sample per cell, eighth-block resolution per
    line (so a 3-line graph has 24 levels). Newest sample on the right; any
    non-zero sample shows at least a sliver."""
    vals = list(values)[-width:]
    vals = [0.0] * (width - len(vals)) + vals
    top = max(vals) if vals else 0.0
    levels = [0 if v <= 0 or top <= 0 else max(1, round(v / top * height * 8)) for v in vals]
    row_colors = gradient(tuple(colors), height)
    lines = []
    for r in range(height):  # r = 0 is the top line
        floor = (height - 1 - r) * 8
        chars = "".join(" ▁▂▃▄▅▆▇█"[min(max(lvl - floor, 0), 8)] for lvl in levels)
        lines.append(Text(chars, row_colors[height - 1 - r]))
    return lines


def sparkline(values: Sequence[float]) -> str:
    if not values:
        return ""
    peak = max(values) or 1
    return "".join(SPARK[min(7, int(v / peak * 7))] for v in values)


def pipeline(theme: Theme, steps: Sequence[str], current: str | None, t: float) -> Text:
    """request ── receive ── parse ── write, with finished steps ticked and the
    current one spinning."""
    out = Text()
    idx = steps.index(current) if current in steps else -1
    for i, name in enumerate(steps):
        if i:
            out.append(" ── ", theme.s(theme.ok if i <= idx else theme.faint))
        if i < idx:
            out.append("✔ ", theme.s(theme.ok))
            out.append(name, theme.s(theme.dim))
        elif i == idx:
            out.append(f"{spinner(t)} ", theme.s(theme.accent, bold=True))
            out.append(name, theme.s(theme.accent, bold=True))
        else:
            out.append("○ ", theme.s(theme.faint))
            out.append(name, theme.s(theme.faint))
    return out


def keycap(theme: Theme, key: str, label: str, *, enabled: bool = True) -> Text:
    out = Text()
    if enabled:
        out.append(f" {key} ", theme.s(theme.key_fg, bold=True, bg=theme.key_bg))
        out.append(f" {label} ", theme.s(theme.dim))
    else:
        out.append(f" {key} ", theme.s(theme.faint, bold=True, bg=theme.select_bg))
        out.append(f" {label} ", theme.s(theme.faint))
    return out


def ratio_color(theme: Theme, frac: float) -> str:
    """Point on the heat gradient for a 0..1 load (quota use, for one)."""
    grad = gradient(theme.heat, 101)
    return grad[min(max(int(math.floor(frac * 100)), 0), 100)]
