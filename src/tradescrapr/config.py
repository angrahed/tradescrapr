"""Configuration loading.

Precedence: CLI flags > TRADESCRAPR_* env vars > config.yaml > built-in defaults.
Secrets come from .env at the project root (COMTRADE_KEY), never from config.yaml.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

from .errors import ConfigError

log = logging.getLogger(__name__)

API_BASE = "https://comtradeapi.un.org"

TIERS = ("free", "premium")
TIER_DEFAULTS: dict[str, dict[str, Any]] = {
    "free": {"rps": 1.0, "max_records": 100_000, "daily_budget": 450},
    "premium": {"rps": 4.0, "max_records": 250_000, "daily_budget": None},
}

_KNOWN_TOP_KEYS = {"tier", "api", "quota", "retry", "planner", "paths", "monitor"}

# env var -> (section, key, parser); section None = top level
_ENV_OVERRIDES: dict[str, tuple[str | None, str, Any]] = {
    "TRADESCRAPR_TIER": (None, "tier", str),
    "TRADESCRAPR_RPS": ("api", "rps", float),
    "TRADESCRAPR_MAX_RECORDS": ("api", "max_records", int),
    "TRADESCRAPR_DAILY_BUDGET": ("quota", "daily_budget", int),
    "TRADESCRAPR_DATA_DIR": ("paths", "data", str),
    "TRADESCRAPR_STATE_DB": ("paths", "state_db", str),
    "TRADESCRAPR_LOGS_DIR": ("paths", "logs", str),
}


@dataclass(frozen=True)
class ApiConfig:
    rps: float
    max_records: int
    timeout_s: float


@dataclass(frozen=True)
class QuotaConfig:
    daily_budget: int | None  # None = unlimited (still counted)
    reset_time: dt.time
    reset_tz: str

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.reset_tz)


@dataclass(frozen=True)
class RetryConfig:
    transport_retries: int
    backoff_factor: float
    task_max_attempts: int
    backoff_base_s: float
    backoff_cap_s: float
    max_split_depth: int
    stale_minutes: int


@dataclass(frozen=True)
class PlannerConfig:
    max_cmd_codes_per_call: int
    combine_flows: bool


@dataclass(frozen=True)
class PathsConfig:
    root: Path
    data: Path
    state_db: Path
    refs: Path
    logs: Path

    @property
    def state_dir(self) -> Path:
        return self.state_db.parent

    def ensure(self) -> None:
        for d in (self.data, self.state_dir, self.refs, self.logs):
            d.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class MonitorConfig:
    datasets: tuple[str, ...]
    type_code: str
    freq: str
    classification: str
    flows: tuple[str, ...]
    cmd_level: int
    breakdown: str  # classic | full
    start_period: str | None
    wait_for_quota: bool
    reporters: tuple[str, ...] | None  # optional restriction, e.g. for drills


@dataclass(frozen=True)
class Config:
    tier: str
    api: ApiConfig
    quota: QuotaConfig
    retry: RetryConfig
    planner: PlannerConfig
    paths: PathsConfig
    monitor: MonitorConfig
    api_key: str | None

    def require_key(self) -> str:
        if not self.api_key:
            raise ConfigError(
                "no API key found. Set the COMTRADE_API_KEY environment variable, or copy "
                f".env.example to .env at the project root ({self.paths.root}) and fill in "
                "your subscription key. Get a free key at https://comtradeplus.un.org "
                "(see README, 'API key')."
            )
        return self.api_key


def find_project_root(start: Path | None = None) -> Path:
    """TRADESCRAPR_ROOT env var, else nearest ancestor of cwd holding config.yaml,
    else the source checkout this package lives in, else cwd."""
    env = os.getenv("TRADESCRAPR_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    cur = (start or Path.cwd()).resolve()
    for cand in (cur, *cur.parents):
        if (cand / "config.yaml").exists():
            return cand
    pkg_root = Path(__file__).resolve().parents[2]  # src/tradescrapr/ -> project root
    if (pkg_root / "pyproject.toml").exists():
        return pkg_root
    return cur


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping, got {type(data).__name__}")
    unknown = set(data) - _KNOWN_TOP_KEYS
    if unknown:
        log.warning("config: ignoring unknown top-level keys %s in %s", sorted(unknown), path)
    return data


def _apply_env(raw: dict[str, Any]) -> None:
    for var, (section, key, parse) in _ENV_OVERRIDES.items():
        val = os.getenv(var)
        if val is None or val == "":
            continue
        parsed = parse(val)
        if section is None:
            raw[key] = parsed
        else:
            raw.setdefault(section, {})[key] = parsed


def _apply_cli(raw: dict[str, Any], overrides: dict[str, Any]) -> None:
    """Overrides use dotted keys, e.g. {'quota.daily_budget': 100, 'tier': 'premium'}."""
    for dotted, val in overrides.items():
        if val is None:
            continue
        parts = dotted.split(".")
        node = raw
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = val


def _parse_reset_time(s: str) -> dt.time:
    try:
        hh, mm = s.split(":")
        return dt.time(int(hh), int(mm))
    except (ValueError, AttributeError) as e:
        raise ConfigError(f"quota.reset_time must be 'HH:MM', got {s!r}") from e


def _tuple_or_none(val: Any) -> tuple[str, ...] | None:
    if val is None:
        return None
    if isinstance(val, str):
        return (val,)
    return tuple(str(v) for v in val)


def load_config(root: Path | None = None, cli_overrides: dict[str, Any] | None = None) -> Config:
    root = (root or find_project_root()).resolve()
    raw = _read_yaml(root / "config.yaml")
    _apply_env(raw)
    _apply_cli(raw, cli_overrides or {})

    tier = str(raw.get("tier") or "free").lower()
    if tier not in TIERS:
        raise ConfigError(f"tier must be one of {TIERS}, got {tier!r}")
    tdef = TIER_DEFAULTS[tier]

    api_raw = raw.get("api") or {}
    api = ApiConfig(
        rps=float(api_raw.get("rps") or tdef["rps"]),
        max_records=int(api_raw.get("max_records") or tdef["max_records"]),
        timeout_s=float(api_raw.get("timeout_s") or 120),
    )

    quota_raw = raw.get("quota") or {}
    # null/missing -> tier default (free 450, premium unlimited); 0 or negative -> error
    budget_raw = quota_raw.get("daily_budget")
    budget = tdef["daily_budget"] if budget_raw is None else int(budget_raw)
    if budget is not None and budget <= 0:
        raise ConfigError(f"quota.daily_budget must be positive or null, got {budget}")
    quota = QuotaConfig(
        daily_budget=budget,
        reset_time=_parse_reset_time(str(quota_raw.get("reset_time") or "00:00")),
        reset_tz=str(quota_raw.get("reset_tz") or "UTC"),
    )
    ZoneInfo(quota.reset_tz)  # validate eagerly

    retry_raw = raw.get("retry") or {}
    retry = RetryConfig(
        transport_retries=int(retry_raw.get("transport_retries") or 5),
        backoff_factor=float(retry_raw.get("backoff_factor") or 1.5),
        task_max_attempts=int(retry_raw.get("task_max_attempts") or 5),
        backoff_base_s=float(retry_raw.get("backoff_base_s") or 5),
        backoff_cap_s=float(retry_raw.get("backoff_cap_s") or 300),
        max_split_depth=int(retry_raw.get("max_split_depth") or 8),
        stale_minutes=int(retry_raw.get("stale_minutes") or 120),
    )

    planner_raw = raw.get("planner") or {}
    planner = PlannerConfig(
        max_cmd_codes_per_call=int(planner_raw.get("max_cmd_codes_per_call") or 100),
        combine_flows=bool(planner_raw.get("combine_flows", False)),
    )

    paths_raw = raw.get("paths") or {}

    def _path(key: str, default: str) -> Path:
        p = Path(str(paths_raw.get(key) or default))
        return p if p.is_absolute() else root / p

    paths = PathsConfig(
        root=root,
        data=_path("data", "data/raw/comtrade"),
        state_db=_path("state_db", "state/tradescrapr.sqlite3"),
        refs=_path("refs", "state/refs"),
        logs=_path("logs", "logs"),
    )

    mon_raw = raw.get("monitor") or {}
    datasets = _tuple_or_none(mon_raw.get("datasets")) or ("final",)
    for ds in datasets:
        if ds not in ("final", "tariffline"):
            raise ConfigError(f"monitor.datasets entries must be final|tariffline, got {ds!r}")
    breakdown = str(mon_raw.get("breakdown") or "classic").lower()
    if breakdown not in ("classic", "full"):
        raise ConfigError(f"monitor.breakdown must be classic|full, got {breakdown!r}")
    start_period = mon_raw.get("start_period")
    monitor = MonitorConfig(
        datasets=datasets,
        type_code=str(mon_raw.get("type") or "C"),
        freq=str(mon_raw.get("freq") or "M"),
        classification=str(mon_raw.get("classification") or "HS"),
        flows=_tuple_or_none(mon_raw.get("flows")) or ("M", "X"),
        cmd_level=int(mon_raw.get("cmd_level") or 6),
        breakdown=breakdown,
        start_period=str(start_period) if start_period is not None else None,
        wait_for_quota=bool(mon_raw.get("wait_for_quota", False)),
        reporters=_tuple_or_none(mon_raw.get("reporters")),
    )

    # Key resolution: COMTRADE_API_KEY (preferred) or legacy COMTRADE_KEY, from the
    # process environment or a .env file at the project root (process env wins for
    # the same name — load_dotenv never overrides existing variables).
    load_dotenv(root / ".env")
    api_key = os.getenv("COMTRADE_API_KEY") or os.getenv("COMTRADE_KEY") or None

    return Config(
        tier=tier,
        api=api,
        quota=quota,
        retry=retry,
        planner=planner,
        paths=paths,
        monitor=monitor,
        api_key=api_key,
    )
