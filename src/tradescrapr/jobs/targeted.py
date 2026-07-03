"""Targeted mode: expand a YAML job spec into a persistent job + task queue."""

from __future__ import annotations

import logging
from pathlib import Path

from ..availability import fetch_availability
from ..client import ComtradeClient
from ..config import Config
from ..planner import expand, load_spec
from ..refs import Refs
from ..state import Store

log = logging.getLogger(__name__)


def cmd_plan(cfg: Config, spec_path: Path, *, dry_run: bool = False) -> int:
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
    print(f"job:        {job_id}")
    print(f"tasks:      {len(tasks)} (>= as many API calls; truncation splits add more)")
    print(f"quota cost: {est_days} at {budget or 'unlimited'} calls/day")
    if tasks:
        print("sample:")
        for t in tasks[:3]:
            print(f"  - {t.describe()}")
        if len(tasks) > 3:
            print(f"  ... and {len(tasks) - 3} more")

    if dry_run:
        print("(dry run: nothing written)")
        return 0

    store = Store(cfg.paths.state_db)
    try:
        created = store.create_job(job_id, "targeted", spec.to_json())
        added = store.add_tasks(job_id, tasks)
    finally:
        store.close()
    if created:
        print(f"created job {job_id} with {added} tasks")
    else:
        print(f"job {job_id} already exists; {added} new task(s) added (re-plan is idempotent)")
    print(f"next: uv run tradescrapr run --job {job_id}")
    return 0
