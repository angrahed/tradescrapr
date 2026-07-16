"""Worker loop end-to-end with a scripted fake client — no network."""

import datetime as dt

import polars as pl

from tradescrapr.client import FetchResult
from tradescrapr.config import QuotaConfig
from tradescrapr.errors import PermanentError, QuotaExhausted, TransientError
from tradescrapr.planner import TaskParams
from tradescrapr.quota import QuotaLedger
from tradescrapr.worker import Worker


class FakeClient:
    """Pops one scripted action per get_data call: a FetchResult or an exception."""

    def __init__(self, script, availability=None):
        self.script = list(script)
        self.calls = 0
        self.availability = availability or []

    def get_data(self, **_kw):
        self.calls += 1
        action = self.script.pop(0)
        if isinstance(action, Exception):
            raise action
        return action

    def get_availability(self, **_kw):
        return self.availability


def _result(n, cap=100):
    return FetchResult(
        records=[{"cmdCode": f"{i:06d}", "primaryValue": float(i)} for i in range(n)],
        truncated=n >= cap,
        cap=cap,
        url="https://api/get?x=1",
        elapsed_s=0.01,
    )


def _params(flow="M", **kw):
    base = dict(
        dataset="final",
        type_code="C",
        freq="A",
        cl_code="HS",
        reporter="8",
        period="2022",
        flows=(flow,),
        cmd_level=6,
        cmd_codes=None,
        partners=None,
    )
    base.update(kw)
    return TaskParams(**base)


def _worker(cfg, store, refs, script, budget=10, availability=None):
    quota = QuotaLedger(
        store, QuotaConfig(daily_budget=budget, reset_time=dt.time(0, 0), reset_tz="UTC")
    )
    return Worker(cfg, store, FakeClient(script, availability), quota, refs)


def _seed(store, params_list):
    store.create_job("j1", "targeted", "{}")
    store.add_tasks("j1", params_list)


def test_success_writes_partitioned_parquet(cfg, store, refs):
    _seed(store, [_params()])
    w = _worker(cfg, store, refs, [_result(5)])
    assert w.run() == 0
    assert w.tasks_done == 1
    prog = store.job_progress("j1")[0]
    assert prog.counts == {"done": 1} and prog.records == 5
    part = cfg.paths.data / "final" / "freq=A" / "period=2022" / "reporter=8"
    files = list(part.glob("part-*.parquet"))
    assert len(files) == 1
    assert pl.read_parquet(files[0]).height == 5
    assert (part / f"{files[0].name}.manifest.json").exists()


def test_zero_records_is_done_without_file(cfg, store, refs):
    _seed(store, [_params()])
    w = _worker(cfg, store, refs, [_result(0)])
    w.run()
    assert store.job_progress("j1")[0].counts == {"done": 1}
    assert not list(cfg.paths.data.rglob("*.parquet"))


def test_truncation_splits_then_children_complete(cfg, store, refs):
    _seed(store, [_params(flow="M")])
    # no availability estimate -> parent splits into 4 default partner groups,
    # which then all succeed.
    w = _worker(cfg, store, refs, [_result(100)] + [_result(7)] * 4)
    assert w.run() == 0
    prog = store.job_progress("j1")[0]
    assert prog.counts == {"split": 1, "done": 4}
    assert prog.records == 28


def test_truncation_split_right_sized_from_availability(cfg, store, refs):
    _seed(store, [_params(flow="M")])
    # totalRecords=800 -> est = 800/3 (classic) /2 (one flow) ≈ 133 rows;
    # cap=100 -> ceil(133 / 75) = 2 partner groups instead of the default 4.
    avail = [{"reporterCode": 8, "period": "2022", "totalRecords": 800}]
    w = _worker(cfg, store, refs, [_result(100), _result(7), _result(7)], availability=avail)
    assert w.run() == 0
    prog = store.job_progress("j1")[0]
    assert prog.counts == {"split": 1, "done": 2}


