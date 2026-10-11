"""The terminal: alternate screen, cbreak input, key decoding, and a writer
that repaints only the cells that changed.

Rich's Live redraws the whole screen on every refresh; at ten frames a second
over SSH that is most of the traffic. Here each frame is compared with the
previous one line by line, and within a changed line only the span between
the first and last differing cell is sent — a ticking clock costs a dozen
bytes, not a screen. Frames are wrapped in synchronized-update markers so
terminals that support them (iTerm2, kitty, WezTerm, Ghostty, foot, Windows
Terminal) never show a half-drawn frame.
"""

from __future__ import annotations

import os
import select
import signal
import sys
import termios
import tty
from collections import deque

from rich.cells import cell_len
from rich.color import ColorSystem
from rich.segment import Segment

Line = list[Segment]

_ENTER_ALT = "\x1b[?1049h\x1b[H\x1b[2J\x1b[?25l\x1b[?7l"
_LEAVE_ALT = "\x1b[0m\x1b[?7h\x1b[?25h\x1b[?1049l"
_SYNC_ON, _SYNC_OFF = "\x1b[?2026h", "\x1b[?2026l"

_CSI_KEYS = {
    "A": "up",
    "B": "down",
    "C": "right",
    "D": "left",
    "H": "home",
    "F": "end",
    "Z": "backtab",
    "1~": "home",
    "4~": "end",
    "7~": "home",
    "8~": "end",
    "5~": "pgup",
    "6~": "pgdn",
    "2~": "insert",
    "3~": "delete",
    "11~": "f1",
}
_SS3_KEYS = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end", "P": "f1"}


def decode(buf: str) -> tuple[list[str], str]:
    """Split raw input into key names; returns (keys, unconsumed tail). A lone
    ESC at the end of the buffer is held back in case its sequence is still in
    flight; the caller flushes it as "esc" after a short wait."""
    keys: list[str] = []
    i = 0
    n = len(buf)
    while i < n:
        ch = buf[i]
        if ch == "\x1b":
            if i + 1 >= n:
                return keys, buf[i:]
            nxt = buf[i + 1]
            if nxt == "[":
                j = i + 2
                while j < n and not "@" <= buf[j] <= "~":  # parameters until the final byte
                    j += 1
                if j >= n:
                    return keys, buf[i:]
                final, params = buf[j], buf[i + 2 : j]
                if final == "~":  # ESC [ 5 ~ ; modifiers ride after a ';'
                    name = _CSI_KEYS.get(params.split(";")[0] + "~", "unknown")
                else:  # ESC [ A, or ESC [ 1 ; 5 A with modifiers
                    name = _CSI_KEYS.get(final, "unknown")
                keys.append(name)
                i = j + 1
                continue
            if nxt == "O" and i + 2 < n:
                keys.append(_SS3_KEYS.get(buf[i + 2], "unknown"))
                i += 3
                continue
            if nxt == "O":
                return keys, buf[i:]
            keys.append("esc")
            i += 1
            continue
        if ch in ("\r", "\n"):
            keys.append("enter")
        elif ch in ("\x7f", "\x08"):
            keys.append("backspace")
        elif ch == "\t":
            keys.append("tab")
        elif ch == " ":
            keys.append("space")
        elif ch == "\x0c":
            keys.append("redraw")  # Ctrl+L
        else:
            keys.append(ch)
        i += 1
    return keys, ""


def _cells(line: Line) -> list[tuple[str, object]]:
    """One (char, style) per terminal cell; a wide char's second cell is ''."""
    out: list[tuple[str, object]] = []
    for text, style, _ in line:
        for ch in text:
            out.append((ch, style))
            if cell_len(ch) == 2:
                out.append(("", style))
    return out


def changed_span(old: Line, new: Line) -> tuple[int, int | None] | None:
    """Cell range [start, stop) where `new` differs from `old`; None if they
    render the same. stop=None means "to the end of the line"."""
    a, b = _cells(old), _cells(new)
    n = min(len(a), len(b))
    start = 0
    while start < n and a[start] == b[start]:
        start += 1
    if start == len(a) == len(b):
        return None
    if len(a) != len(b):
        return start, None
    stop = len(b)
    while stop > start and a[stop - 1] == b[stop - 1]:
        stop -= 1
    while start > 0 and b[start][0] == "":  # don't start inside a wide char
        start -= 1
    if stop < len(b) and b[stop][0] == "":
        stop += 1
    return start, stop


def _encode(line: Line, color_system: ColorSystem | None) -> str:
    if color_system is None:
        return "".join(s.text for s in line)
    return "".join(
        style.render(text, color_system=color_system) if style else text for text, style, _ in line
    )


