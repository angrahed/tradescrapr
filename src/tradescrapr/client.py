"""HTTP client for the Comtrade+ API.

Transport-level retries (429/5xx) live in the urllib3 Retry mounted on the session;
app-level error classification (quota-403, error envelopes, truncation) lives here.
The subscription key travels only in the Ocp-Apim-Subscription-Key header, never in
URLs, so logs and manifests stay clean.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import __version__
from .config import API_BASE, Config
from .errors import PermanentError, QuotaExhausted, ShutdownRequested, TransientError
from .ratelimit import Throttle

log = logging.getLogger(__name__)

DATA_PATHS = {"final": "get", "tariffline": "getTariffline"}
DA_PATHS = {"final": "getDA", "tariffline": "getDaTariffline"}


@dataclass(frozen=True)
class FetchResult:
    records: list[dict[str, Any]]
    truncated: bool  # row count hit the per-call cap -> response is incomplete
    cap: int
    url: str
    elapsed_s: float


def build_session(transport_retries: int, backoff_factor: float) -> requests.Session:
    retry = Retry(
        total=transport_retries,
        connect=transport_retries,
        read=transport_retries,
        status=transport_retries,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class ComtradeClient:
    def __init__(self, cfg: Config, throttle: Throttle | None = None):
        self.cfg = cfg
        self.throttle = throttle or Throttle(cfg.api.rps)
        self.session = build_session(cfg.retry.transport_retries, cfg.retry.backoff_factor)

    # -- plumbing ---------------------------------------------------------

    def _get(
        self, url: str, params: dict[str, Any] | None = None, *, need_key: bool
    ) -> requests.Response:
        headers = {"User-Agent": f"tradescrapr/{__version__}"}
        if need_key:
            headers["Ocp-Apim-Subscription-Key"] = self.cfg.require_key()
        self.throttle.wait()
        try:
            resp = self.session.get(
                url, params=params, headers=headers, timeout=self.cfg.api.timeout_s
            )
        except requests.RequestException as e:
            raise TransientError(f"network error: {e}") from e
        self._raise_for_status(resp)
        return resp

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        code = resp.status_code
        if code == 200:
            return
        snippet = " ".join((resp.text or "").split())[:300]
        if code == 401:
            raise PermanentError(f"401 unauthorized — check COMTRADE_KEY: {snippet}")
        if code == 403:
            if "quota" in snippet.lower():
                raise QuotaExhausted(f"server reports quota exhausted: {snippet}")
            raise PermanentError(f"403 forbidden — key lacks access: {snippet}")
        if code == 429:
            ra = resp.headers.get("Retry-After")
            retry_after = float(ra) if ra and ra.replace(".", "", 1).isdigit() else None
            raise TransientError(
                f"429 rate-limited after transport retries: {snippet}", retry_after=retry_after
            )
        if code >= 500:
            raise TransientError(f"{code} server error: {snippet}")
        raise PermanentError(f"{code} client error: {snippet}")

    @staticmethod
    def _parse_payload(resp: requests.Response) -> dict[str, Any]:
        try:
            payload = resp.json()
        except ValueError as e:
            raise TransientError(f"invalid JSON from {resp.url[:120]}: {e}") from e
        if not isinstance(payload, dict):
            raise TransientError(f"unexpected JSON shape from {resp.url[:120]}")
        err = payload.get("error")
        if err not in (None, "", {}, []):
            # The API occasionally wraps errors in a 200; retryable until proven otherwise.
            raise TransientError(f"API error envelope: {str(err)[:300]}")
        return payload

    # -- endpoints --------------------------------------------------------

    def get_data(
        self, *, dataset: str, type_code: str, freq: str, cl_code: str, query: dict[str, Any]
    ) -> FetchResult:
        """One data call. Counts against the daily quota (caller reserves first)."""
        url = f"{API_BASE}/data/v1/{DATA_PATHS[dataset]}/{type_code}/{freq}/{cl_code}"
        cap = int(query.get("maxRecords") or self.cfg.api.max_records)
        params = {**query, "maxRecords": cap, "format": "JSON"}
        t0 = time.monotonic()
        resp = self._get(url, params, need_key=True)
        payload = self._parse_payload(resp)
        records = payload.get("data") or []
        return FetchResult(
            records=records,
            truncated=len(records) >= cap,
            cap=cap,
            url=resp.url,
            elapsed_s=time.monotonic() - t0,
        )

    def get_availability(
        self,
        *,
        dataset: str,
        type_code: str,
        freq: str,
        cl_code: str,
        period: str | None = None,
        reporter_code: str | None = None,
    ) -> list[dict[str, Any]]:
        """Data-availability rows. Free public endpoint — never quota-debited."""
        url = f"{API_BASE}/public/v1/{DA_PATHS[dataset]}/{type_code}/{freq}/{cl_code}"
        params: dict[str, Any] = {}
        if period:
            params["period"] = period
        if reporter_code:
            params["reporterCode"] = reporter_code
        resp = self._get(url, params or None, need_key=False)
        payload = self._parse_payload(resp)
        return payload.get("data") or []

    def get_bulk_availability(
        self,
        *,
        dataset: str,
        type_code: str,
        freq: str,
        cl_code: str,
        reporter_code: str | None = None,
        period: str | None = None,
        classic: bool = True,
    ) -> list[dict[str, Any]]:
        """Premium-only: list downloadable bulk files (fileUrl, publicationDate, ...).
        URL shapes confirmed against the official comtradeapicall package."""
        if dataset == "tariffline":
            seg = "getTariffline"
        else:
            seg = "getClassic" if classic else "get"
        url = f"{API_BASE}/bulk/v1/{seg}/{type_code}/{freq}/{cl_code}"
        params: dict[str, Any] = {}
        if reporter_code:
            params["reportercode"] = reporter_code  # lowercase per official client
        if period:
            params["period"] = period
        resp = self._get(url, params or None, need_key=True)
        payload = self._parse_payload(resp)
        return payload.get("data") or []

    def download_file(
        self,
        url: str,
        dest: Path,
        *,
        need_key: bool = True,
        shutdown: threading.Event | None = None,
        chunk_size: int = 1 << 20,
    ) -> str:
        """Stream a file to dest via .part + os.replace, resuming with HTTP Range
        when a partial exists (the house BACI-downloader pattern). Returns the
        sha256 of the finished file."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        headers = {"User-Agent": f"tradescrapr/{__version__}"}
        if need_key:
            headers["Ocp-Apim-Subscription-Key"] = self.cfg.require_key()

        pos = part.stat().st_size if part.exists() else 0
        sha = hashlib.sha256()
        if pos:
            with open(part, "rb") as f:
                while chunk := f.read(chunk_size):
                    sha.update(chunk)
            headers["Range"] = f"bytes={pos}-"

        self.throttle.wait()
        try:
            resp = self.session.get(
                url, headers=headers, stream=True, timeout=self.cfg.api.timeout_s
            )
        except requests.RequestException as e:
            raise TransientError(f"network error: {e}") from e
        if resp.status_code == 416:  # our .part is stale/oversized; start over
            part.unlink(missing_ok=True)
            raise TransientError("range not satisfiable; partial discarded for retry")
        self._raise_for_status(resp)
        if pos and resp.status_code == 200:  # server ignored the Range header
            pos = 0
            sha = hashlib.sha256()
        mode = "ab" if pos else "wb"

        try:
            with open(part, mode) as out:
                for chunk in resp.iter_content(chunk_size):
                    if shutdown is not None and shutdown.is_set():
                        raise ShutdownRequested("shutdown during bulk download")
                    out.write(chunk)
                    sha.update(chunk)
        except requests.RequestException as e:
            raise TransientError(f"download interrupted: {e} (partial kept for resume)") from e
        os.replace(part, dest)
        return sha.hexdigest()

    def get_reference(self, filename: str) -> dict[str, Any]:
        """A reference file (Reporters.json, partnerAreas.json, HS.json, ...). Free."""
        url = f"{API_BASE}/files/v1/app/reference/{filename}"
        resp = self._get(url, need_key=False)
        try:
            payload = resp.json()
        except ValueError as e:
            raise TransientError(f"invalid JSON in reference {filename}: {e}") from e
        if not isinstance(payload, dict) or "results" not in payload:
            raise PermanentError(f"reference file {filename} has no 'results' key")
        return payload
