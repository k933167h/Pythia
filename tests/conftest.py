"""Test setup: isolate runtime files and start from a known security config."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# never let a developer's .env leak into the tests
for k in ("PYTHIA_ADMIN_TOKEN", "PYTHIA_READ_TOKEN", "DATA_GO_KR_KEY", "JUDGE_MODEL", "LLM_DAILY_BUDGET"):
    os.environ.pop(k, None)
os.environ.setdefault("MIROFISH_DIR", "/nonexistent")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Each test gets fresh ledger/audit files and default security settings."""
    from engine import audit, runtime, webhooks
    from engine.config import CONFIG
    from engine.ledger import Ledger
    monkeypatch.setattr(runtime, "ledger", Ledger(path=tmp_path / "ledger.jsonl"))
    new_audit = audit.AuditLog(path=tmp_path / "audit.jsonl")
    monkeypatch.setattr(audit, "AUDIT", new_audit)
    import engine.server as server
    monkeypatch.setattr(server, "AUDIT", new_audit)
    monkeypatch.setattr(webhooks, "_PATH", tmp_path / "webhooks.json")
    monkeypatch.setattr(webhooks, "HOOKS", [])
    monkeypatch.setattr(CONFIG, "admin_token", "")
    monkeypatch.setattr(CONFIG, "read_token", "")
    monkeypatch.setattr(CONFIG, "audit_sample_rate", 0.0)
    from engine import quality
    quality._platt_cache.update(ts=0.0, n=-1, params={})
    yield