class Terminal:
    """Context manager owning the tty for a full-screen app."""

    def __init__(self, stdin=None, stdout=None):
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.in_fd = self.stdin.fileno()
        self.out_fd = self.stdout.fileno()
        self._saved = None
        self._prev: list[Line] = []
        self._size: tuple[int, int] | None = None
        self._pending = ""
        self._keys: deque[str] = deque()
        self._resized = False
        self._old_winch = None
        self._old_tstp = None
        self._old_cont = None

    # -- lifecycle ------------------------------------------------------------

    def __enter__(self) -> Terminal:
        self._saved = termios.tcgetattr(self.in_fd)
        tty.setcbreak(self.in_fd)
        self._write(_ENTER_ALT)
        self._old_winch = signal.signal(signal.SIGWINCH, self._on_winch)
        self._old_tstp = signal.signal(signal.SIGTSTP, self._on_tstp)
        self._old_cont = signal.signal(signal.SIGCONT, self._on_cont)
        return self

    def __exit__(self, *exc) -> None:
        for sig, old in (
            (signal.SIGWINCH, self._old_winch),
            (signal.SIGTSTP, self._old_tstp),
            (signal.SIGCONT, self._old_cont),
        ):
            if old is not None:
                signal.signal(sig, old)
        self._restore()

    def _restore(self) -> None:
        self._write(_LEAVE_ALT)
        if self._saved is not None:
            termios.tcsetattr(self.in_fd, termios.TCSADRAIN, self._saved)

    def _on_winch(self, *_args) -> None:
        self._resized = True

    def _on_tstp(self, *_args) -> None:
        # Ctrl+Z: hand the terminal back in a sane state, then really stop.
        self._restore()
        signal.signal(signal.SIGTSTP, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTSTP)

    def _on_cont(self, *_args) -> None:
        if self._saved is not None:
            tty.setcbreak(self.in_fd)
        self._write(_ENTER_ALT)
        signal.signal(signal.SIGTSTP, self._on_tstp)
        self.invalidate()

    # -- output ------------------------------------------------------------------

    def size(self) -> tuple[int, int]:
        try:
            sz = os.get_terminal_size(self.out_fd)
            return sz.columns, sz.lines
        except OSError:
            return 80, 24

    def invalidate(self) -> None:
        """Force a full repaint on the next draw."""
        self._prev = []
        self._size = None

    def draw(
        self, lines: list[Line], size: tuple[int, int], color_system: ColorSystem | None
    ) -> int:
        """Send what differs from the previous frame; returns bytes written."""
        out = [_SYNC_ON]
        full = size != self._size or self._resized
        if full:
            self._resized = False
            self._size = size
            self._prev = []
            out.append("\x1b[0m\x1b[2J")
        prev = self._prev
        for y, line in enumerate(lines):
            old = prev[y] if y < len(prev) else None
            if old == line:
                continue
            span = changed_span(old, line) if old is not None else (0, None)
            if span is None:
                continue
            start, stop = span
            if stop is None:
                # lengths differ (shouldn't, lines are padded): repaint the
                # whole line and clear whatever the old one left behind
                out.append(f"\x1b[{y + 1};1H{_encode(line, color_system)}\x1b[0m\x1b[K")
                continue
            part = list(Segment.divide(line, [start, stop]))[1]
            out.append(f"\x1b[{y + 1};{start + 1}H{_encode(part, color_system)}\x1b[0m")
        out.append(_SYNC_OFF)
        self._prev = list(lines)
        if len(out) == 2:
            return 0
        return self._write("".join(out))

    def _write(self, data: str) -> int:
        raw = data.encode("utf-8", errors="replace")
        view = memoryview(raw)
        while view:
            try:
                n = os.write(self.out_fd, view)
            except InterruptedError:
                continue
            except BlockingIOError:
                select.select([], [self.out_fd], [], 1.0)
                continue
            view = view[n:]
        return len(raw)

    @property
    def resized(self) -> bool:
        return self._resized

    # -- input ---------------------------------------------------------------------

    def read_key(self, timeout: float) -> str | None:
        """Next key name, waiting up to `timeout` seconds; None on timeout."""
        if self._keys:
            return self._keys.popleft()
        data = self._read(timeout)
        if data is None and not self._pending:
            return None
        self._pending += data or ""
        keys, self._pending = decode(self._pending)
        if self._pending:
            # An incomplete escape sequence: give the rest a moment to arrive,
            # otherwise it was a bare ESC press.
            self._pending += self._read(0.03) or ""
            more, self._pending = decode(self._pending)
            keys += more
            if self._pending:
                rest, self._pending = self._pending[1:], ""
                keys.append("esc")
                more, self._pending = decode(rest)
                keys += more
        self._keys.extend(keys)
        return self._keys.popleft() if self._keys else None

    def _read(self, timeout: float) -> str | None:
        try:
            ready, _, _ = select.select([self.in_fd], [], [], max(timeout, 0.0))
        except InterruptedError:  # SIGWINCH and friends
            return None
        if not ready:
            return None
        data = os.read(self.in_fd, 1024)
        if not data:
            raise EOFError("stdin closed")
        return data.decode("utf-8", errors="replace")
