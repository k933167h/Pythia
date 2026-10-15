"""Outbound webhooks — PYTHIA pushes the world to you.

Register any URL and the engine POSTs JSON when something crosses your
thresholds: high-probability forecasts after each oracle pass, and fresh
high-salience world events as the sensing loop spots them. Hooks persist in
runs/webhooks.json. Fire-and-forget: a dead endpoint never slows the oracle.

v0.3 hardening (Standard Webhooks spec + SSRF guard):
  * every hook gets a `whsec_` signing secret (shown once, at registration);
  * deliveries carry webhook-id / webhook-timestamp / webhook-signature headers —
    signature = "v1," + base64(HMAC-SHA256(secret, f"{id}.{timestamp}.{body}"));
  * targets are SSRF-checked at registration *and* at every send, redirects are not
    followed, failures are retried with exponential backoff + jitter (3 attempts),
    and 410 Gone disables the hook;
  * every payload carries an `ai_disclosure` block (AI 기본법 §31 / EU AI Act Art. 50).

Payloads:
  {"kind": "forecasts", "ts": <ms>, "forecasts": [{statement, horizon, probability, location, lat, lng, reasoning}]}
  {"kind": "events",    "ts": <ms>, "events":    [{title, category, source, salience, lat, lng}]}
  {"kind": "alerts",    "ts": <ms>, "alerts":    [{rule_name, kind, title, body, lat, lng, ts}]}
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import random
import secrets
import uuid

import httpx

from . import observability as obs
from .fsutil import atomic_write_json
from .netguard import UnsafeURL, check_url, pinned

from .config import CONFIG, HTTPX_VERIFY
from .models import now_ms

log = logging.getLogger("pythia.webhooks")

_PATH = CONFIG.runs_dir / "webhooks.json"
_seen_titles: set[str] = set()          # events already announced (dedupe across senses)
_tasks: set = set()                     # strong refs so in-flight deliveries aren't GC'd
_MAX_SEEN = 6000


def load() -> list[dict]:
    try:
        data = json.loads(_PATH.read_text())
        return [h for h in data if isinstance(h, dict) and h.get("url")] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


HOOKS: list[dict] = load()


def _save() -> None:
    try:
        atomic_write_json(_PATH, HOOKS, mode=0o600)   # holds signing secrets
    except OSError as e:
        log.warning("webhooks save failed: %s", e)


def _public(hook: dict) -> dict:
    """A hook as the API shows it — never the secret."""
    return {k: v for k, v in hook.items() if k != "secret"}


def public_hooks() -> list[dict]:
    return [_public(h) for h in HOOKS]


def add(url: str, min_probability: float = 0.7, min_salience: float = 0.85) -> dict:
    """Register (or re-register) a hook. Returns it *with* its secret — the only time
    the secret is ever shown. Raises UnsafeURL for SSRF-unsafe targets."""
    url = check_url(url)
    hook = {"url": url.strip(),
            "min_probability": max(0.0, min(1.0, float(min_probability))),
            "min_salience": max(0.0, min(1.0, float(min_salience))),
            "secret": "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode(),
            "enabled": True}
    global HOOKS
    HOOKS = [h for h in HOOKS if h["url"] != hook["url"]] + [hook]
    _save()
    return hook


def remove(url: str) -> bool:
    global HOOKS
    before = len(HOOKS)
    HOOKS = [h for h in HOOKS if h["url"] != url.strip()]
    if len(HOOKS) != before:
        _save()
    return len(HOOKS) != before


def sign(secret: str, msg_id: str, ts: int, body: bytes) -> str:
    key = secret[6:] if secret.startswith("whsec_") else secret
    try:
        raw = base64.b64decode(key)
    except (ValueError, TypeError):
        raw = key.encode()
    mac = hmac.new(raw, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def verify(secret: str, headers: dict, body: bytes, tolerance_sec: int = 300) -> bool:
    """Receiver-side check (also used in tests): constant-time, with a replay window."""
    import time as _t
    try:
        ts = int(headers["webhook-timestamp"])
        msg_id = headers["webhook-id"]
        sigs = headers["webhook-signature"].split()
    except (KeyError, ValueError):
        return False
    if abs(_t.time() - ts) > tolerance_sec:
        return False
    expected = sign(secret, msg_id, ts, body)
    return any(hmac.compare_digest(expected, s) for s in sigs)


def _with_disclosure(body: dict) -> dict:
    from .quality import disclosure
    return {**body, "ai_disclosure": disclosure()}


async def _post(hook: dict, body: dict) -> None:
    import time as _t
    url = hook["url"]
    try:
        target, pin_headers, ext = pinned(url)   # re-check + pin to the validated IP
    except UnsafeURL as e:
        log.warning("webhook %s refused at send time: %s", url, e)
        obs.inc("pythia_webhook_deliveries_total", outcome="refused")
        return
    payload = json.dumps(_with_disclosure(body), ensure_ascii=False).encode("utf-8")
    msg_id = "msg_" + uuid.uuid4().hex
    for attempt in range(3):
        ts = int(_t.time())
        headers = {"content-type": "application/json", "webhook-id": msg_id,
                   "webhook-timestamp": str(ts), "user-agent": "PYTHIA-Webhooks/0.3"}
        if hook.get("secret"):
            headers["webhook-signature"] = sign(hook["secret"], msg_id, ts, payload)
        try:
            async with httpx.AsyncClient(verify=HTTPX_VERIFY, timeout=15, follow_redirects=False) as c:
                r = await c.post(target, content=payload, headers={**headers, **pin_headers},
                                 extensions=ext)
            if r.status_code == 410:
                hook["enabled"] = False
                _save()
                obs.inc("pythia_webhook_deliveries_total", outcome="gone")
                return
            if r.status_code < 300:
                obs.inc("pythia_webhook_deliveries_total", outcome="ok")
                return
        except Exception as e:  # noqa: BLE001 — a dead endpoint is the subscriber's problem
            log.debug("webhook %s attempt %d failed: %s", url, attempt + 1, e)
        await asyncio.sleep((2 ** attempt) + random.random())
    obs.inc("pythia_webhook_deliveries_total", outcome="failed")


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def _live() -> list[dict]:
    return [h for h in HOOKS if h.get("enabled", True)]


def fire_forecasts(preds: list) -> None:
    """After an oracle pass: push forecasts above each hook's probability bar."""
    for hook in _live():
        sel = [{"statement": p.statement, "horizon": p.horizon, "probability": p.probability,
                "location": p.location, "lat": p.lat, "lng": p.lng, "reasoning": p.reasoning,
                "resolution_criteria": p.resolution_criteria, "low_evidence": p.low_evidence,
                "market_anchor": p.market_anchor, "provenance": p.provenance}
               for p in preds if p.probability >= hook["min_probability"]]
        if sel:
            _spawn(_post(hook, {"kind": "forecasts", "ts": now_ms(), "forecasts": sel}))


def fire_alerts(items: list[dict]) -> None:
    """A user-defined alert rule fired — push to every hook, no thresholds:
    these are explicit rules, so the subscriber asked for exactly this."""
    if not items:
        return
    body = {"kind": "alerts", "ts": now_ms(),
            "alerts": [{k: it.get(k) for k in ("rule_name", "kind", "title", "body", "lat", "lng", "ts")}
                       for it in items]}
    for hook in _live():
        _spawn(_post(hook, body))


def fire_events(events: list) -> None:
    """After a sensing pass: push never-before-seen events above the salience bar."""
    global _seen_titles
    fresh = [e for e in events if e.title not in _seen_titles]
    _seen_titles.update(e.title for e in fresh)
    if len(_seen_titles) > _MAX_SEEN:                      # keep the dedupe set bounded
        _seen_titles = set(list(_seen_titles)[-_MAX_SEEN // 2:])
    if not fresh:
        return
    for hook in _live():
        sel = [{"title": e.title, "category": e.category, "source": e.source,
                "salience": e.salience, "lat": e.lat, "lng": e.lng}
               for e in fresh if e.salience >= hook["min_salience"]]
        if sel:
            _spawn(_post(hook, {"kind": "events", "ts": now_ms(), "events": sel}))
