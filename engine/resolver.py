"""Deterministic resolution — settle forecasts from measured data before asking any LLM.

An LLM judge grading its own model's forecasts has a conflict of interest
(self-preference: Panickssery et al., NeurIPS 2024). Wherever a forecast is about a
measurable quantity, resolve it with arithmetic instead:

* Earthquakes: "M6+ earthquake near X within a week" → any archived USGS quake with
  magnitude ≥ threshold within RADIUS_KM of the forecast's coordinates in the window.
* Prediction markets: a forecast anchored to a real-money market resolves only when
  that market has closed inside the forecast window at a decisive price (≥99% / ≤1%).

Negated statements ("no M6+ quake…") are never auto-resolved — polarity errors would
flip the verdict — and go to the judge instead.

Returns None when the data can't decide — the caller then falls back to the judge.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import calibration as cal

RADIUS_KM = 600.0
# The global USGS feed is complete only for larger quakes; never conclude "it did not
# happen" for thresholds the feed may simply not report.
MIN_MAG_FOR_NO = 5.0
MIN_COVERAGE = 0.5          # share of the window's hours that must have archived snapshots

_QUAKE = re.compile(r"earthquake|quake|seismic|tremor", re.I)
_MAG = re.compile(r"(?:\bM(?:w|agnitude)?\s*(?:≥|>=|>|of at least|at least)?\s*|magnitude\s*(?:≥|>=|>|of at least|at least)?\s*)(\d(?:\.\d)?)\s*\+?", re.I)


_KST = timezone(timedelta(hours=9))


def parse_time(v: Any) -> Optional[int]:
    """Epoch ms from epoch seconds/ms, ISO-8601, or KMA-style YYYYMMDDHHMM[SS] (KST)."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        return int(x if x > 1e11 else x * 1000) if x > 0 else None
    s = str(v).strip()
    if re.fullmatch(r"\d{10,13}", s):
        return parse_time(int(s))
    if re.fullmatch(r"\d{12}(\d{2})?", s):
        fmt = "%Y%m%d%H%M%S" if len(s) == 14 else "%Y%m%d%H%M"
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=_KST).timestamp() * 1000)
        except ValueError:
            return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def quake_threshold(statement: str) -> Optional[float]:
    if not _QUAKE.search(statement or ""):
        return None
    m = _MAG.search(statement)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    return v if 2.0 <= v <= 9.9 else None


def coverage(ledger, start_ms: int, end_ms: int) -> float:
    """Share of the window's hours with a snapshot in which the seismic feed actually
    returned data — a dead feed must never look like 'no quake happened'."""
    hours = max(1.0, (end_ms - start_ms) / 3_600_000)
    n = sum(1 for b in ledger.briefs if start_ms <= b["ts"] <= end_ms and b.get("seismic_ok"))
    return min(1.0, n / hours)


def resolve_quake(forecast: dict, ledger) -> Optional[tuple[str, str]]:
    statement = forecast.get("statement", "")
    thr = quake_threshold(statement)
    lat, lng = forecast.get("lat"), forecast.get("lng")
    if thr is None or lat is None or lng is None or cal.negated(statement):
        return None          # negated phrasing ("no M6+ quake…") is left to the judge
    start, end = forecast["ts"], forecast["resolve_after"]
    hits = [m for m in ledger.measured_in_window(start, end)
            if m.get("t") == "quake" and m.get("mag", 0) >= thr and m.get("lat") is not None
            and m.get("origin") is not None and start <= m["origin"] <= end
            and haversine_km(lat, lng, m["lat"], m["lng"]) <= RADIUS_KM]
    if hits:
        best = max(hits, key=lambda m: m["mag"])
        return "yes", f"measured: {best['title']} (M{best['mag']}) within {int(RADIUS_KM)} km"
    if thr >= MIN_MAG_FOR_NO and coverage(ledger, start, end) >= MIN_COVERAGE:
        return "no", f"measured: no M{thr}+ quake within {int(RADIUS_KM)} km in archived USGS data"
    return None


def resolve_market(forecast: dict, ledger, min_score: float = 0.6) -> Optional[tuple[str, str]]:
    """Settle from a prediction market only when that market has itself *closed* inside
    the forecast's window at a decisive price. An open market at 97% is not a
    settlement, and a market closing outside the window answers a different question."""
    if forecast.get("market_probability") is None:
        return None
    statement = forecast.get("statement", "")
    start, end = forecast["ts"], forecast["resolve_after"]
    latest: Optional[dict] = None
    for b in ledger.briefs:
        if b["ts"] < start:
            continue
        for m in b.get("measured", []):
            if m.get("t") != "market" or cal.match_score(statement, m.get("title", "")) < min_score:
                continue
            close = m.get("close")
            if close is None or not (start <= close <= end):
                continue
            if b["ts"] < close and not m.get("closed"):
                continue                       # price observed before the market closed
            if latest is None or b["ts"] >= latest["_ts"]:
                latest = {**m, "_ts": b["ts"]}
    if not latest or latest.get("prob") is None:
        return None
    p = latest["prob"]
    if p >= 0.99:
        return "yes", f"market settled YES: {latest['title'][:120]} ({round(p * 100)}%)"
    if p <= 0.01:
        return "no", f"market settled NO: {latest['title'][:120]} ({round(p * 100)}%)"
    return None


def resolve(forecast: dict, ledger) -> Optional[tuple[str, str]]:
    for fn in (resolve_quake, resolve_market):
        out = fn(forecast, ledger)
        if out:
            return out
    return None
