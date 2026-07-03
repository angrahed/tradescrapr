"""Provenance manifest sidecars, one per parquet chunk.

Mirrors the house pattern (tr-trade write_manifest): what was fetched, from
where, when, and the file hash — enough to audit or rebuild any partition.
The subscription key never appears (it travels in a header, not the URL).
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from . import __version__

LICENSE = (
    "UN Comtrade terms of use; cite: United Nations Comtrade Database, UN DESA/UNSD, "
    "https://comtradeplus.un.org"
)


def build_manifest(
    *,
    dataset: str,
    source_url: str,
    params: dict[str, Any],
    job_id: str,
    task_id: str,
    rows: int,
    local_bytes: int,
    sha256: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "tool": "tradescrapr",
        "tool_version": __version__,
        "dataset": f"UN Comtrade {dataset} trade data",
        "source_url": source_url,
        "params": params,
        "job_id": job_id,
        "task_id": task_id,
        "downloaded_at_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "rows": rows,
        "local_bytes": local_bytes,
        "sha256": sha256,
        "license": LICENSE,
        **extra,
    }


def manifest_path(data_path: Path) -> Path:
    return data_path.with_name(data_path.name + ".manifest.json")


def write_manifest(data_path: Path, payload: dict[str, Any]) -> Path:
    path = manifest_path(data_path)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return path
