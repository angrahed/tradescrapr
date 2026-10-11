"""Pace, ETA, and throughput from the task history.

Task durations vary twenty-fold between reporters (a USA period takes minutes, a
small island seconds), so estimates are per (job, reporter): a reporter seen
before in the job predicts itself, otherwise the job's mean, otherwise the
recent mean across all jobs. Durations are claim -> finish of the attempt that
settled the task, i.e. busy time; ETAs are therefore "work left", and the free
tier's daily quota is layered on top where it is the real bottleneck.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field


def parse_ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


@dataclass
class Pace:
    by_reporter: dict[tuple[str, str], float] = field(default_factory=dict)
    by_job: dict[str, float] = field(default_factory=dict)
    recent: float | None = None

    def expect(self, job_id: str, reporter: str) -> tuple[float | None, str]:
        """Expected seconds for one task, and what the guess is based on."""
        if (v := self.by_reporter.get((job_id, reporter))) is not None:
            return v, "reporter"
        if (v := self.by_job.get(job_id)) is not None:
            return v, "job"
        if self.recent is not None:
            return self.recent, "recent"
        return None, ""


@dataclass(frozen=True)
class Finish:
    job_id: str
    start: float
    end: float
    rows: int

    @property
    def secs(self) -> float:
        return max(self.end - self.start, 0.0)


def finishes(rows: Iterable) -> list[Finish]:
    out = []
    for r in rows:
        start, end = parse_ts(r["claimed_at"]), parse_ts(r["finished_at"])
        if start is None or end is None:
            continue
        out.append(Finish(r["job_id"], start, end, int(r["records_fetched"] or 0)))
    return out


def build_pace(pace_rows: Iterable, recent: Sequence[Finish]) -> Pace:
    pace = Pace()
    job_tot: dict[str, tuple[float, int]] = {}
    for r in pace_rows:
        secs = r["secs"]
        if secs is None:
            continue
        secs = max(float(secs), 0.5)
        pace.by_reporter[(r["job_id"], str(r["reporter"]))] = secs
        t, n = job_tot.get(r["job_id"], (0.0, 0))
        job_tot[r["job_id"]] = (t + secs * r["n"], n + r["n"])
    pace.by_job = {j: t / n for j, (t, n) in job_tot.items() if n}
    tail = recent[-100:]
    if tail:
        pace.recent = max(sum(f.secs for f in tail) / len(tail), 0.5)
    return pace


def work_left(pace: Pace, backlog_rows: Iterable) -> dict[str, float | None]:
    """Busy seconds of work left per job; None where nothing predicts it yet."""
    out: dict[str, float | None] = {}
    for r in backlog_rows:
        job = r["job_id"]
        secs, _basis = pace.expect(job, str(r["reporter"]))
        if secs is None:
            out[job] = None
        elif job not in out or out[job] is not None:
            out[job] = (out.get(job) or 0.0) + secs * r["n"]
    return out


def quota_bound(
    busy_s: float,
    remaining: int,
    *,
    budget: int | None,
    used_today: int,
    reset_s: float,
) -> tuple[float, bool]:
    """Wall-clock ETA when the daily budget throttles the run. Returns (eta,
    quota_bound). Each task costs at least one call."""
    if not budget or remaining <= 0:
        return busy_s, False
    left_today = max(budget - used_today, 0)
    if remaining <= left_today:
        return busy_s, False
    per_task = busy_s / remaining if remaining else 0.0
    rest = remaining - left_today
    full_days = math.ceil(rest / budget) - 1
    tail = rest - full_days * budget
    eta = reset_s + full_days * 86400 + tail * per_task
    return max(eta, busy_s), True


def rates(recent: Sequence[Finish], now: float, window: float = 3600.0) -> tuple[float, float]:
    """(tasks/hour, rows/hour) over busy time in the last `window` seconds."""
    sel = [f for f in recent if f.end >= now - window]
    busy = sum(max(f.secs, 1.0) for f in sel)  # timestamps have one-second resolution
    if not sel or busy <= 0:
        return 0.0, 0.0
    return len(sel) / busy * 3600, sum(f.rows for f in sel) / busy * 3600


def series(recent: Sequence[Finish], now: float, window: float, points: int) -> list[float]:
    """Rows per minute in `points` equal buckets covering the last `window`
    seconds. Each task's rows are spread over its claim -> finish interval, so
    a three-minute call reads as three minutes of throughput, not one spike."""
    if points <= 0 or window <= 0:
        return []
    start = now - window
    width = window / points
    out = [0.0] * points
    for f in recent:
        if f.end < start or f.rows <= 0:
            continue
        a, b = max(f.start, start), min(max(f.end, f.start + 1.0), now)
        if b <= a:
            continue
        density = f.rows / max(f.end - f.start, 1.0)  # rows per second
        i0, i1 = int((a - start) / width), min(int((b - start) / width), points - 1)
        for i in range(i0, i1 + 1):
            lo, hi = start + i * width, start + (i + 1) * width
            overlap = min(b, hi) - max(a, lo)
            if overlap > 0:
                out[i] += density * overlap
    return [v / width * 60 for v in out]
