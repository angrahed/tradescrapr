from tradescrapr.planner import TaskParams
from tradescrapr.state import Store, task_id_for


def _params(reporter="8", period="2022", flow="M", **kw) -> TaskParams:
    base = dict(
        dataset="final",
        type_code="C",
        freq="A",
        cl_code="HS",
        reporter=reporter,
        period=period,
        flows=(flow,),
        cmd_level=6,
        cmd_codes=None,
        partners=None,
    )
    base.update(kw)
    return TaskParams(**base)


def _seed(store: Store, job="j1", kind="targeted", params=None):
    store.create_job(job, kind, "{}")
    params = params if params is not None else [_params()]
    store.add_tasks(job, params)
    return [task_id_for(job, p.params_hash()) for p in params]


def test_create_job_idempotent(store):
    assert store.create_job("j1", "targeted", "{}")
    assert not store.create_job("j1", "targeted", "{}")


def test_add_tasks_dedup(store):
    _seed(store)
    assert store.add_tasks("j1", [_params()]) == 0  # same params -> no new row
    assert store.add_tasks("j1", [_params(flow="X")]) == 1


def test_claim_lifecycle(store):
    (tid,) = _seed(store)
    t = store.claim_next_task()
    assert t.task_id == tid and t.status == "in_progress" and t.attempts == 1
    assert store.claim_next_task() is None  # nothing else claimable
    store.finish_task(tid, records=5, paths=["x.parquet"])
    assert store.claim_next_task() is None


def test_claim_prefers_fresh_over_retries(store):
    ids = _seed(store, params=[_params(flow="M"), _params(flow="X")])
    first = store.claim_next_task()
    store.fail_task(first.task_id, "boom", permanent=False, max_attempts=5)
    nxt = store.claim_next_task()
    assert nxt.task_id != first.task_id  # fresh task (0 attempts) goes first
    assert {first.task_id, nxt.task_id} == set(ids)


def test_fail_task_dead_letters_at_max_attempts(store):
    (tid,) = _seed(store)
    for expected in ("failed", "failed", "dead"):
        t = store.claim_next_task()
        assert t.task_id == tid
        status = store.fail_task(tid, "boom", permanent=False, max_attempts=3)
        assert status == expected
    assert store.claim_next_task() is None
    assert store.retry_dead() == 1
    assert store.claim_next_task().attempts == 1  # attempts were reset


def test_permanent_failure_is_dead_immediately(store):
    (tid,) = _seed(store)
    store.claim_next_task()
    assert store.fail_task(tid, "400", permanent=True, max_attempts=5) == "dead"


def test_release_task_refunds_attempt(store):
    (tid,) = _seed(store)
    store.claim_next_task()
    store.release_task(tid)
    t = store.claim_next_task()
    assert t.task_id == tid and t.attempts == 1  # not 2


def test_reset_stale_in_progress(store):
    (tid,) = _seed(store)
    store.claim_next_task()
    assert store.reset_stale_in_progress(older_than_min=120) == 0  # too fresh
    assert store.reset_stale_in_progress(None) == 1  # startup reset takes all
    assert store.claim_next_task().task_id == tid


def test_split_task_atomic(store):
    (tid,) = _seed(store)
    parent = store.claim_next_task()
    children = [_params(flow="M", partners=("4",)), _params(flow="M", partners=("8",))]
    assert store.split_task(parent, children) == 2
    kids = [store.claim_next_task(), store.claim_next_task()]
    assert all(k.parent_task_id == tid and k.depth == 1 for k in kids)
    prog = store.job_progress("j1")[0]
    assert prog.counts["split"] == 1


def test_claim_kind_filter(store):
    _seed(store, job="jt", kind="targeted", params=[_params(flow="M")])
    _seed(store, job="jm", kind="monitor", params=[_params(flow="X")])
    t = store.claim_next_task(kind="monitor")
    assert t.job_id == "jm"
    assert store.claim_next_task(kind="monitor") is None
    assert store.claim_next_task().job_id == "jt"


def test_releases_finalize_only_when_all_settled(store):
    p1, p2 = _params(flow="M"), _params(flow="X")
    _seed(store, job="mon-1", kind="monitor", params=[p1, p2])
    store.upsert_release("final", "A", "8", "2022", "2026-01-01", "mon-1")

    t1 = store.claim_next_task()
    store.finish_task(t1.task_id, records=1, paths=[])
    assert store.finalize_releases() == 0  # second task still pending

    t2 = store.claim_next_task()
    store.fail_task(t2.task_id, "x", permanent=True, max_attempts=3)
    assert store.finalize_releases() == 0  # dead blocks finalize

    store.retry_dead()
    t2 = store.claim_next_task()
    store.finish_task(t2.task_id, records=1, paths=[])
    assert store.finalize_releases() == 1
    rel = store.get_release("final", "A", "8", "2022")
    assert rel.status == "done"


def test_upsert_release_revision_resets_status(store):
    _seed(store, job="mon-1", kind="monitor")
    store.upsert_release("final", "A", "8", "2022", "2026-01-01", "mon-1")
    t = store.claim_next_task()
    store.finish_task(t.task_id, records=1, paths=[])
    store.finalize_releases()
    store.upsert_release("final", "A", "8", "2022", "2026-02-01", "mon-2")
    rel = store.get_release("final", "A", "8", "2022")
    assert rel.status == "pending" and rel.job_id == "mon-2"


def test_supersede_monitor_tasks(store):
    _seed(store, job="mon-1", kind="monitor", params=[_params(flow="M")])
    _seed(store, job="tgt-1", kind="targeted", params=[_params(flow="X")])
    store.create_job("mon-2", "monitor", "{}")
    n = store.supersede_monitor_tasks("final", "A", "8", "2022", "mon-2")
    assert n == 1  # only the monitor task; targeted jobs untouched
    assert store.claim_next_task(kind="monitor") is None
    assert store.claim_next_task(kind="targeted") is not None


def test_partition_cleanup_fires_once(store):
    store.create_job("j1", "targeted", "{}")
    assert store.try_record_partition_cleanup("j1", "final", "A", "2022", "8")
    assert not store.try_record_partition_cleanup("j1", "final", "A", "2022", "8")
    assert store.try_record_partition_cleanup("j2", "final", "A", "2022", "8")  # new job
