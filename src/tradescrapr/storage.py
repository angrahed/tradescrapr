"""Partitioned parquet storage with atomic writes.

Layout: {data_root}/{dataset}/freq={A|M}/period={period}/reporter={code}/
        part-{task_id}[-{seq}].parquet  (+ .manifest.json sidecar)

Writes go to a tmp file in the destination directory, then os.replace() — the
same crash-safe pattern as the house BACI downloader. The parquet lands first,
the manifest second, the DB 'done' flip last; a kill between any two steps
leaves a task that re-runs idempotently (its outputs are keyed by task_id and
deleted before re-execution).
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from .manifest import build_manifest, write_manifest
from .schema import enforce, enforce_bulk

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WriteResult:
    path: Path
    rows: int
    sha256: str
    local_bytes: int


def partition_dir(data_root: Path, dataset: str, freq: str, period: str, reporter: str) -> Path:
    return data_root / dataset / f"freq={freq}" / f"period={period}" / f"reporter={reporter}"


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def _chunk_name(task_id: str, seq: int | None) -> str:
    return f"part-{task_id}" + (f"-{seq:04d}" if seq is not None else "") + ".parquet"


def write_chunk(
    records: list[dict[str, Any]] | pl.DataFrame,
    *,
    part_dir: Path,
    task_id: str,
    seq: int | None = None,
    manifest_meta: dict[str, Any],
) -> WriteResult:
    """Write one parquet chunk atomically, then its manifest sidecar."""
    if isinstance(records, pl.DataFrame):
        df = enforce(records)
    else:
        df = enforce(pl.DataFrame(records, infer_schema_length=None))
    part_dir.mkdir(parents=True, exist_ok=True)
    final = part_dir / _chunk_name(task_id, seq)
    tmp = final.with_name(final.name + f".tmp-{os.getpid()}")
    try:
        df.write_parquet(tmp, compression="zstd")
        os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    sha = sha256_file(final)
    size = final.stat().st_size
    payload = build_manifest(
        rows=df.height, local_bytes=size, sha256=sha, task_id=task_id, **manifest_meta
    )
    write_manifest(final, payload)
    log.debug("wrote %s (%d rows, %d bytes)", final, df.height, size)
    return WriteResult(path=final, rows=df.height, sha256=sha, local_bytes=size)


def convert_bulk_file(
    gz_path: Path,
    *,
    part_dir: Path,
    task_id: str,
    manifest_meta: dict[str, Any],
    batch_rows: int = 2_000_000,
) -> list[WriteResult]:
    """Convert a gzipped bulk TSV into parquet chunks of ~batch_rows each.

    Read as all-Utf8 (infer_schema_length=0) so every batch has an identical
    schema, then cast through the same dtype pins as API responses."""
    tsv_path = gz_path.with_name(gz_path.name + ".tsv")
    with gzip.open(gz_path, "rb") as f_in, open(tsv_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    results: list[WriteResult] = []
    try:
        lazy = pl.scan_csv(tsv_path, separator="\t", infer_schema_length=0, quote_char=None)
        for seq, batch in enumerate(lazy.collect_batches(chunk_size=batch_rows)):
            if batch.height == 0:
                continue
            results.append(
                write_chunk(
                    enforce_bulk(batch),
                    part_dir=part_dir,
                    task_id=task_id,
                    seq=seq,
                    manifest_meta={**manifest_meta, "seq": seq},
                )
            )
    finally:
        tsv_path.unlink(missing_ok=True)
    return results


def remove_task_outputs(part_dir: Path, task_id: str) -> int:
    """Delete any prior outputs of this task (idempotent re-runs), including
    stale tmp files from a killed process."""
    n = 0
    if not part_dir.is_dir():
        return 0
    for p in part_dir.glob(f"part-{task_id}*"):
        p.unlink(missing_ok=True)
        n += 1
    return n


def clean_partition(part_dir: Path) -> int:
    """Wipe every chunk in a partition — used once per (job, partition) so
    re-scrapes and revisions fully replace old data."""
    n = 0
    if not part_dir.is_dir():
        return 0
    for p in part_dir.glob("part-*"):
        p.unlink(missing_ok=True)
        n += 1
    if n:
        log.info("cleaned partition %s (%d files)", part_dir, n)
    return n
