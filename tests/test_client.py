import json

import pytest
import responses

from tradescrapr.client import ComtradeClient
from tradescrapr.errors import PermanentError, QuotaExhausted, TransientError

DATA_URL = "https://comtradeapi.un.org/data/v1/get/C/A/HS"


@pytest.fixture
def client(cfg, monkeypatch):
    monkeypatch.setenv("COMTRADE_API_KEY", "test-key-123")
    from tradescrapr.config import load_config

    c = load_config(root=cfg.paths.root)
    return ComtradeClient(c)


def _payload(n):
    return {
        "elapsedTime": "0",
        "count": n,
        "data": [{"cmdCode": "01", "primaryValue": 1.0}] * n,
        "error": "",
    }


@responses.activate
def test_get_data_ok_and_key_in_header_only(client):
    responses.get(DATA_URL, json=_payload(3))
    res = client.get_data(
        dataset="final",
        type_code="C",
        freq="A",
        cl_code="HS",
        query={"reporterCode": "8", "period": "2022", "flowCode": "M"},
    )
    assert len(res.records) == 3 and not res.truncated
    req = responses.calls[0].request
    assert req.headers["Ocp-Apim-Subscription-Key"] == "test-key-123"
    assert "test-key-123" not in req.url
    assert "maxRecords=100" in req.url  # cfg fixture caps at 100


@responses.activate
def test_truncation_detected_at_cap(client):
    responses.get(DATA_URL, json=_payload(100))  # == cfg max_records
    res = client.get_data(dataset="final", type_code="C", freq="A", cl_code="HS", query={})
    assert res.truncated and res.cap == 100


@responses.activate
def test_quota_403_vs_auth_403(client):
    responses.get(
        DATA_URL,
        status=403,
        body=json.dumps(
            {"message": "Out of call volume quota. Quota will be replenished in 02:11:00."}
        ),
    )
    with pytest.raises(QuotaExhausted):
        client.get_data(dataset="final", type_code="C", freq="A", cl_code="HS", query={})

    responses.get(DATA_URL, status=403, body="Access denied due to invalid subscription")
    with pytest.raises(PermanentError):
        client.get_data(dataset="final", type_code="C", freq="A", cl_code="HS", query={})


@responses.activate
def test_429_transient_with_retry_after(client):
    responses.get(DATA_URL, status=429, headers={"Retry-After": "17"}, body="slow down")
    with pytest.raises(TransientError) as ei:
        client.get_data(dataset="final", type_code="C", freq="A", cl_code="HS", query={})
    assert ei.value.retry_after == 17.0


@responses.activate
def test_5xx_transient_400_permanent(client):
    responses.get(DATA_URL, status=502, body="bad gateway")
    with pytest.raises(TransientError):
        client.get_data(dataset="final", type_code="C", freq="A", cl_code="HS", query={})
    responses.get(DATA_URL, status=400, body="invalid parameter")
    with pytest.raises(PermanentError):
        client.get_data(dataset="final", type_code="C", freq="A", cl_code="HS", query={})


@responses.activate
def test_200_error_envelope_is_transient(client):
    responses.get(DATA_URL, json={"count": 0, "data": [], "error": "internal error"})
    with pytest.raises(TransientError):
        client.get_data(dataset="final", type_code="C", freq="A", cl_code="HS", query={})


@responses.activate
def test_availability_needs_no_key(client):
    url = "https://comtradeapi.un.org/public/v1/getDA/C/M/HS"
    responses.get(
        url,
        json={
            "count": 1,
            "data": [{"reporterCode": 8, "period": 202401, "lastReleased": "2026-01-01"}],
            "error": "",
        },
    )
    rows = client.get_availability(dataset="final", type_code="C", freq="M", cl_code="HS")
    assert rows[0]["reporterCode"] == 8
    assert "Ocp-Apim-Subscription-Key" not in responses.calls[0].request.headers


