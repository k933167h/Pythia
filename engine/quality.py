"""Post-processing that turns the council's numbers into a publishable forecast.

Order (each step recorded on the Prediction so the UI and auditors can see it):
  council consensus (raw_probability)
    → market blend      (when a matching real-money market exists)
    → Platt recalibration (per horizon, once the ledger has enough resolutions)
    → evidence gate     (flag forecasts with too few related live signals)
    → provenance stamp  (model, prompt/engine version, input hash, AI-generated label)
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Optional

from . import calibration as cal
from .config import CONFIG, ENGINE_VERSION, PROMPT_VERSION
from .models import Prediction, WorldBrief, WorldEvent

log = logging.getLogger("pythia.quality")

_platt_cache: dict = {"ts": 0.0, "n": -1, "params": {}}
_PLATT_TTL = 600.0   # refit at most every 10 minutes


def platt_params(force: bool = False) -> dict[str, dict]:
    """Per-horizon Platt parameters fitted on the ledger: {horizon: {a, b, n}}."""
    from .runtime import ledger
    pairs_by_h = ledger.calibration_pairs()
    # key on count AND outcomes, so a human override that flips a verdict refits too
    n_total = (sum(len(v) for v in pairs_by_h.values()),
               round(sum(y for v in pairs_by_h.values() for _, y in v), 3))
    now = time.monotonic()
    if (not force and _platt_cache["n"] == n_total
            and now - _platt_cache["ts"] < _PLATT_TTL):
        return _platt_cache["params"]
    params: dict[str, dict] = {}
    for h, pairs in pairs_by_h.items():
        if len(pairs) >= CONFIG.calibration_min_n:
            a, b = cal.fit_platt([p for p, _ in pairs], [y for _, y in pairs])
            params[h] = {"a": round(a, 4), "b": round(b, 4), "n": len(pairs)}
    _platt_cache.update(ts=now, n=n_total, params=params)
    return params


def input_hash(brief: Optional[WorldBrief]) -> str:
    if not brief:
        return ""
    return hashlib.sha256(brief.text.encode("utf-8")).hexdigest()[:16]


def finalize(preds: list[Prediction], events: list[WorldEvent], brief: Optional[WorldBrief],
             model: str) -> list[Prediction]:
    markets = [e for e in events if e.category == "market-odds"]
    params = platt_params() if CONFIG.calibration_enabled else {}
    ihash = input_hash(brief)
    for p in preds:
        p.raw_probability = p.probability
        prob = p.probability

        if CONFIG.market_blend_weight > 0 and markets:
            m = cal.match_market(p.statement, markets, CONFIG.market_match_threshold)
            if m:
                p.market_anchor = m
                prob = cal.blend_with_market(prob, m["probability"], CONFIG.market_blend_weight)

        p.precal_probability = round(prob, 4)
        par = params.get(p.horizon)
        if par:
            prob = cal.apply_platt(prob, par["a"], par["b"])
            p.calibrated = True

        p.evidence_count = cal.related_evidence(p.statement, p.location, events)
        p.low_evidence = p.evidence_count < CONFIG.min_evidence

        p.probability = round(cal.clamp(prob, 0.01, 0.99), 3)
        p.ai_generated = True
        p.provenance = {
            "model": model,
            "council": sorted({a.model for a in p.agents if a.model}),
            "aggregation": CONFIG.swarm_aggregation,
            "prompt_version": PROMPT_VERSION,
            "engine_version": ENGINE_VERSION,
            "input_hash": ihash,
            "generated_at": int(time.time() * 1000),
            "pipeline": [s for s, on in (("council", bool(p.agents)), ("market_blend", bool(p.market_anchor)),
                                         ("platt", p.calibrated)) if on],
        }
    return preds


def disclosure(lang: Optional[str] = None) -> dict:
    """Machine- and human-readable AI-generated notice attached to every output
    (Korea Framework Act on AI §31; EU AI Act Art. 50)."""
    lang = lang or CONFIG.output_lang
    text_ko = ("이 예측과 분석은 생성형 인공지능이 공개 데이터를 바탕으로 자동 생성한 것입니다. "
               "확률은 불확실성을 포함하며 사실이나 전문적 조언이 아닙니다. 중요한 결정 전에는 원 출처를 확인하십시오.")
    text_en = ("These forecasts and analyses were generated automatically by a generative AI system from "
               "public data. Probabilities are uncertain and are not facts or professional advice; verify "
               "against primary sources before relying on them.")
    return {"ai_generated": True, "generator": f"PYTHIA engine {ENGINE_VERSION}",
            "notice": text_ko if lang == "ko" else text_en,
            "notice_ko": text_ko, "notice_en": text_en}
