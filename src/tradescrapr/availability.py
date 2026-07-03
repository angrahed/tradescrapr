"""Data-availability (getDA) fetching and parsing.

The free getDA endpoints list, per dataset, every (reporter, period) that exists,
with its publication timestamps. This drives both the monthly monitor (diff vs the
releases table) and 'all reporters' expansion in the planner (only enumerate
reporter-periods that actually have data).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .client import ComtradeClient
from .config import Config
from .refs import Refs

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReleaseRow:
    dataset: str
    type_code: str
    freq: str
    classification: str  # search code requested, e.g. HS
    reporter_code: str
    period: str
    last_released: str  # ISO timestamp string as published by the API
    total_records: int | None


def fetch_availability(
    client: ComtradeClient,
    *,
    dataset: str,
    type_code: str,
    freq: str,
    classification: str,
    period: str | None = None,
    reporter_code: str | None = None,
) -> list[ReleaseRow]:
    """Fetch and normalise getDA rows. Deduplicates on (reporter, period), keeping
    the most recent lastReleased."""
    raw = client.get_availability(
        dataset=dataset,
        type_code=type_code,
        freq=freq,
        cl_code=classification,
        period=period,
        reporter_code=reporter_code,
    )
    best: dict[tuple[str, str], ReleaseRow] = {}
    for r in raw:
        row = ReleaseRow(
            dataset=dataset,
            type_code=type_code,
            freq=freq,
            classification=classification,
            reporter_code=str(r.get("reporterCode")),
            period=str(r.get("period")),
            last_released=str(r.get("lastReleased") or r.get("firstReleased") or ""),
            total_records=r.get("totalRecords"),
        )
        key = (row.reporter_code, row.period)
        if key not in best or row.last_released > best[key].last_released:
            best[key] = row
    rows = sorted(best.values(), key=lambda r: (r.period, int(r.reporter_code)))
    log.debug(
        "availability: %s %s/%s/%s -> %d rows (%d raw)",
        dataset,
        type_code,
        freq,
        classification,
        len(rows),
        len(raw),
    )
    return rows


def cmd_da(
    cfg: Config,
    *,
    type_code: str,
    freq: str,
    classification: str,
    period: str | None,
    reporter: str | None,
) -> int:
    client = ComtradeClient(cfg)
    reporter_code = None
    if reporter:
        reporter_code = Refs(client, cfg.paths.refs).reporter_code(reporter)
    rows = fetch_availability(
        client,
        dataset="final",
        type_code=type_code,
        freq=freq,
        classification=classification,
        period=period,
        reporter_code=reporter_code,
    )
    print(f"{'period':>8}  {'reporter':>8}  {'records':>12}  lastReleased")
    for r in rows:
        recs = f"{r.total_records:,}" if r.total_records is not None else "?"
        print(f"{r.period:>8}  {r.reporter_code:>8}  {recs:>12}  {r.last_released}")
    reporters = {r.reporter_code for r in rows}
    print(f"-- {len(rows)} rows, {len(reporters)} reporters")
    return 0
