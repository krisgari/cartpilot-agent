"""API-level tests. They run only where fastapi + httpx are installed (pip install -r requirements.txt);
elsewhere they are skipped silently, so the rest of the suite still runs."""
import importlib.util as _u
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_HAVE = all(_u.find_spec(m) for m in ("fastapi", "httpx"))
if _HAVE:
    from fastapi.testclient import TestClient

    from cartpilot.agents.router import CartPilot
    from cartpilot.api import app as app_module
    from cartpilot.marketplace.store import Store
    from cartpilot.security import encode_token


def _needs_api(fn):
    return fn if _HAVE else None   # None is not collected by pytest or by the simple runner


SEED = Path(__file__).resolve().parents[1] / "data" / "seed.json"
SECRET = "test-secret"
CFG = SimpleNamespace(jwt_secret=SECRET, jwt_issuer="", jwt_audience="", env="production")


def _token(sub, role):
    return {"Authorization": "Bearer " + encode_token({"sub": sub, "role": role, "exp": int(time.time()) + 600}, SECRET)}


def _client(tmp_path):
    store = Store(tmp_path / "api.db", SEED)
    app_module._agent = CartPilot(store=store, llm=object())  # model unusable -> degrades safely, no network
    return TestClient(app_module.app), store


@_needs_api
def test_health_is_public_and_adds_trace_header(tmp_path):
    client, _ = _client(tmp_path)
    r = client.get("/health")
    assert r.status_code == 200 and r.headers["X-Trace-Id"]


@_needs_api
def test_query_requires_a_valid_token(tmp_path):
    client, _ = _client(tmp_path)
    with mock.patch("cartpilot.security.settings", CFG):
        assert client.post("/query", json={"query": "hi"}).status_code == 401
        assert client.post("/query", json={"query": "hi"}, headers={"Authorization": "Bearer x.y.z"}).status_code == 401


@_needs_api
def test_fail_closed_when_no_secret_configured(tmp_path):
    client, _ = _client(tmp_path)
    with mock.patch("cartpilot.security.settings", SimpleNamespace(jwt_secret="", jwt_issuer="", jwt_audience="", env="production")):
        r = client.post("/query", json={"query": "hi"}, headers={"X-Dev-Actor": "ops:evil"})
    assert r.status_code == 503


@_needs_api
def test_identity_comes_from_token_not_body(tmp_path):
    client, store = _client(tmp_path)
    with mock.patch("cartpilot.security.settings", CFG):
        r = client.post("/query", json={"query": "eggs missing from order o1002", "actor_id": "c2", "actor_type": "ops"},
                        headers=_token("c1", "consumer"))
    assert r.status_code == 200 and r.json()["degraded"] is True
    assert store.escalations()[0]["customer_id"] == "c1"   # the token's identity, not the body's


@_needs_api
def test_consumer_cannot_use_ops_endpoints(tmp_path):
    client, _ = _client(tmp_path)
    with mock.patch("cartpilot.security.settings", CFG):
        assert client.get("/ops/escalations", headers=_token("c1", "consumer")).status_code == 403
        assert client.post("/ops/escalations/1/resolve", json={"decision": "approve"}, headers=_token("c1", "consumer")).status_code == 403


@_needs_api
def test_ops_review_flow_issues_refund_once(tmp_path):
    from cartpilot.agents.tools import build_consumer_tools
    client, store = _client(tmp_path)
    build_consumer_tools(store, "c1")["request_refund"]("o1003", "i1", "damaged")   # $62 -> escalated
    with mock.patch("cartpilot.security.settings", CFG):
        ops = _token("maria", "ops")
        pending = client.get("/ops/escalations", headers=ops).json()
        assert len(pending) == 1 and pending[0]["kind"] == "refund_approval"
        ok = client.post(f"/ops/escalations/{pending[0]['id']}/resolve", json={"decision": "approve"}, headers=ops)
        again = client.post(f"/ops/escalations/{pending[0]['id']}/resolve", json={"decision": "approve"}, headers=ops)
    assert ok.status_code == 200 and again.status_code == 409
    assert len(store.refunds()) == 1 and store.refunds()[0]["approved_by"] == "ops:maria"


@_needs_api
def test_metrics_endpoint_serves_prometheus_text(tmp_path):
    client, _ = _client(tmp_path)
    client.get("/health")
    r = client.get("/metrics")
    assert r.status_code == 200 and "cartpilot_http_requests_total" in r.text
