import datetime as dt

from tradescrapr.config import QuotaConfig
from tradescrapr.quota import QuotaLedger


def _ledger(store, clock, budget=3, reset="00:00", tz="UTC"):
    hh, mm = reset.split(":")
    cfg = QuotaConfig(daily_budget=budget, reset_time=dt.time(int(hh), int(mm)), reset_tz=tz)
    return QuotaLedger(store, cfg, clock=clock)


def test_reserve_until_budget_then_deny(store, clock):
    q = _ledger(store, clock, budget=3)
    assert [q.try_reserve() for _ in range(4)] == [True, True, True, False]
    assert q.used_today() == 3


def test_unlimited_budget_still_counts(store, clock):
    q = _ledger(store, clock, budget=None)
    assert all(q.try_reserve() for _ in range(5))
    assert q.used_today() == 5


def test_rollover_at_reset(store, clock):
    q = _ledger(store, clock, budget=2)
    assert q.try_reserve() and q.try_reserve() and not q.try_reserve()
    clock.advance(days=1)
    assert q.try_reserve()  # fresh day, fresh budget
    assert q.used_today() == 1


def test_non_utc_reset_boundary(store, clock):
    # Reset at 09:00 New York time. At 12:00 UTC (08:00 EDT) we're still in the
    # PREVIOUS quota day; crossing 13:00 UTC (09:00 EDT) starts a new one.
    q = _ledger(store, clock, budget=1, reset="09:00", tz="America/New_York")
    day1 = q.quota_date()
    assert q.try_reserve() and not q.try_reserve()
    clock.advance(hours=2)  # 14:00 UTC = 10:00 EDT -> past the reset
    assert q.quota_date() != day1
    assert q.try_reserve()


def test_force_exhaust_resyncs(store, clock):
    q = _ledger(store, clock, budget=5)
    assert q.try_reserve()
    q.force_exhaust()
    assert q.used_today() == 5
    assert not q.try_reserve()


def test_seconds_until_reset(store, clock):
    q = _ledger(store, clock, budget=1)  # reset 00:00 UTC, now 12:00 UTC
    assert q.seconds_until_reset() == 12 * 3600
