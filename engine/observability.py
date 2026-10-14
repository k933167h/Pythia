"""Lightweight metrics + LLM budget guard (no extra dependencies).

GET /metrics renders Prometheus text format so any standard scraper (Prometheus,
Grafana Agent, Datadog) can watch PYTHIA. The LLM budget enforces a rolling-24h
ceiling on model calls — OWASP LLM "unbounded consumption" control.
"""
from __future__ import annotations

import threading
import time
from collections import deque

from .config import CONFIG

_lock = threading.Lock()
_counters: dict[tuple[str, tuple], float] = {}
_hist: dict[str, list[float]] = {}          # name -> [count, sum]
_HELP = {
    "pythia_llm_calls_total": "LLM completion calls by purpose and outcome",
    "pythia_llm_latency_seconds": "LLM call latency",
    "pythia_passes_total": "Oracle passes by outcome",
    "pythia_feed_failures_total": "Feed fetch failures by source",
    "pythia_injection_flagged_total": "Untrusted feed strings that matched prompt-injection patterns",
    "pythia_auth_failures_total": "Rejected API requests",
    "pythia_rate_limited_total": "Requests refused by the rate limiter",
    "pythia_webhook_deliveries_total": "Webhook delivery attempts by outcome",
    "pythia_resolutions_total": "Forecast resolutions by method",
}


def inc(name: str, value: float = 1.0, **labels) -> None:
    key = (name, tuple(sorted(labels.items())))
    with _lock:
        _counters[key] = _counters.get(key, 0.0) + value


def observe(name: str, value: float) -> None:
    with _lock:
        h = _hist.setdefault(name, [0.0, 0.0])
        h[0] += 1
        h[1] += value


def render(extra_gauges: dict[str, float] | None = None) -> str:
    from .sanitize import STATS
    lines: list[str] = []
    with _lock:
        names = sorted({k[0] for k in _counters} | set(_hist))
        for n in names:
            if n in _HELP:
                lines.append(f"# HELP {n} {_HELP[n]}")
            if n in _hist:
                lines.append(f"# TYPE {n} summary")
                c, s = _hist[n]
                lines.append(f"{n}_count {c:g}")
                lines.append(f"{n}_sum {s:.6f}")
                continue
            lines.append(f"# TYPE {n} counter")
            for (name, labels), v in sorted(_counters.items()):
                if name != n:
                    continue
                lab = ",".join(f'{k}="{str(val)}"' for k, val in labels)
                lines.append(f"{n}{{{lab}}} {v:g}" if lab else f"{n} {v:g}")
    lines.append("# TYPE pythia_injection_flagged_total counter")
    lines.append(f"pythia_injection_flagged_total {STATS['flagged']}")
    for k, v in (extra_gauges or {}).items():
        lines.append(f"# TYPE {k} gauge")
        lines.append(f"{k} {v:g}")
    return "\n".join(lines) + "\n"


class BudgetExceeded(RuntimeError):
    pass


class LLMBudget:
    """Rolling 24-hour cap on LLM calls (0 = unlimited)."""

    def __init__(self) -> None:
        self._calls: deque[float] = deque()

    def _trim(self, now: float) -> None:
        while self._calls and now - self._calls[0] > 86_400:
            self._calls.popleft()

    def check_and_count(self) -> None:
        cap = CONFIG.llm_daily_budget
        now = time.time()
        with _lock:
            self._trim(now)
            if cap > 0 and len(self._calls) >= cap:
                raise BudgetExceeded(f"LLM daily budget of {cap} calls reached")
            self._calls.append(now)

    def used(self) -> int:
        with _lock:
            self._trim(time.time())
            return len(self._calls)


BUDGET = LLMBudget()
