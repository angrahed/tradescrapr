"""Cached Comtrade reference data: reporters, partners, classification code lists.

Reference files are free endpoints, cached in state/refs/ with a TTL (default 30
days). `tradescrapr refs refresh` forces a re-download.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any

from .client import ComtradeClient
from .config import Config
from .errors import ConfigError

log = logging.getLogger(__name__)

REPORTERS = "Reporters.json"
PARTNERS = "partnerAreas.json"
WORLD = "0"  # partner code for the World aggregate


def _partner_code(row: dict[str, Any]) -> str:
    # partnerAreas.json capitalises PartnerCode; be tolerant anyway
    for key in ("PartnerCode", "partnerCode", "id"):
        if row.get(key) is not None:
            return str(row[key])
    raise KeyError(f"no partner code in {row!r}")


def _partner_iso3(row: dict[str, Any]) -> str | None:
    return row.get("PartnerCodeIsoAlpha3") or row.get("partnerCodeIsoAlpha3")


class Refs:
    def __init__(self, client: ComtradeClient, refs_dir: Path, ttl_days: int = 30):
        self.client = client
        self.dir = refs_dir
        self.ttl = dt.timedelta(days=ttl_days)

    # -- cache ------------------------------------------------------------

    def _load(self, filename: str, force: bool = False) -> list[dict[str, Any]]:
        path = self.dir / filename
        if not force and path.exists():
            try:
                payload = json.loads(path.read_text())
                fetched = dt.datetime.fromisoformat(payload["fetched_at_utc"])
                if dt.datetime.now(dt.UTC) - fetched < self.ttl:
                    return payload["results"]
            except (ValueError, KeyError):
                log.warning("refs: cache %s unreadable, refetching", path)
        remote = self.client.get_reference(filename)
        out = {
            "fetched_at_utc": dt.datetime.now(dt.UTC).isoformat(),
            "results": remote["results"],
        }
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
        tmp.write_text(json.dumps(out))
        os.replace(tmp, path)
        log.info("refs: refreshed %s (%d entries)", filename, len(out["results"]))
        return out["results"]

    def refresh(self, classifications: tuple[str, ...] = ("HS",)) -> None:
        self._load(REPORTERS, force=True)
        self._load(PARTNERS, force=True)
        for cl in classifications:
            self._load(f"{cl}.json", force=True)

    # -- reporters ---------------------------------------------------------

    def reporters(self) -> list[dict[str, Any]]:
        return self._load(REPORTERS)

    def reporter_code(self, ident: str) -> str:
        """ISO3 or numeric identifier -> numeric code string."""
        s = str(ident).strip()
        if s.isdigit():
            return s
        upper = s.upper()
        for r in self.reporters():
            if r.get("reporterCodeIsoAlpha3") == upper:
                return str(r["reporterCode"])
        raise ConfigError(f"unknown reporter {ident!r} (expected ISO3 or numeric code)")

    def all_reporter_codes(self, include_groups: bool = False) -> list[str]:
        return [
            str(r["reporterCode"])
            for r in self.reporters()
            if include_groups or not r.get("isGroup")
        ]

    def reporter_iso3(self, code: str) -> str | None:
        for r in self.reporters():
            if str(r["reporterCode"]) == str(code):
                return r.get("reporterCodeIsoAlpha3")
        return None

    # -- partners ------------------------------------------------------------

    def partners(self) -> list[dict[str, Any]]:
        return self._load(PARTNERS)

    def partner_code(self, ident: str) -> str:
        s = str(ident).strip()
        if s.isdigit():
            return s
        upper = s.upper()
        for r in self.partners():
            if _partner_iso3(r) == upper:
                return _partner_code(r)
        raise ConfigError(f"unknown partner {ident!r} (expected ISO3 or numeric code)")

    def all_partner_codes(self, include_world: bool = True) -> list[str]:
        """All individual partners. World (0) is included by default so that
        partner-split children reproduce exactly the row set of an unsplit
        all-partner call (which contains the World aggregate rows)."""
        out = []
        for r in self.partners():
            code = _partner_code(r)
            if r.get("isGroup") and code != WORLD:
                continue
            if code == WORLD and not include_world:
                continue
            out.append(code)
        return out

    # -- commodity classifications --------------------------------------------

    def classification(self, cl_code: str) -> list[dict[str, Any]]:
        return self._load(f"{cl_code}.json")

    def cmd_codes(self, cl_code: str, level: int) -> list[str]:
        """All commodity codes at the given aggregation level (e.g. HS level 6)."""
        rows = self.classification(cl_code)
        codes = [
            str(r["id"])
            for r in rows
            if str(r.get("id", "")).upper() != "TOTAL"
            and int(r.get("aggrLevel", len(str(r.get("id", ""))))) == level
        ]
        if not codes:
            raise ConfigError(f"classification {cl_code} has no codes at level {level}")
        return sorted(codes)


def cmd_refs_refresh(cfg: Config) -> int:
    client = ComtradeClient(cfg)
    refs = Refs(client, cfg.paths.refs)
    refs.refresh()
    print(
        f"refreshed: {len(refs.reporters())} reporters, {len(refs.partners())} partners, "
        f"{len(refs.classification('HS'))} HS entries -> {cfg.paths.refs}"
    )
    return 0
