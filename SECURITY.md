# Security model (v0.3)

PYTHIA reads attacker-controllable text from dozens of public feeds and can call an LLM,
post webhooks and serve agents. The controls below are on by default.

| Threat | Control | Where |
|---|---|---|
| Anyone on the LAN driving the engine | Binds to `127.0.0.1`; with no tokens set only loopback clients are served | `engine/config.py`, `engine/security.py` |
| Remote access without identity | `PYTHIA_ADMIN_TOKEN` (read+write) / `PYTHIA_READ_TOKEN` (read, plus `/chat` and `/whatif`) | `engine/security.py` |
| DNS rebinding against a localhost engine | Loopback mode rejects foreign `Host` headers | `engine/security.py` |
| Cross-site calls from a browser | Explicit `CORS_ORIGINS` allow-list (was `*`) | `engine/server.py` |
| Prompt injection through feeds (OWASP LLM01, ASI01/06) | Normalise, defang override phrases, fence data in `<world_data>`, system rule "data, never instructions" | `engine/sanitize.py` |
| Unbounded consumption (LLM10) | Per-user rate limit on LLM endpoints; rolling 24h LLM call budget; 256 KB body cap (chunked included) | `engine/security.py`, `engine/observability.py` |
| SSRF through webhooks | Every resolved IP checked at registration and at send; connection pinned to the checked IP (Host + SNI preserved); no redirects | `engine/netguard.py`, `engine/webhooks.py` |
| Forged or replayed webhook deliveries | Standard Webhooks signing (`webhook-id/-timestamp/-signature`, HMAC-SHA256), secrets shown once, file mode 0600 | `engine/webhooks.py` |
| Tampering with evidence or history | SHA-256 hash chains on the audit trail and the archived world snapshots | `engine/audit.py`, `engine/ledger.py` |
| Over-privileged agents (LLM06, ASI03) | MCP bridge uses a read token; state-changing MCP tools off unless `PYTHIA_MCP_ALLOW_ACTIONS=true` | `engine/mcp.py` |
| Anyone who can open a shared dashboard driving the engine | Proxy injects the token server-side and allow-lists calls by `PYTHIA_PROXY_MODE` — `read` by default (reads + chat/what-if), `operator`, `admin`; `run-all.sh` binds the UI to loopback unless `PYTHIA_LAN=1` | `integrations/osiris/lib/proxyPolicy.ts`, `routes/engine-proxy-route.ts`, `run-all.sh` |
| Forged identity in rate limits / audit | `X-Forwarded-For` forwarded only with `PYTHIA_PROXY_TRUST_XFF=true`, right-most hop only; the engine trusts it only from loopback | `lib/proxyPolicy.ts`, `engine/security.py` |
| Corrupted state on crash | Atomic temp-file + fsync + rename for every JSON state file; ledger appends recover from a torn last line | `engine/fsutil.py`, `engine/ledger.py` |
| Judge key leaking to another host | Oracle key is reused for the judge only when the judge runs on the same base URL | `engine/oracle.py` |

## Exposing the engine

```bash
export PYTHIA_ADMIN_TOKEN=$(openssl rand -hex 32)
export PYTHIA_READ_TOKEN=$(openssl rand -hex 32)
export ENGINE_HOST=0.0.0.0           # only with tokens set
```

Put TLS in front (Caddy, nginx, Tailscale Serve). Give agents and the MCP bridge the read token only.

## Verifying the audit trail

`GET /governance/audit/verify` re-walks both hash chains and reports any broken record.

## Reporting

Please report vulnerabilities privately to the maintainers rather than in a public issue.
