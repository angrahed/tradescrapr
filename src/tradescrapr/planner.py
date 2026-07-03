"""Job specs, task expansion, and the truncation split ladder.

A job spec (YAML) expands into tasks of one reporter x one period x one flow,
all partners, at the requested commodity detail. That granularity is the natural
resume unit, matches getDA's reporter-period grain, and maps 1:1 onto parquet
partitions; the few reporter-periods too big for one call are split on demand.

Split ladder for truncated responses: flow -> partner groups (right-sized to
the expected row count when known) -> commodity codes (chapter-batched, then
bisected). First applicable dimension wins; children that still truncate
re-split recursively.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from .availability import ReleaseRow
from .errors import CannotSplit, ConfigError
from .refs import Refs

log = logging.getLogger(__name__)

DATASETS = ("final", "tariffline")


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# TaskParams: the unit of work, serialised into the tasks table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskParams:
    dataset: str
    type_code: str
    freq: str
    cl_code: str
    reporter: str  # single numeric code, always
    period: str  # single period, always
    flows: tuple[str, ...]  # >=1; split dimension 1
    cmd_level: int | None  # target detail when cmd_codes is None (AG{level})
    cmd_codes: tuple[str, ...] | None  # explicit codes; split dimension 2
    partners: tuple[str, ...] | None  # None = all partners; split dimension 3
    # 'classic' breakdown pins these to the totals ("0"/"C00"/"0"); None = full
    # breakdown by second partner / customs procedure / transport mode (~10x rows)
    partner2: str | None = "0"
    customs: str | None = "C00"
    mot: str | None = "0"
    include_desc: bool = False
    kind: str = "get"  # get | bulk

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "dataset": self.dataset,
            "type_code": self.type_code,
            "freq": self.freq,
            "cl_code": self.cl_code,
            "reporter": self.reporter,
            "period": self.period,
            "flows": sorted(self.flows),
            "cmd_level": self.cmd_level,
            "cmd_codes": sorted(self.cmd_codes) if self.cmd_codes is not None else None,
            "partners": sorted(self.partners) if self.partners is not None else None,
            "partner2": self.partner2,
            "customs": self.customs,
            "mot": self.mot,
            "include_desc": self.include_desc,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    def params_hash(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TaskParams:
        return cls(
            kind=d.get("kind", "get"),
            dataset=d["dataset"],
            type_code=d["type_code"],
            freq=d["freq"],
            cl_code=d["cl_code"],
            reporter=str(d["reporter"]),
            period=str(d["period"]),
            flows=tuple(d["flows"]),
            cmd_level=d.get("cmd_level"),
            cmd_codes=tuple(d["cmd_codes"]) if d.get("cmd_codes") is not None else None,
            partners=tuple(d["partners"]) if d.get("partners") is not None else None,
            partner2=d.get("partner2", "0"),
            customs=d.get("customs", "C00"),
            mot=d.get("mot", "0"),
            include_desc=bool(d.get("include_desc", False)),
        )

    def api_query(self) -> dict[str, Any]:
        """Query params for /data/v1/get*. Omitted partnerCode = all partners."""
        q: dict[str, Any] = {
            "reporterCode": self.reporter,
            "period": self.period,
            "flowCode": ",".join(self.flows),
        }
        if self.cmd_codes is not None:
            q["cmdCode"] = ",".join(self.cmd_codes)
        elif self.cmd_level is not None:
            q["cmdCode"] = f"AG{self.cmd_level}"
        else:
            q["cmdCode"] = "TOTAL"
        if self.partners is not None:
            q["partnerCode"] = ",".join(self.partners)
        if self.partner2 is not None:
            q["partner2Code"] = self.partner2
        if self.customs is not None:
            q["customsCode"] = self.customs
        if self.mot is not None:
            q["motCode"] = self.mot
        if self.include_desc:
            q["includeDesc"] = "true"
        return q

    def describe(self) -> str:
        cmd = (
            f"{len(self.cmd_codes)} codes"
            if self.cmd_codes is not None
            else (f"AG{self.cmd_level}" if self.cmd_level is not None else "TOTAL")
        )
        partners = "all" if self.partners is None else f"{len(self.partners)} partners"
        full = "" if self.partner2 is not None else " breakdown=full"
        return (
            f"{self.dataset} {self.type_code}/{self.freq}/{self.cl_code} "
            f"reporter={self.reporter} period={self.period} "
            f"flows={','.join(self.flows)} cmd={cmd} partners={partners}{full}"
        )


# ---------------------------------------------------------------------------
# Job specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobSpec:
    name: str
    dataset: str
    type_code: str
    freq: str
    classification: str
    reporters: tuple[str, ...] | str  # tuple of idents, or the string "all"
    partners: tuple[str, ...] | None
    flows: tuple[str, ...]
    cmd_level: int | None
    cmd_codes: tuple[str, ...] | None
    periods: tuple[str, ...]  # raw tokens, e.g. ("2015:2024",)
    breakdown: str  # classic (partner2/customs/mot pinned to totals) | full
    include_desc: bool
    use_availability: bool  # restrict to reporter-periods that exist per getDA
    raw: dict[str, Any]  # as loaded, for hashing and provenance

    def spec_hash8(self) -> str:
        return hashlib.sha256(_canonical_json(self.raw).encode()).hexdigest()[:8]

    def job_id(self) -> str:
        return f"tgt-{self.name}-{self.spec_hash8()}"

    def to_json(self) -> str:
        return _canonical_json(self.raw)


def load_spec(path: Path) -> JobSpec:
    with open(path) as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: job spec must be a YAML mapping")

    def req(key: str) -> Any:
        if key not in raw:
            raise ConfigError(f"{path}: missing required key {key!r}")
        return raw[key]

    name = str(req("name")).strip()
    if not name or any(c in name for c in " /\\"):
        raise ConfigError(f"{path}: name must be a slug without spaces or slashes")

    dataset = str(raw.get("dataset", "final"))
    if dataset not in DATASETS:
        raise ConfigError(f"{path}: dataset must be one of {DATASETS}, got {dataset!r}")

    freq = str(raw.get("freq", "A")).upper()
    if freq not in ("A", "M"):
        raise ConfigError(f"{path}: freq must be A or M, got {freq!r}")

    reporters_raw = req("reporters")
    reporters: tuple[str, ...] | str
    if isinstance(reporters_raw, str) and reporters_raw.lower() == "all":
        reporters = "all"
    elif isinstance(reporters_raw, (list, tuple)):
        reporters = tuple(str(r) for r in reporters_raw)
        if not reporters:
            raise ConfigError(f"{path}: reporters list is empty")
    else:
        reporters = (str(reporters_raw),)

    partners_raw = raw.get("partners", "all")
    if isinstance(partners_raw, str) and partners_raw.lower() == "all":
        partners = None
    elif partners_raw is None:
        partners = None
    else:
        partners = tuple(str(p) for p in partners_raw)

    flows_raw = raw.get("flows", ["M", "X"])
    flows = tuple(str(f).upper() for f in flows_raw)
    if not flows:
        raise ConfigError(f"{path}: flows list is empty")

    commodities = raw.get("commodities") or {}
    if not isinstance(commodities, dict):
        raise ConfigError(f"{path}: commodities must be a mapping with level/codes")
    codes_raw = commodities.get("codes")
    cmd_codes = tuple(str(c) for c in codes_raw) if codes_raw else None
    cmd_level = int(commodities["level"]) if commodities.get("level") is not None else None
    if cmd_codes is not None:
        cmd_level = None  # explicit codes win

    periods_raw = req("periods")
    if isinstance(periods_raw, (list, tuple)):
        periods = tuple(str(p) for p in periods_raw)
    else:
        periods = tuple(t.strip() for t in str(periods_raw).split(",") if t.strip())

    breakdown = str(raw.get("breakdown", "classic")).lower()
    if breakdown not in ("classic", "full"):
        raise ConfigError(f"{path}: breakdown must be classic|full, got {breakdown!r}")

    return JobSpec(
        name=name,
        dataset=dataset,
        type_code=str(raw.get("type", "C")).upper(),
        freq=freq,
        classification=str(raw.get("classification", "HS")),
        reporters=reporters,
        partners=partners,
        flows=flows,
        cmd_level=cmd_level,
        cmd_codes=cmd_codes,
        periods=periods,
        breakdown=breakdown,
        include_desc=bool(raw.get("include_desc", False)),
        use_availability=bool(raw.get("use_availability", True)),
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Period expansion
# ---------------------------------------------------------------------------


def _iter_months(start: str, end: str) -> list[str]:
    y, m = int(start[:4]), int(start[4:6])
    ey, em = int(end[:4]), int(end[4:6])
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y}{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _validate_period(tok: str, freq: str) -> str:
    if freq == "A":
        if len(tok) != 4 or not tok.isdigit():
            raise ConfigError(f"annual period must be YYYY, got {tok!r}")
    else:
        if len(tok) != 6 or not tok.isdigit() or not 1 <= int(tok[4:6]) <= 12:
            raise ConfigError(f"monthly period must be YYYYMM, got {tok!r}")
    return tok


def parse_periods(tokens: tuple[str, ...] | list[str] | str, freq: str) -> list[str]:
    """Expand period tokens ('2015:2024', '202001:202412', '2019', mixed lists)
    into a sorted list of explicit periods."""
    if isinstance(tokens, str):
        tokens = [t.strip() for t in tokens.split(",") if t.strip()]
    out: set[str] = set()
    for tok in tokens:
        tok = str(tok).strip()
        if ":" in tok:
            lo, hi = (t.strip() for t in tok.split(":", 1))
            _validate_period(lo, freq)
            _validate_period(hi, freq)
            if lo > hi:
                raise ConfigError(f"period range start > end: {tok!r}")
            if freq == "A":
                out.update(str(y) for y in range(int(lo), int(hi) + 1))
            else:
                out.update(_iter_months(lo, hi))
        else:
            out.add(_validate_period(tok, freq))
    if not out:
        raise ConfigError("no periods after expansion")
    return sorted(out)


# ---------------------------------------------------------------------------
# Expansion
# ---------------------------------------------------------------------------


def expand(
    spec: JobSpec,
    refs: Refs,
    *,
    combine_flows: bool = False,
    availability: list[ReleaseRow] | None = None,
) -> list[TaskParams]:
    """Expand a spec into TaskParams. When availability rows are given, only
    (reporter, period) combos that actually exist are generated — with
    reporters: all this saves thousands of empty calls."""
    periods = parse_periods(spec.periods, spec.freq)
    period_set = set(periods)

    if availability is not None:
        combos = sorted(
            {(row.reporter_code, row.period) for row in availability if row.period in period_set},
            key=lambda c: (c[1], int(c[0])),
        )
        if spec.reporters != "all":
            wanted = {refs.reporter_code(r) for r in spec.reporters}
            combos = [c for c in combos if c[0] in wanted]
        n_full = (
            len(periods) * len(spec.reporters)
            if spec.reporters != "all"
            else len(periods) * len(refs.all_reporter_codes())
        )
        log.info(
            "expand: %d reporter-periods exist per getDA (blind cartesian would be %d)",
            len(combos),
            n_full,
        )
    else:
        if spec.reporters == "all":
            rep_codes = refs.all_reporter_codes()
        else:
            rep_codes = [refs.reporter_code(r) for r in spec.reporters]
        combos = [(rep, per) for per in periods for rep in sorted(rep_codes, key=int)]

    partners = (
        tuple(sorted((refs.partner_code(p) for p in spec.partners), key=int))
        if spec.partners is not None
        else None
    )
    flow_groups: list[tuple[str, ...]] = (
        [spec.flows] if combine_flows else [(f,) for f in spec.flows]
    )
    classic = spec.breakdown == "classic"

    return [
        TaskParams(
            dataset=spec.dataset,
            type_code=spec.type_code,
            freq=spec.freq,
            cl_code=spec.classification,
            reporter=rep,
            period=per,
            flows=fg,
            cmd_level=spec.cmd_level,
            cmd_codes=spec.cmd_codes,
            partners=partners,
            partner2="0" if classic else None,
            customs="C00" if classic else None,
            mot="0" if classic else None,
            include_desc=spec.include_desc,
        )
        for (rep, per) in combos
        for fg in flow_groups
    ]


# ---------------------------------------------------------------------------
# Split ladder
# ---------------------------------------------------------------------------


def _batch_cmd_codes(codes: list[str], max_per_call: int) -> list[tuple[str, ...]]:
    """Group codes by 2-digit chapter, then greedily pack whole chapters into
    batches of <= max_per_call; oversized chapters are sliced."""
    chapters: dict[str, list[str]] = {}
    for c in codes:
        chapters.setdefault(c[:2], []).append(c)
    batches: list[tuple[str, ...]] = []
    current: list[str] = []
    for _, group in sorted(chapters.items()):
        if len(group) > max_per_call:
            if current:
                batches.append(tuple(current))
                current = []
            batches.extend(
                tuple(group[i : i + max_per_call]) for i in range(0, len(group), max_per_call)
            )
        elif len(current) + len(group) > max_per_call:
            batches.append(tuple(current))
            current = list(group)
        else:
            current.extend(group)
    if current:
        batches.append(tuple(current))
    return batches


def _bisect(items: tuple[str, ...]) -> list[tuple[str, ...]]:
    mid = len(items) // 2
    return [items[:mid], items[mid:]]


SPLIT_TARGET_FILL = 0.75  # aim each child at ~75% of the record cap
DEFAULT_PARTNER_GROUPS = 4  # first partner split when no size estimate exists


def _partner_groups(codes: tuple[str, ...], n: int) -> list[tuple[str, ...]]:
    """Interleaved assignment (every n-th code) so heavy partners spread across
    groups instead of clustering in one contiguous slice."""
    n = max(2, min(n, len(codes)))
    return [tuple(codes[i::n]) for i in range(n)]


def split(
    params: TaskParams,
    refs: Refs,
    *,
    max_cmd_codes: int = 100,
    est_rows: float | None = None,
    cap: int = 100_000,
) -> list[TaskParams]:
    """Split a truncated task: flow -> partner groups -> commodity batches.

    Partners are the cheap split dimension: a partner list keeps the URL short,
    so the group count can be sized to the expected response (est_rows, derived
    from the free availability endpoint) instead of a fixed fan-out. Commodity
    chapter-batching is capped at max_cmd_codes per call by URL length (70+
    calls for full HS6), so it is only the fallback once a task is down to a
    single partner. Children that still truncate re-split recursively.
    Raises CannotSplit at the floor."""
    if params.kind != "get":
        raise CannotSplit(f"cannot split a {params.kind} task")

    if len(params.flows) > 1:
        return [replace(params, flows=(f,)) for f in params.flows]

    if params.partners is None:
        allp = tuple(sorted(refs.all_partner_codes(include_world=True), key=int))
        if est_rows and cap > 0:
            n = math.ceil(est_rows / (SPLIT_TARGET_FILL * cap))
        else:
            n = DEFAULT_PARTNER_GROUPS
        return [replace(params, partners=g) for g in _partner_groups(allp, n)]
    if len(params.partners) > 1:
        return [replace(params, partners=half) for half in _bisect(params.partners)]

    if params.cmd_codes is None and params.cmd_level is not None:
        codes = refs.cmd_codes(params.cl_code, params.cmd_level)
        return [
            replace(params, cmd_level=None, cmd_codes=batch)
            for batch in _batch_cmd_codes(codes, max_cmd_codes)
        ]
    if params.cmd_codes is not None and len(params.cmd_codes) > 1:
        return [replace(params, cmd_codes=half) for half in _bisect(params.cmd_codes)]

    raise CannotSplit(
        f"unsplittable truncation (single flow/commodity/partner): {params.describe()}"
    )
