"""HTTP client for the Comtrade+ API.

Transport-level retries (429/5xx) live in the urllib3 Retry mounted on the session;
app-level error classification (quota-403, error envelopes, truncation) lives here.
The subscription key travels only in the Ocp-Apim-Subscription-Key header, never in
URLs, so logs and manifests stay clean.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable
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

# on_progress(stage, wire_bytes, wire_total): stage is "headers" once the response
# starts, "body" as bytes arrive, "done" when the body is complete. Byte counts are
# as transferred (compressed), so they line up with Content-Length when present.
ProgressFn = Callable[[str, int, int | None], None]


@dataclass(frozen=True)
class FetchResult:
    records: list[dict[str, Any]]
    truncated: bool  # row count hit the per-call cap -> response is incomplete
    cap: int
    url: str
    elapsed_s: float
    wire_bytes: int = 0  # response body as transferred
    ttfb_s: float = 0.0  # request sent -> response headers (server-side time, mostly)


def _content_length(resp: requests.Response) -> int | None:
    raw = resp.headers.get("Content-Length", "")
    return int(raw) if raw.isdigit() else None


def _wire_bytes(resp: requests.Response, fallback: int) -> int:
    """Bytes pulled off the socket so far (urllib3's tell()). Chunked bodies
    never advance that counter, so fall back to the decoded length."""
    try:
        n = int(resp.raw.tell())
    except (AttributeError, TypeError, ValueError):
        return fallback
    return n if n > 0 else fallback


class _ReportingRetry(Retry):
    """urllib3 Retry that says so when it retries: otherwise a call riding out
    a run of 503s just looks slow. `notify` is bound per session through a
    subclass, because Retry.new() rebuilds the object on every attempt."""

    notify: Callable[[str], None] | None = None

    def increment(
        self, method=None, url=None, response=None, error=None, _pool=None, _stacktrace=None
    ):
        new = super().increment(method, url, response, error, _pool, _stacktrace)
        if response is not None and response.status:
            cause = f"HTTP {response.status}"
        else:
            cause = type(error).__name__ if error is not None else "error"
        attempt = len(new.history)
        # urllib3 honours a Retry-After header over its own backoff
        after = new.get_retry_after(response) if response is not None else None
        wait = after if after is not None else new.get_backoff_time()
        msg = f"transport retry {attempt}/{attempt + (new.total or 0)} after {cause}"
        msg += f", waiting {wait:.0f}s" if wait >= 1 else ""
        log.info("%s", msg)
        if type(self).notify is not None:
            type(self).notify(msg)
        return new


def build_session(
    transport_retries: int, backoff_factor: float, notify: Callable[[str], None] | None = None
) -> requests.Session:
    retry_cls: type[Retry] = Retry
    if notify is not None:
        retry_cls = type("SessionRetry", (_ReportingRetry,), {"notify": staticmethod(notify)})
    retry = retry_cls(
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
        self._on_retry: Callable[[str], None] | None = None  # set for the duration of a call
        self.session = build_session(
            cfg.retry.transport_retries, cfg.retry.backoff_factor, notify=self._retried
        )

    def _retried(self, msg: str) -> None:
        if self._on_retry is not None:
            self._on_retry(msg)

    # -- plumbing ---------------------------------------------------------

    def _get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        need_key: bool,
        stream: bool = False,
    ) -> requests.Response:
        headers = {"User-Agent": f"tradescrapr/{__version__}"}
        if need_key:
            headers["Ocp-Apim-Subscription-Key"] = self.cfg.require_key()
        self.throttle.wait()
        try:
            resp = self.session.get(
                url,
                params=params,
                headers=headers,
                timeout=self.cfg.api.timeout_s,
                stream=stream,
            )
        except requests.RequestException as e:
            raise TransientError(f"network error: {e}") from e
        self._raise_for_status(resp)
        return resp

    @staticmethod
    def _read_body(
        resp: requests.Response, on_progress: ProgressFn | None
    ) -> tuple[bytearray, int]:
        """Drain a streamed response, reporting progress per chunk."""
        total = _content_length(resp)
        if on_progress:
            on_progress("headers", 0, total)
        body = bytearray()
        try:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                body += chunk
                if on_progress:
                    on_progress("body", _wire_bytes(resp, len(body)), total)
        except requests.RequestException as e:
            raise TransientError(f"response interrupted: {e}") from e
        finally:
            resp.close()
        wire = _wire_bytes(resp, len(body))
        if on_progress:
            on_progress("done", wire, total)
        return body, wire

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
    def _parse_json(raw: bytes | bytearray, url: str) -> dict[str, Any]:
        try:
            payload = json.loads(raw)
        except ValueError as e:
            raise TransientError(f"invalid JSON from {url[:120]}: {e}") from e
        if not isinstance(payload, dict):
            raise TransientError(f"unexpected JSON shape from {url[:120]}")
        err = payload.get("error")
        if err not in (None, "", {}, []):
            # The API occasionally wraps errors in a 200; retryable until proven otherwise.
            raise TransientError(f"API error envelope: {str(err)[:300]}")
        return payload

    @classmethod
    def _parse_payload(cls, resp: requests.Response) -> dict[str, Any]:
        return cls._parse_json(resp.content, resp.url)

    # -- endpoints --------------------------------------------------------

    def get_data(
        self,
        *,
        dataset: str,
        type_code: str,
        freq: str,
        cl_code: str,
        query: dict[str, Any],
        on_progress: ProgressFn | None = None,
        on_retry: Callable[[str], None] | None = None,
    ) -> FetchResult:
        """One data call. Counts against the daily quota (caller reserves first).
        The body is streamed so on_progress can report bytes as they arrive;
        on_retry hears about transport-level retries (429/5xx, dropped links)."""
        url = f"{API_BASE}/data/v1/{DATA_PATHS[dataset]}/{type_code}/{freq}/{cl_code}"
        cap = int(query.get("maxRecords") or self.cfg.api.max_records)
        params = {**query, "maxRecords": cap, "format": "JSON"}
        t0 = time.monotonic()
        self._on_retry = on_retry
        try:
            resp = self._get(url, params, need_key=True, stream=True)
        finally:
            self._on_retry = None
        ttfb = time.monotonic() - t0
        body, wire = self._read_body(resp, on_progress)
        payload = self._parse_json(body, resp.url)
        records = payload.get("data") or []
        return FetchResult(
            records=records,
            truncated=len(records) >= cap,
            cap=cap,
            url=resp.url,
            elapsed_s=time.monotonic() - t0,
            wire_bytes=wire,
            ttfb_s=ttfb,
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
        on_progress: ProgressFn | None = None,
    ) -> str:
        """Stream a file to dest via .part + os.replace, resuming with HTTP Range
        when a partial exists (the house BACI-downloader pattern). Returns the
        sha256 of the finished file. Progress counts the resumed prefix too."""
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
        length = _content_length(resp)
        total = pos + length if length is not None else None
        got = pos
        if on_progress:
            on_progress("headers", got, total)

        try:
            with open(part, mode) as out:
                for chunk in resp.iter_content(chunk_size):
                    if shutdown is not None and shutdown.is_set():
                        raise ShutdownRequested("shutdown during bulk download")
                    out.write(chunk)
                    sha.update(chunk)
                    got += len(chunk)
                    if on_progress:
                        on_progress("body", got, total)
        except requests.RequestException as e:
            raise TransientError(f"download interrupted: {e} (partial kept for resume)") from e
        os.replace(part, dest)
        if on_progress:
            on_progress("done", got, total)
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
