"""Targeted mode: expand a YAML job spec into a persistent job + task queue."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..availability import fetch_availability
from ..client import ComtradeClient
from ..config import Config
from ..planner import expand, load_spec
from ..refs import Refs
from ..state import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlanResult:
    job_id: str
    n_tasks: int
    est_days: str  # "~2 day(s)" or "n/a (unlimited)"
    sample: tuple[str, ...]  # first few task descriptions
    created: bool  # job row newly created (False on dry-run or re-plan)
    added: int  # tasks inserted (0 on dry-run)
    dry_run: bool


def plan_spec(cfg: Config, spec_path: Path, *, dry_run: bool = False) -> PlanResult:
    spec = load_spec(spec_path)
    client = ComtradeClient(cfg)
    refs = Refs(client, cfg.paths.refs)

    availability = None
    if spec.use_availability:
        availability = fetch_availability(
            client,
            dataset=spec.dataset,
            type_code=spec.type_code,
            freq=spec.freq,
            classification=spec.classification,
        )

    tasks = expand(spec, refs, combine_flows=cfg.planner.combine_flows, availability=availability)
    job_id = spec.job_id()

    budget = cfg.quota.daily_budget
    est_days = f"~{max(1, round(len(tasks) / budget))} day(s)" if budget else "n/a (unlimited)"
    sample = tuple(t.describe() for t in tasks[:3])

    if dry_run:
        return PlanResult(job_id, len(tasks), est_days, sample, False, 0, dry_run=True)

    store = Store(cfg.paths.state_db)
    try:
        created = store.create_job(job_id, "targeted", spec.to_json())
        added = store.add_tasks(job_id, tasks)
    finally:
        store.close()
    return PlanResult(job_id, len(tasks), est_days, sample, created, added, dry_run=False)


def cmd_plan(cfg: Config, spec_path: Path, *, dry_run: bool = False) -> int:
    res = plan_spec(cfg, spec_path, dry_run=dry_run)

    budget = cfg.quota.daily_budget
    print(f"job:        {res.job_id}")
    print(f"tasks:      {res.n_tasks} (>= as many API calls; truncation splits add more)")
    print(f"quota cost: {res.est_days} at {budget or 'unlimited'} calls/day")
    if res.sample:
        print("sample:")
        for line in res.sample:
            print(f"  - {line}")
        if res.n_tasks > 3:
            print(f"  ... and {res.n_tasks - 3} more")

    if res.dry_run:
        print("(dry run: nothing written)")
        return 0

    if res.created:
        print(f"created job {res.job_id} with {res.added} tasks")
    else:
        print(
            f"job {res.job_id} already exists; "
            f"{res.added} new task(s) added (re-plan is idempotent)"
        )
    print(f"next: uv run tradescrapr run --job {res.job_id}")
    return 0
