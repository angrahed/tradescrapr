"""Status reporting and dead-task management."""

from __future__ import annotations

import logging

from .config import Config
from .quota import QuotaLedger
from .state import Store

log = logging.getLogger(__name__)

_STATUSES = ("pending", "in_progress", "done", "split", "failed", "dead", "cancelled")


def cmd_status(cfg: Config, *, job_id: str | None = None, verbose: bool = False) -> int:
    store = Store(cfg.paths.state_db)
    try:
        jobs = store.job_progress(job_id)
        quota = QuotaLedger(store, cfg.quota)
        used = quota.used_today()
        budget = cfg.quota.daily_budget

        if not jobs:
            print("no jobs" + (f" matching {job_id!r}" if job_id else ""))
            return 0

        header = f"{'job':<40} {'total':>6} " + " ".join(f"{s:>11}" for s in _STATUSES)
        print(header + f" {'records':>12}  progress")
        for jp in jobs:
            counts = " ".join(f"{jp.counts.get(s, 0):>11}" for s in _STATUSES)
            print(f"{jp.job_id:<40} {jp.total:>6} {counts} {jp.records:>12,}  {jp.pct_done:5.1f}%")

        remaining = sum(jp.remaining for jp in jobs)
        print()
        if budget:
            print(f"quota today: {used}/{budget} calls")
            if remaining:
                days = remaining / budget
                print(
                    f"remaining:   {remaining} task(s) ≈ {days:.1f} day(s) at current budget "
                    "(truncation splits may add more)"
                )
        else:
            print(f"quota today: {used} calls (unlimited budget)")
            if remaining:
                print(f"remaining:   {remaining} task(s)")

        if verbose:
            dead = store.dead_tasks(job_id)
            if dead:
                print(f"\ndead tasks ({len(dead)}):")
                for r in dead:
                    print(
                        f"  {r['task_id']}  {r['job_id']}  {r['dataset']}/{r['freq']} "
                        f"period={r['period']} reporter={r['reporter']} "
                        f"attempts={r['attempts']}\n    {r['last_error']}"
                    )
        return 0
    finally:
        store.close()


def cmd_retry_dead(cfg: Config, *, job_id: str | None = None, task_id: str | None = None) -> int:
    store = Store(cfg.paths.state_db)
    try:
        n = store.retry_dead(job_id=job_id, task_id=task_id)
    finally:
        store.close()
    print(f"revived {n} dead task(s)")
    return 0
