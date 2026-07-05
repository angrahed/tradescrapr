"""plan_spec / cmd_plan for targeted jobs (offline: use_availability false)."""

from __future__ import annotations

from tradescrapr.jobs.targeted import cmd_plan, plan_spec
from tradescrapr.state import Store

SPEC = """
name: smoke
dataset: final
type: C
freq: A
classification: HS
reporters: [ALB, DEU]
partners: all
flows: [M, X]
commodities: {level: 6}
periods: "2022"
use_availability: false
"""


def _spec_path(cfg, refs):
    path = cfg.paths.root / "jobs" / "smoke.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(SPEC)
    return path


def test_plan_spec_dry_run(cfg, refs):
    path = _spec_path(cfg, refs)
    res = plan_spec(cfg, path, dry_run=True)
    assert res.job_id.startswith("tgt-smoke-")
    assert res.n_tasks == 4  # 2 reporters x 1 period x 2 flows
    assert res.est_days == "~1 day(s)"  # budget 10 in the cfg fixture
    assert len(res.sample) == 3
    assert res.dry_run and not res.created and res.added == 0
    store = Store(cfg.paths.state_db)
    try:
        assert store.job_progress() == []  # nothing written
    finally:
        store.close()


def test_plan_spec_writes_and_replans_idempotently(cfg, refs):
    path = _spec_path(cfg, refs)
    res = plan_spec(cfg, path)
    assert res.created and res.added == 4
    again = plan_spec(cfg, path)
    assert not again.created and again.added == 0
    store = Store(cfg.paths.state_db)
    try:
        jobs = store.job_progress()
        assert len(jobs) == 1 and jobs[0].job_id == res.job_id and jobs[0].total == 4
    finally:
        store.close()


def test_cmd_plan_output(cfg, refs, capsys):
    path = _spec_path(cfg, refs)
    assert cmd_plan(cfg, path, dry_run=True) == 0
    out = capsys.readouterr().out
    assert "tasks:      4" in out
    assert "quota cost: ~1 day(s) at 10 calls/day" in out
    assert "  ... and 1 more" in out
    assert "(dry run: nothing written)" in out

    assert cmd_plan(cfg, path) == 0
    out = capsys.readouterr().out
    assert "created job tgt-smoke-" in out
    assert "next: uv run tradescrapr run --job tgt-smoke-" in out
