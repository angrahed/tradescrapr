"""Status reporting and job/dead-task management."""

from __future__ import annotations

import logging
import sys

from . import storage
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


def cmd_cancel_job(cfg: Config, *, job_id: str) -> int:
    """Stop a job running without deleting it: its unfinished tasks become
    cancelled so the worker skips them; history and data stay in place."""
    store = Store(cfg.paths.state_db)
    try:
        if store.get_job(job_id) is None:
            print(f"no such job: {job_id}", file=sys.stderr)
            return 1
        n = store.cancel_job(job_id)
    finally:
        store.close()
    print(f"cancelled {job_id}: {n} unfinished task(s) marked cancelled (data kept)")
    print("note: a task already in flight in a running worker may still finish")
    return 0


def cmd_delete_job(
    cfg: Config, *, job_id: str, remove_data: bool = False, assume_yes: bool = False
) -> int:
    """Remove a job and its tasks from the queue. With --data, also delete the
    parquet chunks this job wrote (located per task, so sibling jobs sharing a
    partition are left intact)."""
    store = Store(cfg.paths.state_db)
    try:
        if store.get_job(job_id) is None:
            print(f"no such job: {job_id}", file=sys.stderr)
            return 1
        progress = store.job_progress(job_id)
        total = progress[0].total if progress else 0
        outputs = store.job_task_outputs(job_id)

        if not assume_yes:
            if not sys.stdin.isatty():
                print("refusing to delete without confirmation; pass --yes", file=sys.stderr)
                return 2
            extra = f" and delete {len(outputs)} task(s)' data files" if remove_data else ""
            reply = input(f"delete job {job_id} ({total} task(s)){extra}? [y/N] ").strip().lower()
            if reply not in ("y", "yes"):
                print("aborted")
                return 1

        removed_files = 0
        if remove_data:
            for r in outputs:
                part = storage.partition_dir(
                    cfg.paths.data, r["dataset"], r["freq"], r["period"], r["reporter"]
                )
                removed_files += storage.remove_task_outputs(part, r["task_id"])

        n = store.delete_job(job_id)
    finally:
        store.close()
    tail = (
        f", {removed_files} data file(s) deleted"
        if remove_data
        else " (data files kept; re-run with --data to remove them)"
    )
    print(f"deleted {job_id}: {n} task(s) removed{tail}")
    return 0
