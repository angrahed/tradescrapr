"""Monthly monitor: scrape newly published (or re-published) reporter-periods.

One run: poll the free getDA endpoint -> diff against the releases table
(new = unseen reporter-period; revised = newer lastReleased) -> enqueue tasks
into a fresh monitor job (bulk tasks on premium) -> drain the monitor queue ->
finalize releases whose tasks all settled. A release only flips to 'done' when
fully scraped, so interrupted runs resume on the next cron tick; a revision
supersedes any still-queued tasks for that reporter-period and rewrites the
partition from scratch.
"""

from __future__ import annotations

import datetime as dt
import logging
from contextlib import ExitStack

from ..availability import ReleaseRow, fetch_availability
from ..client import ComtradeClient
from ..config import Config
from ..livestatus import STATUS_FILE, StatusBoard
from ..planner import TaskParams
from ..quota import QuotaLedger
from ..refs import Refs
from ..state import Store
from ..worker import Worker, worker_lock

log = logging.getLogger(__name__)


def _monitor_tasks(cfg: Config, row: ReleaseRow) -> list[TaskParams]:
    mon = cfg.monitor
    common = dict(
        dataset=row.dataset,
        type_code=mon.type_code,
        freq=mon.freq,
        cl_code=mon.classification,
        reporter=row.reporter_code,
        period=row.period,
    )
    if cfg.tier == "premium":
        return [
            TaskParams(
                **common,
                flows=(),
                cmd_level=None,
                cmd_codes=None,
                partners=None,
                partner2=None,
                customs=None,
                mot=None,
                kind="bulk",
            )
        ]
    classic = mon.breakdown == "classic"
    return [
        TaskParams(
            **common,
            flows=(flow,),
            cmd_level=mon.cmd_level,
            cmd_codes=None,
            partners=None,
            partner2="0" if classic else None,
            customs="C00" if classic else None,
            mot="0" if classic else None,
        )
        for flow in mon.flows
    ]


def cmd_monitor(
    cfg: Config, *, dry_run: bool = False, wait_quota: bool = False, live: bool = False
) -> int:
    mon = cfg.monitor
    if not dry_run:
        cfg.require_key()
    store = Store(cfg.paths.state_db)
    client = ComtradeClient(cfg)
    refs = Refs(client, cfg.paths.refs)

    reporter_filter: set[str] | None = None
    if mon.reporters:
        reporter_filter = {refs.reporter_code(r) for r in mon.reporters}

    try:
        # -- diff availability against seen releases -------------------------
        new: list[ReleaseRow] = []
        revised: list[ReleaseRow] = []
        for dataset in mon.datasets:
            rows = fetch_availability(
                client,
                dataset=dataset,
                type_code=mon.type_code,
                freq=mon.freq,
                classification=mon.classification,
            )
            seen = store.all_releases(dataset, mon.freq)
            for row in rows:
                if mon.start_period and row.period < mon.start_period:
                    continue
                if reporter_filter and row.reporter_code not in reporter_filter:
                    continue
                prev = seen.get((row.reporter_code, row.period))
                if prev is None:
                    new.append(row)
                elif row.last_released > prev.last_released:
                    revised.append(row)

        if dry_run:
            for label, items in (("new", new), ("revised", revised)):
                print(f"{label}: {len(items)}")
                for r in items[:20]:
                    print(
                        f"  {r.dataset} period={r.period} reporter={r.reporter_code} "
                        f"released={r.last_released} records={r.total_records}"
                    )
                if len(items) > 20:
                    print(f"  ... and {len(items) - 20} more")
            print("(dry run: nothing enqueued)")
            return 0

        # -- enqueue + drain under the singleton lock ----------------------------
        with worker_lock(cfg.paths.state_dir / "worker.lock"):
            job_id = f"mon-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%S}"
            if new or revised:
                store.create_job(job_id, "monitor", "{}")
                n_tasks = 0
                for row in new + revised:
                    superseded = store.supersede_monitor_tasks(
                        row.dataset, mon.freq, row.reporter_code, row.period, job_id
                    )
                    if superseded:
                        log.info(
                            "superseded %d stale task(s) for %s/%s reporter=%s period=%s",
                            superseded,
                            row.dataset,
                            mon.freq,
                            row.reporter_code,
                            row.period,
                        )
                    n_tasks += store.add_tasks(job_id, _monitor_tasks(cfg, row))
                    store.upsert_release(
                        row.dataset,
                        mon.freq,
                        row.reporter_code,
                        row.period,
                        row.last_released,
                        job_id,
                    )
                log.info(
                    "monitor %s: %d new + %d revised release(s) -> %d task(s)",
                    job_id,
                    len(new),
                    len(revised),
                    n_tasks,
                )
            else:
                log.info("monitor: no new releases; draining any leftover tasks")

            quota = QuotaLedger(store, cfg.quota)
            board = StatusBoard(cfg.paths.state_dir / STATUS_FILE)
            worker = Worker(cfg, store, client, quota, refs, board)
            worker.install_signal_handlers()
            with ExitStack() as stack:
                if live:
                    from ..ui.inline import InlineProgress

                    stack.enter_context(InlineProgress(cfg, store, board, kind="monitor"))
                rc = worker.run(job_kind="monitor", wait_quota=wait_quota or mon.wait_for_quota)

            finalized = store.finalize_releases()

        remaining = sum(jp.remaining for jp in store.job_progress() if jp.kind == "monitor")
        summary = (
            f"monitor run={job_id} new={len(new)} revised={len(revised)} "
            f"tasks_done={worker.tasks_done} split={worker.tasks_split} "
            f"failed={worker.tasks_failed} remaining={remaining} "
            f"releases_finalized={finalized} quota_used={quota.used_today()}"
            + (f"/{cfg.quota.daily_budget}" if cfg.quota.daily_budget else "")
        )
        log.info("%s", summary)
        print(summary)
        return rc
    finally:
        store.close()
