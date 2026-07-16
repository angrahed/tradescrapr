"""Premium bulk-file execution: one gzipped file per reporter-period replaces
dozens of paged /get calls and cannot truncate.

Flow per bulk task: bulk availability lookup (newest file for the reporter-
period) -> streamed download with Range-resume into state/tmp -> gzip TSV ->
parquet chunks in the standard partition layout -> gz deleted.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from . import storage
from .errors import QuotaExhausted, TransientError
from .planner import TaskParams
from .state import Task

if TYPE_CHECKING:
    from .worker import Worker

log = logging.getLogger(__name__)


def execute_bulk_task(worker: Worker, task: Task, params: TaskParams) -> None:
    cfg = worker.cfg
    classic = cfg.monitor.breakdown == "classic"

    if not worker.quota.try_reserve():
        raise QuotaExhausted(f"daily budget spent ({worker.quota.used_today()} calls)")
    worker.calls_made += 1
    try:
        rows = worker.client.get_bulk_availability(
            dataset=params.dataset,
            type_code=params.type_code,
            freq=params.freq,
            cl_code=params.cl_code,
            reporter_code=params.reporter,
            period=params.period,
            classic=classic,
        )
    except QuotaExhausted:
        worker.quota.force_exhaust()
        raise
    if not rows:
        # getDA said this reporter-period exists; the bulk listing usually lags
        # only briefly, so retry later rather than dead-letter.
        raise TransientError(
            f"no bulk file listed yet for reporter={params.reporter} period={params.period}"
        )
    newest = max(rows, key=lambda r: str(r.get("publicationDate") or r.get("timestamp") or ""))
    file_url = newest.get("fileUrl")
    if not file_url:
        raise TransientError(f"bulk availability row lacks fileUrl: {newest!r}")

    part_dir = storage.partition_dir(
        cfg.paths.data, params.dataset, params.freq, params.period, params.reporter
    )
    if worker.replaces_whole_partition(task.job_id) and worker.store.try_record_partition_cleanup(
        task.job_id, params.dataset, params.freq, params.period, params.reporter
    ):
        storage.clean_partition(part_dir)
    storage.remove_task_outputs(part_dir, task.task_id)

    tmp_gz = cfg.paths.state_dir / "tmp" / f"{task.task_id}.gz"
    sha_gz = worker.client.download_file(file_url, tmp_gz, shutdown=worker.shutdown)
    log.info("task=%s bulk file downloaded (%d bytes)", task.task_id, tmp_gz.stat().st_size)

    try:
        results = storage.convert_bulk_file(
            tmp_gz,
            part_dir=part_dir,
            task_id=task.task_id,
            manifest_meta={
                "dataset": params.dataset,
                "source_url": file_url,
                "params": params.to_dict(),
                "job_id": task.job_id,
                "bulk_file_sha256": sha_gz,
                "bulk_publication_date": str(
                    newest.get("publicationDate") or newest.get("timestamp") or ""
                ),
            },
        )
    finally:
        tmp_gz.unlink(missing_ok=True)

    rows_total = sum(r.rows for r in results)
    paths = [str(r.path.relative_to(cfg.paths.root)) for r in results]
    worker.store.finish_task(task.task_id, records=rows_total, paths=paths)
    worker.tasks_done += 1
    log.info(
        "task=%s job=%s bulk %s reporter=%s period=%s status=done records=%d chunks=%d",
        task.task_id,
        task.job_id,
        params.dataset,
        params.reporter,
        params.period,
        rows_total,
        len(results),
    )