def test_transient_retries_then_succeeds(cfg, store, refs):
    _seed(store, [_params()])
    w = _worker(cfg, store, refs, [TransientError("net"), TransientError("net"), _result(2)])
    assert w.run() == 0
    prog = store.job_progress("j1")[0]
    assert prog.counts == {"done": 1}


def test_transient_dead_letters_at_max_attempts(cfg, store, refs):
    _seed(store, [_params()])
    # cfg fixture: task_max_attempts=3
    w = _worker(cfg, store, refs, [TransientError("net")] * 3)
    assert w.run() == 0
    assert store.job_progress("j1")[0].counts == {"dead": 1}


def test_permanent_error_dead_immediately(cfg, store, refs):
    _seed(store, [_params()])
    w = _worker(cfg, store, refs, [PermanentError("400 bad request")])
    w.run()
    assert store.job_progress("j1")[0].counts == {"dead": 1}
    assert w.client.calls == 1


def test_local_quota_exhaustion_releases_and_exits(cfg, store, refs):
    _seed(store, [_params(flow="M"), _params(flow="X")])
    w = _worker(cfg, store, refs, [_result(1)], budget=1)
    assert w.run(wait_quota=False) == 0
    prog = store.job_progress("j1")[0]
    assert prog.counts == {"done": 1, "pending": 1}
    # released task kept attempts at 0
    t = store.claim_next_task()
    assert t.attempts == 1


def test_server_quota_exhaustion_resyncs_ledger(cfg, store, refs):
    _seed(store, [_params()])
    w = _worker(cfg, store, refs, [QuotaExhausted("server says no")], budget=7)
    w.run(wait_quota=False)
    assert w.quota.used_today() == 7  # force_exhaust resynced
    assert store.job_progress("j1")[0].counts == {"pending": 1}


def test_unexpected_exception_requeues(cfg, store, refs):
    _seed(store, [_params()])
    w = _worker(cfg, store, refs, [ValueError("bug"), _result(1)])
    assert w.run() == 0
    assert store.job_progress("j1")[0].counts == {"done": 1}


def test_max_calls_stops_early(cfg, store, refs):
    _seed(store, [_params(flow="M"), _params(flow="X")])
    w = _worker(cfg, store, refs, [_result(1), _result(1)])
    w.run(max_calls=1)
    prog = store.job_progress("j1")[0]
    assert prog.counts == {"done": 1, "pending": 1}


def test_targeted_jobs_do_not_wipe_existing_partition(cfg, store, refs):
    # job 1 writes the partition
    _seed(store, [_params(flow="M")])
    _worker(cfg, store, refs, [_result(3)]).run()
    part = cfg.paths.data / "final" / "freq=A" / "period=2022" / "reporter=8"
    old_files = set(p.name for p in part.glob("part-*.parquet"))
    # a second targeted scrape in the same reporter-period should coexist
    store.create_job("j2", "targeted", "{}")
    store.add_tasks("j2", [_params(flow="M"), _params(flow="X")])
    _worker(cfg, store, refs, [_result(2), _result(2)]).run(job_id="j2")
    new_files = set(p.name for p in part.glob("part-*.parquet"))
    assert old_files < new_files
    assert len(new_files) == 3


def test_monitor_cleanup_wipes_partition_once_per_job(cfg, store, refs):
    # job 1 writes the partition
    _seed(store, [_params(flow="M")])
    _worker(cfg, store, refs, [_result(3)]).run()
    part = cfg.paths.data / "final" / "freq=A" / "period=2022" / "reporter=8"
    old_files = set(p.name for p in part.glob("part-*.parquet"))
    # monitor jobs are release replacements and wipe old chunks once per partition
    store.create_job("j2", "monitor", "{}")
    store.add_tasks("j2", [_params(flow="M"), _params(flow="X")])
    _worker(cfg, store, refs, [_result(2), _result(2)]).run(job_id="j2")
    new_files = set(p.name for p in part.glob("part-*.parquet"))
    assert not (old_files & new_files)
    assert len(new_files) == 2