@responses.activate
def test_tariffline_paths(client):
    responses.get("https://comtradeapi.un.org/data/v1/getTariffline/C/M/HS", json=_payload(1))
    res = client.get_data(dataset="tariffline", type_code="C", freq="M", cl_code="HS", query={})
    assert len(res.records) == 1


@responses.activate
def test_bulk_availability_paths(client):
    responses.get(
        "https://comtradeapi.un.org/bulk/v1/getClassic/C/M/HS",
        json={"count": 1, "data": [{"fileUrl": "https://x/f.gz"}], "error": ""},
    )
    rows = client.get_bulk_availability(
        dataset="final",
        type_code="C",
        freq="M",
        cl_code="HS",
        reporter_code="8",
        period="202401",
        classic=True,
    )
    assert rows[0]["fileUrl"] == "https://x/f.gz"
    assert "reportercode=8" in responses.calls[0].request.url


@responses.activate
def test_download_file_writes_and_hashes(client, tmp_path):
    responses.get("https://x/f.gz", body=b"hello-bulk-bytes")
    dest = tmp_path / "f.gz"
    sha = client.download_file("https://x/f.gz", dest, need_key=False)
    assert dest.read_bytes() == b"hello-bulk-bytes"
    import hashlib

    assert sha == hashlib.sha256(b"hello-bulk-bytes").hexdigest()
    assert not list(tmp_path.glob("*.part"))


@responses.activate
def test_get_data_streams_with_progress(client):
    responses.get(DATA_URL, json=_payload(50))
    seen = []
    res = client.get_data(
        dataset="final",
        type_code="C",
        freq="A",
        cl_code="HS",
        query={},
        on_progress=lambda stage, wire, total: seen.append((stage, wire, total)),
    )
    stages = [s for s, _, _ in seen]
    assert stages[0] == "headers" and stages[-1] == "done" and "body" in stages
    assert seen[-1][1] == res.wire_bytes > 0
    assert res.ttfb_s >= 0 and len(res.records) == 50


@responses.activate
def test_download_progress_counts_against_content_length(client, tmp_path):
    body = b"x" * 5000
    responses.get("https://x/f.gz", body=body, headers={"Content-Length": "5000"})
    seen = []
    client.download_file(
        "https://x/f.gz",
        tmp_path / "f.gz",
        need_key=False,
        chunk_size=1000,
        on_progress=lambda stage, wire, total: seen.append((stage, wire, total)),
    )
    assert seen[0] == ("headers", 0, 5000)
    assert seen[-1] == ("done", 5000, 5000)
    assert [w for s, w, _ in seen if s == "body"] == [1000, 2000, 3000, 4000, 5000]


def test_transport_retries_are_reported():
    from urllib3.response import HTTPResponse

    from tradescrapr.client import build_session

    heard = []
    session = build_session(3, 0.5, notify=heard.append)
    retry = session.get_adapter("https://").max_retries
    nxt = retry.increment(method="GET", url="/x", response=HTTPResponse(status=503))
    nxt.increment(method="GET", url="/x", response=HTTPResponse(status=502))
    assert heard[0].startswith("transport retry 1/3 after HTTP 503")
    assert heard[1].startswith("transport retry 2/3 after HTTP 502")


def test_wire_bytes_fall_back_for_chunked_bodies():
    from types import SimpleNamespace

    from tradescrapr.client import _wire_bytes

    chunked = SimpleNamespace(raw=SimpleNamespace(tell=lambda: 0))
    assert _wire_bytes(chunked, 4096) == 4096
    plain = SimpleNamespace(raw=SimpleNamespace(tell=lambda: 1234))
    assert _wire_bytes(plain, 4096) == 1234


def test_retry_note_honours_retry_after():
    from urllib3.response import HTTPResponse

    from tradescrapr.client import build_session

    heard = []
    retry = build_session(3, 0.5, notify=heard.append).get_adapter("https://").max_retries
    resp = HTTPResponse(status=503, headers={"Retry-After": "120"})
    retry.increment(method="GET", url="/x", response=resp)
    assert heard == ["transport retry 1/3 after HTTP 503, waiting 120s"]
