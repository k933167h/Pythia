"""System card + control mapping — PYTHIA's transparency record.

Served at GET /governance/system-card. It is generated from live configuration and
the ledger, so it never drifts from what the engine actually does.
"""
from __future__ import annotations

from .config import CONFIG, ENGINE_VERSION, PROMPT_VERSION

CONTROLS = [
    # (control, where in code, standards it evidences)
    ("Loopback-only by default; bearer tokens with read/admin scopes",
     "engine/security.py", ["OWASP LLM 2025 LLM06", "OWASP ASI03", "MCP security best practices 2025-11-25",
                            "ISO/IEC 42001 A.6.2.6", "NIST AI RMF MANAGE 2.4", "국정원 AI 보안 가이드북"]),
    ("Explicit CORS allow-list, DNS-rebinding Host check, security headers, 256 KB body cap",
     "engine/security.py · engine/server.py", ["OWASP ASVS V14", "MCP security best practices"]),
    ("Rate limit on LLM-costly endpoints + rolling 24h LLM call budget",
     "engine/security.py · engine/observability.py", ["OWASP LLM10 (Unbounded Consumption)", "NIST AI 600-1 2.7"]),
    ("Untrusted feed text normalised, injection phrases defanged, data fenced in <world_data>",
     "engine/sanitize.py", ["OWASP LLM01", "OWASP ASI01/ASI06", "NIST AI 600-1 2.9 Information Security"]),
    ("Webhook SSRF guard (all resolved IPs, at add + send, no redirects) and Standard Webhooks HMAC signing",
     "engine/netguard.py · engine/webhooks.py", ["OWASP API7 SSRF", "Standard Webhooks spec", "OWASP ASI02"]),
    ("Log-odds aggregation, market blending, per-horizon Platt recalibration",
     "engine/calibration.py · engine/quality.py", ["NIST AI RMF MEASURE 2.5 (validity)", "ISO/IEC 42001 A.6.2.4"]),
    ("Resolution criteria fixed at forecast time; deterministic resolution first; independent judge model",
     "engine/resolver.py · engine/oracle.py", ["NIST AI RMF MEASURE 2.3", "ISO/IEC 42001 A.6.2.4"]),
    ("Human review queue (sampled) and audited overrides",
     "engine/ledger.py · /governance/review-queue", ["ISO/IEC 42001 A.9", "NIST AI RMF MANAGE 4.1",
                                                     "행안부 공공부문 AI 윤리기준(인간 최종 책임)"]),
    ("Brier, log loss, ECE, skill vs climatology and vs market, yes-bias, per horizon",
     "engine/calibration.py · /scorecard", ["NIST AI RMF MEASURE 2.5/4.2", "ISO/IEC 42001 9.1"]),
    ("Tamper-evident audit trail and world-archive hash chains",
     "engine/audit.py · engine/ledger.py", ["ISO/IEC 42001 7.5 / A.6.2.8", "NIST AI RMF GOVERN 1.5"]),
    ("AI-generated labels on every output (API, webhook, brief, MCP) + provenance stamp",
     "engine/quality.py", ["인공지능기본법 제31조(투명성)", "EU AI Act Art. 50", "NIST AI 600-1 2.8"]),
    ("Prometheus metrics, liveness/readiness probes",
     "engine/observability.py · /metrics /healthz /readyz", ["NIST AI RMF MANAGE 4.1", "ISO/IEC 42001 9.1"]),
]


def system_card() -> dict:
    from .osiris_intake import FEEDS
    from .runtime import ledger, oracle
    from .state import STATE
    sc = ledger.scorecard()
    return {
        "system": "PYTHIA world-event forecasting engine",
        "version": {"engine": ENGINE_VERSION, "prompt": PROMPT_VERSION},
        "models": {"oracle": oracle.model, "judge": oracle.judge_model_name,
                   "judge_independent": oracle.judge_independent,
                   "council_overrides": dict(sorted(STATE.swarm_models.items()))},
        "intended_use": [
            "Situational awareness and horizon scanning across public global signals",
            "Probabilistic, explainable forecasts as one input to human judgement",
            "A machine-readable world view for agents, with provenance and trust flags",
        ],
        "out_of_scope_use": [
            "Automated decisions about individuals (credit, hiring, benefits, policing, healthcare) — "
            "would make the deployment high-impact AI under 인공지능기본법 제34조 and EU AI Act Annex III",
            "Sole basis for trading, emergency response or security actions without human review",
            "Presenting forecasts as facts or as official warnings (use the issuing agency's alerts)",
        ],
        "data": {
            "sources": sorted({src for _, src, _ in FEEDS}),
            "count": len(FEEDS),
            "personal_data": "Not intentionally collected; feeds are public and aggregate. Camera/aircraft "
                             "layers are visual context only and are not stored.",
            "trust": "All feed text is untrusted (see engine/sanitize.py).",
        },
        "known_limitations": [
            "Council personas share one model and one context — their errors are correlated; "
            "agreement is weaker evidence than it looks.",
            "Local models have knowledge cutoffs; back-tests on pre-release questions leak "
            "(Paleka et al. 2025; 'Simulated Ignorance Fails' 2026).",
            "Resolution evidence is limited to archived feed headlines unless a deterministic "
            "resolver applies; LLM verdicts are sampled for human review.",
            "Calibration is fitted only after CALIBRATION_MIN_N resolutions per horizon.",
        ],
        "performance": {
            "resolved": sc.get("resolved"), "brier": sc.get("brier"),
            "quality": sc.get("quality"), "resolution_methods": sc.get("resolution_methods"),
        },
        "human_oversight": {
            "review_queue": "/governance/review-queue",
            "override": "POST /governance/review/{forecast_id}",
            "audit_sample_rate": CONFIG.audit_sample_rate,
        },
        "transparency": {
            "ai_label": "Every forecast carries ai_generated=true and a provenance block; API responses "
                        "carry an ai_disclosure notice and the X-AI-Generated header.",
            "lang": CONFIG.output_lang,
        },
        "security": {
            "auth_mode": "token" if (CONFIG.admin_token or CONFIG.read_token) else "local-only",
            "cors_origins": CONFIG.cors_origins,
            "llm_rate_per_min": CONFIG.llm_rate_per_min,
            "llm_daily_budget": CONFIG.llm_daily_budget or "unlimited",
        },
        "controls": [{"control": c, "implementation": where, "evidences": std} for c, where, std in CONTROLS],
    }
