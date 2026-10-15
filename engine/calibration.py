"""Forecast quality: aggregation, market anchoring, recalibration and proper scoring.

Everything here is dependency-free math so it can be unit-tested in isolation.

Research basis (see docs/IMPROVEMENTS.md for full citations):
- Aggregate in log-odds, optionally extremized (Satopää et al. 2014; Baron et al. 2014).
  Personas share one model, so errors are correlated — default extremizing a = 1.0.
- Average the model with a real-money crowd when one exists (Halawi et al. 2024:
  system+crowd beat both; Schoenegger et al. 2024: arithmetic blending beat letting
  the model "update" on the crowd number).
- Platt scaling on the system's own resolved history (Metaculus FutureEval 2026:
  best of five post-hoc methods for bots).
- Report Brier, log score and a skill score against a baseline, not raw Brier alone
  (Paleka et al. 2025; ForecastBench).
"""
from __future__ import annotations

import math
import re
from typing import Iterable, Optional, Sequence

EPS = 1e-4          # probability floor/ceiling — no forecast is ever 0% or 100%
VOTE_FLOOR = 0.05   # individual votes are clamped to [5%, 95%] before log-odds pooling


# ── basic transforms ─────────────────────────────────────────────────────────────

def clamp(p: float, lo: float = EPS, hi: float = 1 - EPS) -> float:
    return max(lo, min(hi, float(p)))


def logit(p: float) -> float:
    p = clamp(p)
    return math.log(p / (1 - p))


