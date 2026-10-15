"""HTTP surface: authentication modes, scopes, limits, governance endpoints."""
import pytest
from fastapi.testclient import TestClient

from engine.config import CONFIG, ENGINE_VERSION
from engine.server import app


def client(**kw):
    # no `with` → lifespan (background loops, network) does not start
    return TestClient(app, **kw)


def test_local_mode_serves_loopback():
    r = client().get("/health")
    assert r.status_code == 200
    assert client().get("/predictions").status_code == 200


def test_local_mode_rejects_remote_clients():
    c = TestClient(app, client=("203.0.113.9", 5555))
    assert c.get("/predictions").status_code == 403
    assert c.get("/health").status_code == 200          # liveness stays public


def test_dns_rebinding_host_header_refused():
    r = client().get("/predictions", headers={"Host": "evil.example.com"})
    assert r.status_code == 403


@pytest.fixture
def tokens(monkeypatch):
    monkeypatch.setattr(CONFIG, "admin_token", "adm-secret-123")
    monkeypatch.setattr(CONFIG, "read_token", "read-secret-456")


def test_token_mode_scopes(tokens):
    c = TestClient(app, client=("203.0.113.9", 5555))
    assert c.get("/predictions").status_code == 401
    assert c.get("/predictions", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = c.get("/predictions", headers={"Authorization": "Bearer read-secret-456"})
    assert ok.status_code == 200
    assert ok.headers.get("x-ai-generated") == "true"
    assert ok.json()["ai_disclosure"]["ai_generated"] is True
    assert c.get("/predictions", headers={"X-API-Key": "read-secret-456"}).status_code == 200
    # read token cannot change state
    r = c.post("/model", json={"model": "x"}, headers={"Authorization": "Bearer read-secret-456"})
    assert r.status_code == 403
    r = c.post("/model", json={"model": "qwen3:8b"}, headers={"Authorization": "Bearer adm-secret-123"})
    assert r.status_code == 200
    # …and the change is in the audit trail, attributed, without the token
    entries = c.get("/governance/audit", headers={"Authorization": "Bearer adm-secret-123"}).json()["entries"]
    sw = [e for e in entries if e["action"] == "model_switch"]
    assert sw and sw[0]["detail"]["new"] == "qwen3:8b" and sw[0]["actor"].startswith("admin@")
    assert "adm-secret" not in str(entries)
    assert any(e["action"] == "access_denied" for e in entries)


def test_body_size_limit():
    r = client().post("/chat", content=b"x" * (300 * 1024), headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_rate_limit_on_costly_endpoints(monkeypatch):
    from engine import security
    from engine.state import STATE
    monkeypatch.setattr(STATE, "generating", True)       # /predict returns "already running", no LLM
    mw = app.middleware_stack                             # built lazily on first request
    if mw is None:
        client().get("/health")
    # find our middleware instance and give it a tiny bucket
    node = app.middleware_stack
    while node is not None and not isinstance(node, security.SecurityMiddleware):
        node = getattr(node, "app", None)
    assert node is not None
    monkeypatch.setattr(node, "bucket", security._Bucket(2))
    codes = [client().post("/predict").status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and 429 in codes[2:]


def test_cors_allowlist():
    ok = client().get("/health", headers={"Origin": "http://localhost:3000"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:3000"
    bad = client().get("/health", headers={"Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in bad.headers


def test_webhook_registration_ssrf_and_secret():
    c = client()
    r = c.post("/webhooks", json={"url": "http://169.254.169.254/latest/meta-data/"})
    assert r.status_code == 400
    r = c.post("/webhooks", json={"url": "https://8.8.8.8/hook"})
    assert r.status_code == 200
    assert r.json()["added"]["secret"].startswith("whsec_")
    listed = c.get("/webhooks").json()["webhooks"]
    assert listed and "secret" not in listed[0]


def test_governance_endpoints():
    c = client()
    card = c.get("/governance/system-card").json()
    assert card["version"]["engine"] == ENGINE_VERSION
    assert any("제31조" in s for ctl in card["controls"] for s in ctl["evidences"])
    v = c.get("/governance/audit/verify").json()
    assert v["audit_log"]["ok"] and v["world_archive"]["ok"]
    assert c.get("/governance/review-queue").json() == {"queue": []}
    assert c.post("/governance/review/nope", json={"verdict": "yes"}).status_code == 404
    cal = c.get("/governance/calibration").json()
    assert "platt" in cal and cal["min_n"] == CONFIG.calibration_min_n


def test_metrics_endpoint():
    client().get("/predictions")
    r = client().get("/metrics")
    assert r.status_code == 200
    assert "pythia_predictions_live" in r.text
    assert "pythia_injection_flagged_total" in r.text


def test_healthz():
    assert client().get("/healthz").json() == {"status": "ok"}


def test_read_token_may_chat_but_not_change_state(tokens, monkeypatch):
    from engine import runtime

    async def fake(messages, max_tokens=900, model=None, purpose="general", **kw):
        return "answer"
    monkeypatch.setattr(runtime.oracle, "_complete", fake)
    from engine.models import WorldBrief
    from engine.state import STATE
    monkeypatch.setattr(STATE, "world", WorldBrief(text="calm"))
    c = TestClient(app, client=("203.0.113.9", 5555))
    h = {"Authorization": "Bearer read-secret-456"}
    assert c.post("/chat", json={"message": "hi"}, headers=h).status_code == 200
    assert c.post("/loop", json={"enabled": True}, headers=h).status_code == 403


def test_chunked_body_cannot_dodge_size_cap():
    def gen():
        for _ in range(40):
            yield b"x" * 10_000
    r = client().post("/chat", content=gen(), headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_ipv6_loopback_host_header_accepted():
    assert client().get("/predictions", headers={"Host": "[::1]:8088"}).status_code == 200
    assert client().get("/predictions", headers={"Host": "[::1]"}).status_code == 200


def test_public_health_hides_config():
    body = client().get("/health").json()
    assert "config" not in body and "llm_base_url" not in str(body)


def test_forwarded_user_attributed_in_audit():
    c = client()
    c.post("/loop", json={"enabled": False}, headers={"X-Forwarded-For": "100.101.102.103"})
    entries = c.get("/governance/audit").json()["entries"]
    assert entries[0]["action"] == "loop_toggle" and entries[0]["actor"].endswith("@100.101.102.103")
