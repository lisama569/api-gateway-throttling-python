"""版本协商场景：默认版本、显式接受、明确拒绝、跨入口一致。"""

import pytest

from gateway.config import default_config
from gateway.errors import VersionUnsupportedError
from gateway.versioning import VersionNegotiator


def test_decide_defaults_when_unspecified():
    negotiator = VersionNegotiator(default_config())
    assert negotiator.decide(None) == "v1"


@pytest.mark.parametrize("raw,expected", [("v1", "v1"), ("V2", "v2"), (" v2 ", "v2")])
def test_decide_accepts_supported_versions(raw, expected):
    negotiator = VersionNegotiator(default_config())
    assert negotiator.decide(raw) == expected


@pytest.mark.parametrize("raw", ["v9", "", "   ", "2", "v3"])
def test_decide_rejects_unsupported(raw):
    negotiator = VersionNegotiator(default_config())
    with pytest.raises(VersionUnsupportedError) as exc:
        negotiator.decide(raw)
    assert exc.value.details["supported_versions"] == ["v1", "v2"]
    assert exc.value.details["requested_version"] == raw.strip()


async def test_http_default_version_is_v1(client, gateway_logs):
    resp = await client.get("/api/echo", headers={"X-Request-Id": "ver-default"})
    assert resp.status_code == 200
    assert resp.json()["version"] == "v1"
    assert resp.headers["x-request-id"] == "ver-default"
    assert any(
        r.request_id if False else "ver-default" in r.getMessage()
        and "version_accepted" in r.getMessage()
        for r in gateway_logs.records
    )


async def test_http_unsupported_version_rejected(client, gateway_logs):
    resp = await client.get(
        "/api/echo",
        headers={"X-API-Version": "v9", "X-Request-Id": "ver-reject-1"},
    )
    assert resp.status_code == 400
    body = resp.json()["error"]
    assert body["category"] == "version_unsupported"
    assert body["request_id"] == "ver-reject-1"
    assert body["details"]["requested_version"] == "v9"
    assert any("ver-reject-1" in r.getMessage() and "version_denied" in r.getMessage()
               for r in gateway_logs.records), [r.getMessage() for r in gateway_logs.records]


async def test_same_version_same_verdict_across_entries(client):
    """同一不受支持版本在 /api 与 /internal 两个入口下结论一致。"""

    for entry in ("/api/echo", "/internal/echo"):
        resp_ok = await client.get(entry, headers={"X-API-Version": "v2"})
        resp_bad = await client.get(entry, headers={"X-API-Version": "v3"})
        assert resp_ok.status_code == 200
        assert resp_ok.json()["version"] == "v2"
        assert resp_bad.status_code == 400
        assert resp_bad.json()["error"]["category"] == "version_unsupported"


async def test_empty_version_header_is_rejected(client):
    resp = await client.get("/api/echo", headers={"X-API-Version": "   "})
    assert resp.status_code == 400
    assert resp.json()["error"]["category"] == "version_unsupported"
