"""Shared fixtures: offline config, state store, canned reference data, frozen clock."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tradescrapr.config import Config, load_config
from tradescrapr.refs import Refs
from tradescrapr.state import Store


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> Config:
    (tmp_path / "config.yaml").write_text(
        """
tier: free
api: {max_records: 100, rps: 1000}
quota: {daily_budget: 10}
retry: {task_max_attempts: 3, backoff_base_s: 0.01, backoff_cap_s: 0.02, stale_minutes: 120}
"""
    )
    monkeypatch.setenv("TRADESCRAPR_ROOT", str(tmp_path))
    monkeypatch.delenv("COMTRADE_API_KEY", raising=False)
    monkeypatch.delenv("COMTRADE_KEY", raising=False)
    config = load_config(root=tmp_path)
    config.paths.ensure()
    return config


@pytest.fixture
def store(cfg) -> Store:
    s = Store(cfg.paths.state_db)
    yield s
    s.close()


class _NoNetworkClient:
    """Refs must never hit the network in unit tests."""

    def get_reference(self, filename):  # pragma: no cover
        raise AssertionError(f"unexpected network fetch of {filename}")


REPORTERS = [
    {"reporterCode": 8, "reporterCodeIsoAlpha3": "ALB", "isGroup": False},
    {"reporterCode": 276, "reporterCodeIsoAlpha3": "DEU", "isGroup": False},
    {"reporterCode": 352, "reporterCodeIsoAlpha3": "ISL", "isGroup": False},
    {"reporterCode": 97, "reporterCodeIsoAlpha3": None, "isGroup": True},  # EU
]

PARTNERS = [
    {"PartnerCode": 0, "PartnerCodeIsoAlpha3": "W00", "isGroup": True},  # World
    {"PartnerCode": 4, "PartnerCodeIsoAlpha3": "AFG", "isGroup": False},
    {"PartnerCode": 8, "PartnerCodeIsoAlpha3": "ALB", "isGroup": False},
    {"PartnerCode": 76, "PartnerCodeIsoAlpha3": "BRA", "isGroup": False},
    {"PartnerCode": 251, "PartnerCodeIsoAlpha3": "FRA", "isGroup": False},
]

HS = [
    {"id": "TOTAL", "aggrLevel": 0},
    {"id": "01", "aggrLevel": 2},
    {"id": "02", "aggrLevel": 2},
    {"id": "0101", "aggrLevel": 4},
    {"id": "010121", "aggrLevel": 6},
    {"id": "010129", "aggrLevel": 6},
    {"id": "020110", "aggrLevel": 6},
    {"id": "020120", "aggrLevel": 6},
    {"id": "020130", "aggrLevel": 6},
]


@pytest.fixture
def refs(cfg) -> Refs:
    now = dt.datetime.now(dt.UTC).isoformat()
    for name, results in (
        ("Reporters.json", REPORTERS),
        ("partnerAreas.json", PARTNERS),
        ("HS.json", HS),
    ):
        (cfg.paths.refs / name).write_text(json.dumps({"fetched_at_utc": now, "results": results}))
    return Refs(_NoNetworkClient(), cfg.paths.refs)


class FrozenClock:
    def __init__(self, start: dt.datetime):
        self.now = start

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += dt.timedelta(**kwargs)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.UTC))
