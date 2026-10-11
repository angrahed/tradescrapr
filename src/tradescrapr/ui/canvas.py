"""Screen composition: panels, joins, overlays, and the ANSI encoder.

Rich draws the text; this module does the layout. A Block is a rectangle of
segment lines, every line exactly `width` cells, so blocks join side by side
by concatenating lines and the terminal writer can diff the screen line by
line. Keeping layout here (instead of rich.layout) is what lets the dashboard
repaint ten times a second for the cost of the lines that changed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from rich.color import ColorSystem
from rich.console import Console
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from .theme import Theme

Line = list[Segment]

# Only used to turn Text into Segments (style lookup); it never prints.
_RENDER = Console(force_terminal=True, color_system="truecolor", width=4000, legacy_windows=False)

_COLOR_SYSTEMS = {
    "standard": ColorSystem.STANDARD,
    "256": ColorSystem.EIGHT_BIT,
    "truecolor": ColorSystem.TRUECOLOR,
    "windows": ColorSystem.WINDOWS,
}


@dataclass
class Block:
    width: int
    lines: list[Line]

    @property
    def height(self) -> int:
        return len(self.lines)

    def plain(self) -> str:
        return "\n".join("".join(s.text for s in ln) for ln in self.lines)


# Characters that would break the one-char-one-cell layout: C0/C1 controls (a
# server error message with a newline, a tab, an escape) and zero-width marks.
_UNSAFE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u0300-\u036f\u200b-\u200f\u2028-\u202e\u2060-\u2064\ufeff]"
)


def sanitize(text: Text) -> Text:
    """Replace unsafe characters with spaces; same length, so spans still fit."""
    plain = text.plain
    if not _UNSAFE.search(plain):
        return text
    return Text(
        _UNSAFE.sub(" ", plain),
        style=text.style,
        justify=text.justify,
        overflow=text.overflow,
        no_wrap=text.no_wrap,
        end=text.end,
        spans=list(text.spans),
    )


def text_line(text: Text | str, width: int, style: Style | None = None) -> Line:
    """One Text as exactly `width` cells: ellipsis when too long, padded when short."""
    if width <= 0:
        return []
    text = sanitize(Text(text) if isinstance(text, str) else text)
    if text.cell_len > width:
        text = text.copy()
        text.truncate(width, overflow="ellipsis")
    segs = list(text.render(_RENDER))
    if text.style and not text.spans:
        # Text.render skips the base style when there are no spans (Rich applies
        # it a level up, in __rich_console__); a single-style line needs it here.
        base = _RENDER.get_style(text.style) if isinstance(text.style, str) else text.style
        segs = [Segment(s.text, base) for s in segs]
    return Segment.adjust_line_length(segs, width, style=style)


def blank(width: int, height: int) -> Block:
    return Block(width, [[Segment(" " * width)] for _ in range(height)] if width > 0 else [])


def lines_block(width: int, height: int, body: Iterable[Text | str]) -> Block:
    rows = [text_line(t, width) for t, _ in zip(body, range(height), strict=False)]
    rows.extend([Segment(" " * width)] for _ in range(height - len(rows)))
    return Block(width, rows)


def panel(
    theme: Theme,
    width: int,
    height: int,
    body: Iterable[Text | str],
    *,
    title: str = "",
    right: Text | str | None = None,
    footer: Text | str | None = None,
    focus: bool = False,
    title_color: str | None = None,
    pad: int = 1,
) -> Block:
    """A rounded box with a title (and optional right-hand note) in the top
    border and an optional caption in the bottom border. `focus` draws the
    frame in the accent colour."""
    if width < 4 or height < 2:
        return blank(max(width, 0), max(height, 0))
    edge = theme.s(theme.accent if focus else theme.border)
    inner = width - 2 - 2 * pad

    def border_row(left: str, right_corner: str, lhs: Text | None, rhs: Text | None) -> Line:
        avail = width - 2
        lhs_t = Text("─ ", edge) + lhs + Text(" ", edge) if lhs else Text("", edge)
        rhs_t = Text(" ", edge) + rhs + Text(" ─", edge) if rhs else Text("", edge)
        if lhs_t.cell_len + rhs_t.cell_len > avail:
            rhs_t = Text("", edge)
        if lhs_t.cell_len > avail:
            lhs_t.truncate(avail, overflow="ellipsis")
        fill = avail - lhs_t.cell_len - rhs_t.cell_len
        row = Text(left, edge) + lhs_t + Text("─" * fill, edge) + rhs_t + Text(right_corner, edge)
        return list(row.render(_RENDER))

    tcolor = title_color or (theme.accent if focus else theme.text)
    title_t = Text(title, theme.s(tcolor, bold=True)) if title else None
    right_t = Text(right, theme.s(theme.dim)) if isinstance(right, str) else right
    foot_t = Text(footer, theme.s(theme.dim)) if isinstance(footer, str) else footer

    lines: list[Line] = [border_row("╭", "╮", title_t, right_t)]
    side = Segment("│", edge)
    gap = Segment(" " * pad) if pad else None
    body_rows = height - 2
    count = 0
    for t in body:
        if count >= body_rows:
            break
        row = [side] + ([gap] if gap else []) + text_line(t, inner) + ([gap] if gap else [])
        lines.append(row + [side])
        count += 1
    empty = [side, Segment(" " * (width - 2)), side]
    lines.extend(list(empty) for _ in range(body_rows - count))
    lines.append(border_row("╰", "╯", None, foot_t))
    return Block(width, lines)


def hjoin(*blocks: Block) -> Block:
    height = max((b.height for b in blocks), default=0)
    rows: list[Line] = [[] for _ in range(height)]
    for b in blocks:
        for y in range(height):
            rows[y].extend(b.lines[y] if y < b.height else [Segment(" " * b.width)])
    return Block(sum(b.width for b in blocks), rows)


def vjoin(*blocks: Block) -> Block:
    width = max((b.width for b in blocks), default=0)
    rows: list[Line] = []
    for b in blocks:
        for ln in b.lines:
            rows.append(ln if b.width == width else Segment.adjust_line_length(ln, width))
    return Block(width, rows)


def overlay(base: Block, top: Block, x: int, y: int) -> Block:
    """Paint `top` over `base` at column x, row y (a modal dialog)."""
    rows = list(base.lines)
    for dy, top_line in enumerate(top.lines):
        row = y + dy
        if not 0 <= row < base.height:
            continue
        left, _mid, right = Segment.divide(rows[row], [x, x + top.width, base.width])
        rows[row] = list(left) + list(top_line) + list(right)
    return Block(base.width, rows)


def restyle(block: Block, style: Style | None = None, post: Style | None = None) -> Block:
    """Apply a base style under (style) or on top of (post) every segment —
    a painted theme background, or dimming the screen behind a dialog."""
    return Block(
        block.width,
        [list(Segment.apply_style(ln, style=style, post_style=post)) for ln in block.lines],
    )


def color_system_for(console: Console) -> ColorSystem | None:
    return _COLOR_SYSTEMS.get(console.color_system or "")


def encode_line(line: Line, color_system: ColorSystem | None) -> str:
    """Segments -> one ANSI string (each segment resets after itself)."""
    if color_system is None:
        return "".join(s.text for s in line)
    out = []
    for text, style, _control in line:
        out.append(style.render(text, color_system=color_system) if style else text)
    return "".join(out)