def expit(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1 / (1 + z)
    z = math.exp(x)
    return z / (1 + z)


# ── aggregation ──────────────────────────────────────────────────────────────────

def aggregate(probs: Sequence[float], weights: Optional[Sequence[float]] = None,
              method: str = "logodds", extremize: float = 1.0) -> float:
    """Combine several probability estimates of the same event into one."""
    if not probs:
        raise ValueError("no probabilities to aggregate")
    ws = list(weights) if weights else [1.0] * len(probs)
    if len(ws) != len(probs):
        raise ValueError("weights and probabilities differ in length")
    if sum(ws) <= 0:
        ws = [1.0] * len(probs)
    if method == "mean":
        return clamp(sum(w * p for w, p in zip(ws, probs)) / sum(ws))
    if method == "median":
        return clamp(weighted_median(probs, ws))
    # default: weighted log-odds mean, then extremize. Inputs are clamped to [5%, 95%]
    # first — in log-odds a single 0%/100% vote would otherwise dominate the pool.
    x = sum(w * logit(clamp(p, VOTE_FLOOR, 1 - VOTE_FLOOR)) for w, p in zip(ws, probs)) / sum(ws)
    return clamp(expit(extremize * x))


def weighted_median(values: Sequence[float], weights: Sequence[float]) -> float:
    pairs = sorted(zip(values, weights))
    total = sum(w for _, w in pairs)
    acc = 0.0
    for i, (v, w) in enumerate(pairs):
        acc += w
        if acc >= total / 2:
            # exact half on an even split → average with the next value
            if math.isclose(acc, total / 2) and i + 1 < len(pairs):
                return (v + pairs[i + 1][0]) / 2
            return v
    return pairs[-1][0]


def blend_with_market(model_p: float, market_p: float, weight: float) -> float:
    """Log-odds average of the model and the crowd. weight = share given to the market."""
    weight = max(0.0, min(1.0, weight))
    return clamp(expit((1 - weight) * logit(model_p) + weight * logit(market_p)))


# ── matching a forecast to a market question ─────────────────────────────────────

_STOP = {
    "the", "a", "an", "of", "in", "on", "at", "to", "by", "for", "and", "or", "will", "be", "is",
    "are", "was", "with", "from", "than", "that", "this", "its", "it", "as", "into", "over",
    "within", "next", "before", "after", "least", "more", "less", "new", "any", "least",
    "24", "hours", "week", "month", "year", "end", "yes", "no", "kalshi", "polymarket",
    "manifold", "crowd", "odds", "market", "markets", "probability",
}
_WORD = re.compile(r"[a-z][a-z0-9\-]+|\d+(?:\.\d+)?")


def content_tokens(text: str) -> set[str]:
    toks = {t for t in _WORD.findall((text or "").lower()) if t not in _STOP and len(t) > 2}
    # crude stemming so "strikes"/"strike", "elections"/"election" meet
    return {t[:-1] if t.endswith("s") and len(t) > 4 else t for t in toks}


def similarity(a: str, b: str) -> float:
    """Overlap coefficient on content tokens — loose relatedness (evidence counting)."""
    ta, tb = content_tokens(a), content_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


_NEG = re.compile(r"\b(not|no|never|won't|wont|fails?|failed|without|neither|nor|below|under|"
                  r"fewer|less than|unlikely)\b|않|못|아니|없|미만|이하", re.I)


def negated(text: str) -> bool:
    return bool(_NEG.search(text or ""))


def match_score(a: str, b: str, min_shared: int = 3) -> float:
    """Strict same-question score: Jaccard on content tokens, 0 unless at least
    `min_shared` tokens overlap and both texts have the same polarity (a negated
    forecast must never inherit the YES price of a positive market)."""
    ta, tb = content_tokens(a), content_tokens(b)
    shared = ta & tb
    if len(shared) < min_shared or negated(a) != negated(b):
        return 0.0
    return len(shared) / len(ta | tb)


# Only explicit odds markers count — "Will inflation exceed 3%?" is a threshold, not odds.
_ODDS = re.compile(r"^(?:kalshi|polymarket|manifold)\s+(\d{1,3})\s*%|crowd odds:\s*(\d{1,3})\s*%\s*yes", re.I)


def market_probability(event) -> Optional[float]:
    """Pull the crowd's YES probability out of a market-odds WorldEvent."""
    raw = getattr(event, "raw", None) or {}
    for k in ("yes_prob", "prob", "probability"):
        v = raw.get(k)
        if v is not None:
            try:
                v = float(v)
                return clamp(v / 100 if v > 1 else v)
            except (TypeError, ValueError):
                pass
    for text in (getattr(event, "title", ""), getattr(event, "summary", "")):
        m = _ODDS.search((text or "").strip())
        if m:
            return clamp(int(m.group(1) or m.group(2)) / 100)
    return None


def match_market(statement: str, market_events: Iterable, threshold: float) -> Optional[dict]:
    """Best-matching market question for a forecast statement, or None."""
    best, best_sim = None, 0.0
    for ev in market_events:
        p = market_probability(ev)
        if p is None:
            continue
        sim = match_score(statement, ev.title)
        if sim > best_sim:
            best, best_sim = (ev, p), sim
    if best is None or best_sim < threshold:
        return None
    ev, p = best
    return {"source": ev.source, "probability": round(p, 3), "title": ev.title[:200],
            "similarity": round(best_sim, 2), "url": ev.url or ""}


def related_evidence(statement: str, location: str, events: Iterable, threshold: float = 0.34) -> int:
    """How many live signals plausibly bear on this forecast (evidence gate)."""
    probe = f"{statement} {location}"
    return sum(1 for e in events if similarity(probe, f"{e.title} {e.summary[:200]}") >= threshold)


# ── Platt scaling ────────────────────────────────────────────────────────────────

def _platt_loss(xs, ys, a, b, prior) -> float:
    eps = 1e-12
    ll = 0.0
    for x, y in zip(xs, ys):
        q = expit(a * x + b)
        ll -= y * math.log(q + eps) + (1 - y) * math.log(1 - q + eps)
    return ll + 0.5 * prior * ((a - 1) ** 2 + b ** 2)


A_RANGE = (0.25, 4.0)
B_RANGE = (-3.0, 3.0)


def fit_platt(probs: Sequence[float], outcomes: Sequence[float], iters: int = 100,
              prior: float = 1.0) -> tuple[float, float]:
    """Fit p' = expit(a·logit(p) + b) by damped Newton (step-halving line search) with
    a Gaussian prior pulling (a, b) toward the identity (1, 0). The result is clamped to
    a ∈ [0.25, 4], b ∈ [−3, 3] and kept only if it beats the identity map's log loss on
    the data — so a degenerate horizon (all NO, separable data) can never squash every
    live forecast to the floor. Returns (a, b); (1, 0) is the identity."""
    xs = [logit(p) for p in probs]
    ys = [float(y) for y in outcomes]
    if len(xs) < 2:
        return 1.0, 0.0
    a, b = 1.0, 0.0
    loss = _platt_loss(xs, ys, a, b, prior)
    for _ in range(iters):
        ga, gb = prior * (a - 1), prior * b
        haa, hab, hbb = prior, 0.0, prior
        for x, y in zip(xs, ys):
            q = expit(a * x + b)
            r = q - y
            w = q * (1 - q)
            ga += r * x
            gb += r
            haa += w * x * x
            hab += w * x
            hbb += w
        det = haa * hbb - hab * hab
        if det <= 1e-12:
            break
        da = (hbb * ga - hab * gb) / det
        db = (haa * gb - hab * ga) / det
        step = 1.0
        while step > 1e-4:
            na, nb = a - step * da, b - step * db
            nl = _platt_loss(xs, ys, na, nb, prior)
            if nl <= loss:
                break
            step /= 2
        else:
            break
        a, b, improvement = na, nb, loss - nl
        loss = nl
        if improvement < 1e-9:
            break
    if not (math.isfinite(a) and math.isfinite(b)):
        return 1.0, 0.0
    a = min(max(a, A_RANGE[0]), A_RANGE[1])
    b = min(max(b, B_RANGE[0]), B_RANGE[1])
    if _platt_loss(xs, ys, a, b, 0.0) >= _platt_loss(xs, ys, 1.0, 0.0, 0.0):
        return 1.0, 0.0
    return a, b


def apply_platt(p: float, a: float, b: float) -> float:
    return clamp(expit(a * logit(p) + b))


# ── proper scoring ───────────────────────────────────────────────────────────────

def brier(p: float, y: float) -> float:
    return (p - y) ** 2


def log_loss(p: float, y: float) -> float:
    p = clamp(p, 0.01, 0.99)       # cap so one confident miss can't dominate the mean
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def score_set(pairs: Sequence[tuple[float, float]], baseline: Optional[Sequence[float]] = None) -> dict:
    """Brier, log loss, ECE, bias, and Brier skill score for (probability, outcome) pairs.

    Skill score: 1 − Brier/Brier_ref. The reference is `baseline` (e.g. the market's
    price for the same questions) when given, else the climatological base rate —
    a forecaster who always says the base rate scores 0; negative = worse than that."""
    n = len(pairs)
    if n == 0:
        return {"n": 0}
    b = sum(brier(p, y) for p, y in pairs) / n
    ll = sum(log_loss(p, y) for p, y in pairs) / n
    base_rate = sum(y for _, y in pairs) / n
    mean_p = sum(p for p, _ in pairs) / n
    if baseline is not None and len(baseline) == n:
        ref = sum(brier(q, y) for q, (_, y) in zip(baseline, pairs)) / n
        ref_name = "baseline"
    else:
        ref = base_rate * (1 - base_rate)        # Brier of always forecasting the base rate
        ref_name = "climatology"
    bss = None if ref <= 0 else round(1 - b / ref, 3)
    # expected calibration error over 10 equal-width bins
    bins: dict[int, list[tuple[float, float]]] = {}
    for p, y in pairs:
        bins.setdefault(min(9, int(p * 10)), []).append((p, y))
    ece = sum(len(v) / n * abs(sum(p for p, _ in v) / len(v) - sum(y for _, y in v) / len(v))
              for v in bins.values())
    return {
        "n": n,
        "brier": round(b, 4),
        "log_loss": round(ll, 4),
        "brier_skill": bss,
        "skill_reference": ref_name,
        "ece": round(ece, 4),
        "mean_forecast": round(mean_p, 3),
        "base_rate": round(base_rate, 3),
        # >0 means systematically too eager to say YES (acquiescence bias, Schoenegger 2024)
        "yes_bias": round(mean_p - base_rate, 3),
    }


# ── same-question test (ledger revision dedup) ───────────────────────────────────

_NUM = re.compile(r"\d+(?:\.\d+)?")
_CAPWORD = re.compile(r"\b[A-Z][A-Za-z\-]+")
_LEAD_STOP = {"a", "an", "the", "no", "will", "there", "it", "this", "that", "by", "in", "on", "at",
              "over", "after", "before", "within", "next", "some", "any", "at least"}
# direction words: two statements on opposite sides of a pair ask different questions
_DIRECTIONS = [
    ({"cut", "cuts", "lower", "lowers", "lowered", "ease", "eases", "reduce", "reduces"},
     {"raise", "raises", "raised", "hike", "hikes", "hiked", "increase", "increases", "tighten", "tightens"}),
    ({"win", "wins", "won", "winning", "victory"}, {"lose", "loses", "lost", "losing", "defeat", "defeated"}),
    ({"above", "over", "exceed", "exceeds", "higher", "top", "tops"},
     {"below", "under", "beneath", "lower", "drops"}),
    ({"rise", "rises", "rose", "climb", "climbs", "gain", "gains", "surge", "surges", "jump", "jumps", "up"},
     {"fall", "falls", "fell", "drop", "drops", "decline", "declines", "slide", "slides", "plunge", "down"}),
    ({"approve", "approves", "approved", "pass", "passes", "passed", "accept", "accepts"},
     {"reject", "rejects", "rejected", "veto", "vetoes", "block", "blocks", "fail", "fails"}),
    ({"enter", "enters", "join", "joins", "open", "opens", "reopen", "reopens", "resume", "resumes"},
     {"exit", "exits", "leave", "leaves", "close", "closes", "shut", "shuts", "halt", "halts", "suspend"}),
    ({"인하", "상승", "승리", "통과", "재개"}, {"인상", "하락", "패배", "부결", "중단"}),
]


def _numbers(text: str) -> set[float]:
    return {float(n) for n in _NUM.findall(text or "")}


def _side(words: set[str], text: str, pair) -> Optional[int]:
    a = bool(words & pair[0]) or any(w in text for w in pair[0] if not w.isascii())
    b = bool(words & pair[1]) or any(w in text for w in pair[1] if not w.isascii())
    return 0 if a and not b else 1 if b and not a else None


def _entity_seq(text: str, other: str) -> list[str]:
    """Proper-noun-ish tokens in order, normalised to a 4-letter stem (Iran/Iranian/Iran's).
    A capitalised word that the other statement uses in lower case (sentence-initial
    "Magnitude" vs "a magnitude") is ordinary vocabulary, not an entity; a leading
    stop-word ("The", "Will") never counts."""
    other_lower_words = set(re.findall(r"[a-z][a-z\-]+", other or ""))   # words written lower-case there
    out = []
    for m in _CAPWORD.finditer(text or ""):
        w = m.group(0)
        lw = w.lower()
        if lw in _LEAD_STOP:
            continue
        if lw in other_lower_words:
            continue
        out.append(lw[:4])
    return out


def same_question(a: str, b: str, min_jaccard: float = 0.5) -> bool:
    """Conservative test that two forecast statements ask the same question. A missed
    merge only costs a duplicate record; a false merge corrupts the track record — so
    every guard must pass: same polarity, same numbers, no opposite direction words,
    the same named entities in the same order, and enough shared content words."""
    if negated(a) != negated(b):
        return False
    if _numbers(a) != _numbers(b):
        return False
    al, bl = (a or "").lower(), (b or "").lower()
    aw, bw = set(re.findall(r"[a-z]+", al)), set(re.findall(r"[a-z]+", bl))
    for pair in _DIRECTIONS:
        sa, sb = _side(aw, al, pair), _side(bw, bl, pair)
        if sa is not None and sb is not None and sa != sb:
            return False
        if (sa is None) != (sb is None) and (sa is not None or sb is not None):
            # one states a direction the other doesn't mention — different claims
            return False
    if _entity_seq(a, b) != _entity_seq(b, a):
        return False
    ta, tb = content_tokens(a), content_tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= min_jaccard
