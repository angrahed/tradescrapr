"""Number, size, duration, and name formatting for the terminal views."""

from __future__ import annotations

import json
from pathlib import Path

FLOW_NAMES = {"M": "imports", "X": "exports", "RM": "re-imports", "RX": "re-exports"}


def human_bytes(n: float) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or unit == "TB":
            return f"{x:,.0f} {unit}" if unit == "B" else f"{x:,.1f} {unit}"
        x /= 1024
    return f"{x:,.1f} TB"


def human_count(n: float) -> str:
    n = float(n)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 100_000:
        return f"{n / 1_000:.0f}k"
    if n >= 10_000:
        return f"{n / 1_000:.1f}k"
    return f"{n:,.0f}"


def clock(seconds: float) -> str:
    """Elapsed-timer style: 0:07, 12:03, 1:12:04."""
    s = max(int(seconds), 0)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def span(seconds: float) -> str:
    """Human duration, two units at most: 45s, 12m, 1h 52m, 3d 4h."""
    s = max(int(round(seconds)), 0)
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m {sec:02d}s" if m < 10 and sec else f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


def rate(bytes_per_s: float) -> str:
    return f"{human_bytes(bytes_per_s)}/s"


def flows_label(flows: list[str] | tuple[str, ...], *, short: bool = False) -> str:
    if not flows:
        return "all flows"
    if short or len(flows) > 1:
        return "+".join(flows)
    return FLOW_NAMES.get(flows[0], flows[0])


def job_name(job_id: str, spec_name: str | None = None) -> str:
    """tgt-<name>-<hash8> -> <name>; monitor ids keep a readable timestamp."""
    if spec_name:
        return spec_name
    if job_id.startswith("tgt-") and job_id.count("-") >= 2:
        return job_id[4:].rsplit("-", 1)[0]
    if job_id.startswith("mon-") and len(job_id) >= 19:
        ts = job_id[4:]
        return f"monitor {ts[0:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}"
    return job_id


class Names:
    """Reporter code -> ISO3 / name from the cached reference file. Never touches
    the network: a dashboard must render even when offline."""

    def __init__(self, refs_dir: Path):
        self._path = refs_dir / "Reporters.json"
        self._iso: dict[str, str] | None = None
        self._name: dict[str, str] = {}

    def _load(self) -> dict[str, str]:
        if self._iso is None:
            self._iso = {}
            try:
                rows = json.loads(self._path.read_text())["results"]
            except (OSError, ValueError, KeyError, TypeError):
                rows = []
            for r in rows:
                code = str(r.get("reporterCode"))
                iso = r.get("reporterCodeIsoAlpha3")
                if iso:
                    self._iso[code] = str(iso)
                if r.get("reporterDesc"):
                    self._name[code] = str(r["reporterDesc"])
        return self._iso

    def iso(self, code: str | None) -> str:
        if code is None:
            return "?"
        return self._load().get(str(code), str(code))

    def name(self, code: str | None) -> str:
        self._load()
        return self._name.get(str(code), self.iso(code))
