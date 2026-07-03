"""Daily call-quota ledger.

Persisted in SQLite so restarts and multi-day jobs keep an honest count. The
quota day boundary sits at the configured reset instant (default 00:00 UTC):
quota_date = date of (now - reset offset) in the reset timezone.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable

from .config import QuotaConfig
from .state import Store

log = logging.getLogger(__name__)


class QuotaLedger:
    def __init__(
        self,
        store: Store,
        cfg: QuotaConfig,
        clock: Callable[[], dt.datetime] | None = None,
    ):
        self.store = store
        self.cfg = cfg
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))

    def quota_date(self, now: dt.datetime | None = None) -> str:
        local = (now or self._clock()).astimezone(self.cfg.tzinfo)
        offset = dt.timedelta(hours=self.cfg.reset_time.hour, minutes=self.cfg.reset_time.minute)
        return (local - offset).date().isoformat()

    def try_reserve(self) -> bool:
        """Reserve one call against today's budget. Unlimited budgets always
        succeed but are still counted (burn-rate stats)."""
        return self.store.quota_try_reserve(self.quota_date(), self.cfg.daily_budget)

    def force_exhaust(self) -> None:
        """The server said the quota is gone — resync the local ledger to its truth."""
        log.warning("quota: server-side exhaustion, marking local budget spent")
        self.store.quota_force_exhaust(self.quota_date(), self.cfg.daily_budget)

    def used_today(self) -> int:
        return self.store.quota_used(self.quota_date())

    def seconds_until_reset(self, now: dt.datetime | None = None) -> float:
        local = (now or self._clock()).astimezone(self.cfg.tzinfo)
        reset = self.cfg.reset_time
        today_reset = local.replace(hour=reset.hour, minute=reset.minute, second=0, microsecond=0)
        nxt = today_reset if today_reset > local else today_reset + dt.timedelta(days=1)
        return max((nxt - local).total_seconds(), 0.0)
