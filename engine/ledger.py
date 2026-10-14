"""The forecast ledger — PYTHIA's accountability.

Every prediction is persisted the moment it's made. When its horizon expires,
an LLM judge grades it against the archived world (the top signals PYTHIA saw
during the window). Resolved forecasts get a Brier score; the scorecard shows
overall accuracy, calibration, and how each swarm persona is really doing.
Append-only JSONL in runs/ledger.jsonl — no database, no cloud.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
from pathlib import Path
from typing import Optional

from .config import CONFIG
from .models import Prediction, WorldBrief, now_ms

log = logging.getLogger("pythia.ledger")

HORIZON_MS = {
    "24h": 86_400_000,
    "week": 7 * 86_400_000,
    "month": 30 * 86_400_000,
    "year": 365 * 86_400_000,
}
GRACE = 2.0                    # keep trying to judge until due + GRACE × horizon, then void
MAX_BRIEFS = 500               # compact world archive kept in memory
CAL_BINS = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]


class Ledger:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or (CONFIG.runs_dir / "ledger.jsonl")
        self.forecasts: dict[str, dict] = {}     # id -> forecast record
        self.resolutions: dict[str, dict] = {}   # id -> resolution record
        self.briefs: list[dict] = []             # {ts, top_events} — the world archive
        self._load()

    # ── persistence ──
    def _load(self) -> None:
        if not self.path.exists():
            return
        n = 0
        for line in self.path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            kind = rec.get("kind")
            if kind == "forecast" and rec.get("id"):
                self.forecasts[rec["id"]] = rec
            elif kind == "resolution" and rec.get("id"):
                self.resolutions[rec["id"]] = rec
            elif kind == "brief":
                self.briefs.append(rec)
            n += 1
        self.briefs = self.briefs[-MAX_BRIEFS:]
        log.info("ledger loaded: %d records, %d forecasts (%d resolved)",
                 n, len(self.forecasts), len(self.resolutions))

    def _append(self, rec: dict) -> None:
        try:
            with self.path.open("a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:  # a full disk shouldn't sink a forecast pass
            log.warning("ledger write failed: %s", e)

    # ── recording ──
    def record_forecasts(self, preds: list[Prediction], brief: Optional[WorldBrief]) -> None:
        for p in preds:
            if p.id in self.forecasts:
                continue
            rec = {
                "kind": "forecast", "id": p.id, "statement": p.statement,
                "horizon": p.horizon, "probability": p.probability,
                "base_probability": p.base_probability, "location": p.location,
                "reasoning": p.reasoning, "lat": p.lat, "lng": p.lng,
                "split": p.split, "ts": p.ts,
                "resolve_after": p.ts + HORIZON_MS.get(p.horizon, HORIZON_MS["week"]),
                "agents": [{"name": a.name, "probability": a.probability, "model": a.model}
                           for a in p.agents],
                "brief_id": brief.id if brief else None,
                # v0.3 quality & governance fields
                "resolution_criteria": p.resolution_criteria,
                "raw_probability": p.raw_probability,
                "precal_probability": p.precal_probability,
                "market_probability": (p.market_anchor or {}).get("probability"),
                "market_source": (p.market_anchor or {}).get("source"),
                "evidence_count": p.evidence_count,
                "low_evidence": p.low_evidence,
                "provenance": p.provenance,
            }
            self.forecasts[p.id] = rec
            self._append(rec)

    def maybe_record_brief(self, brief: WorldBrief, min_gap_ms: int = 3_300_000,
                           events: Optional[list] = None) -> None:
        """Archive the brief's top signals, at most ~hourly — the judge's evidence base.
        500 hourly snapshots ≈ 3 weeks of world memory.

        v0.3: also keeps compact *measurable* events (quake magnitudes + coords) so some
        forecasts can be resolved deterministically, and chains each record to the
        previous one with SHA-256 so archived evidence can't be silently edited
        (OWASP ASI06 memory poisoning)."""
        if self.briefs and brief.ts - self.briefs[-1]["ts"] < min_gap_ms:
            return
        from .resolver import parse_time
        from .calibration import market_probability
        measured = []
        seismic_ok = False
        for e in (events or []):
            raw = e.raw or {}
            if e.category == "seismic":
                seismic_ok = True
                mag = raw.get("magnitude") or raw.get("mag")
                # the quake's own origin time — NOT the fetch time, or yesterday's quake still
                # in the feed would "happen" inside every new forecast's window
                origin = parse_time(raw.get("time") or raw.get("origin_time") or raw.get("timestamp")
                                    or raw.get("tmEqk") or raw.get("date"))
                if mag is not None and e.lat is not None and origin is not None:
                    try:
                        measured.append({"t": "quake", "mag": float(mag), "lat": e.lat, "lng": e.lng,
                                         "title": e.title[:120], "origin": origin})
                    except (TypeError, ValueError):
                        pass
            elif e.category == "market-odds":
                mp = market_probability(e)
                if mp is not None:
                    measured.append({"t": "market", "prob": round(mp, 3), "title": e.title[:200],
                                     "source": e.source,
                                     "close": parse_time(raw.get("close") or raw.get("close_time")
                                                         or raw.get("end_date") or raw.get("endDate")),
                                     "closed": bool(raw.get("closed") or raw.get("resolved"))})
        rec = {"kind": "brief", "ts": brief.ts, "top_events": brief.top_events[:20],
               "measured": measured[:60], "seismic_ok": seismic_ok}
        rec["prev"] = self.briefs[-1].get("hash", "") if self.briefs else ""
        rec["hash"] = _chain_hash(rec)
        self.briefs.append(rec)
        self.briefs = self.briefs[-MAX_BRIEFS:]
        self._append(rec)

    def verify_archive(self) -> dict:
        """Re-walk the hash chain of archived world snapshots."""
        broken = []
        prev = None
        for i, b in enumerate(self.briefs):
            if "hash" not in b:          # pre-v0.3 records carry no hash
                prev = None
                continue
            if b["hash"] != _chain_hash(b) or (prev is not None and b.get("prev") != prev):
                broken.append(i)
            prev = b["hash"]
        return {"records": len(self.briefs), "broken": broken, "ok": not broken}

    def resolve(self, fid: str, verdict: str, outcome: Optional[float], evidence: str,
                method: str = "llm", judge_model: str = "") -> None:
        audit = (method == "llm" and outcome is not None
                 and random.random() < max(0.0, min(1.0, CONFIG.audit_sample_rate)))
        rec = {"kind": "resolution", "id": fid, "verdict": verdict,
               "outcome": outcome, "evidence": evidence[:400], "resolved_ms": now_ms(),
               "method": method, "judge_model": judge_model, "audit": audit}
        self.resolutions[fid] = rec
        self._append(rec)

    def override(self, fid: str, verdict: str, note: str, reviewer: str = "human") -> dict:
        """Human-in-the-loop correction of a resolution (ISO 42001 / NIST MANAGE oversight)."""
        if fid not in self.forecasts:
            raise KeyError(fid)
        outcome = {"yes": 1.0, "no": 0.0}.get(verdict)
        if verdict not in ("yes", "no", "unclear"):
            raise ValueError("verdict must be yes|no|unclear")
        prev = self.resolutions.get(fid, {})
        rec = {"kind": "resolution", "id": fid, "verdict": verdict, "outcome": outcome,
               "evidence": f"[human review by {reviewer}] {note}"[:400], "resolved_ms": now_ms(),
               "method": "human", "judge_model": reviewer, "audit": False,
               "overrides": prev.get("verdict")}
        self.resolutions[fid] = rec
        self._append(rec)
        return rec

    def audit_queue(self, limit: int = 50) -> list[dict]:
        """LLM-judged resolutions sampled for human review that nobody has reviewed yet."""
        out = []
        for fid, r in self.resolutions.items():
            if r.get("audit") and r.get("method") == "llm":
                f = self.forecasts.get(fid, {})
                out.append({"id": fid, "statement": f.get("statement"), "horizon": f.get("horizon"),
                            "probability": f.get("probability"),
                            "resolution_criteria": f.get("resolution_criteria", ""),
                            "verdict": r["verdict"], "evidence": r.get("evidence", ""),
                            "judge_model": r.get("judge_model", ""), "resolved_ms": r["resolved_ms"]})
        out.sort(key=lambda x: x["resolved_ms"], reverse=True)
        return out[:limit]

    def calibration_pairs(self) -> dict[str, list[tuple[float, float]]]:
        """(pre-calibration probability, outcome) per horizon — what Platt is fitted on."""
        out: dict[str, list[tuple[float, float]]] = {}
        for fid, f in self.forecasts.items():
            r = self.resolutions.get(fid)
            if not r or r.get("outcome") is None:
                continue
            p = f.get("precal_probability")
            if p is None:
                p = f["probability"]
            out.setdefault(f["horizon"], []).append((float(p), float(r["outcome"])))
        return out

    def measured_in_window(self, start_ms: int, end_ms: int) -> list[dict]:
        """Measured records from snapshots taken during (or shortly after) the window.
        Callers must still filter on each record's own event time."""
        seen, out = set(), []
        for b in self.briefs:
            if start_ms <= b["ts"] <= end_ms + 6 * 3_600_000:
                for m in b.get("measured", []):
                    key = (m.get("title"), m.get("mag"))
                    if key not in seen:
                        seen.add(key)
                        out.append(m)
        return out

    def history_for(self, statement: str, horizon: str, cap: int = 20) -> list[dict]:
        """Probability drift: this forecast's probability across past passes.
        Statements are re-drafted each pass, so match by similarity (same horizon,
        ≥0.6 ratio) — the same trick the deck's momentum arrows use."""
        import difflib
        target = statement.lower()
        pts = []
        for f in self.forecasts.values():
            if f["horizon"] != horizon:
                continue
            r = difflib.SequenceMatcher(None, f["statement"].lower(), target).ratio()
            if r >= 0.6:
                pts.append({"ts": f["ts"], "p": f["probability"]})
        pts.sort(key=lambda x: x["ts"])
        return pts[-cap:]

    def open_recent(self, limit: int = 16) -> list[dict]:
        """Still-live forecasts (horizon not yet expired), newest first — used to
        rehydrate the deck after an engine restart."""
        now = now_ms()
        out = [f for fid, f in self.forecasts.items()
               if fid not in self.resolutions and f["resolve_after"] > now]
        out.sort(key=lambda f: f["ts"], reverse=True)
        return out[:limit]

    # ── resolution queue ──
    def due(self, now: Optional[int] = None) -> list[dict]:
        """Open forecasts past their horizon, oldest first."""
        now = now or now_ms()
        out = [f for fid, f in self.forecasts.items()
               if fid not in self.resolutions and now >= f["resolve_after"]]
        out.sort(key=lambda f: f["resolve_after"])
        return out

    @staticmethod
    def past_grace(f: dict, now: Optional[int] = None) -> bool:
        now = now or now_ms()
        horizon = HORIZON_MS.get(f["horizon"], HORIZON_MS["week"])
        return now > f["resolve_after"] + GRACE * horizon

    def window_events(self, start_ms: int, end_ms: int, cap: int = 40) -> list[str]:
        """Top world signals archived during a forecast's window (evidence for the judge)."""
        seen: list[str] = []
        for b in self.briefs:
            if start_ms <= b["ts"] <= end_ms + 6 * 3_600_000:   # small slack after the window
                for t in b.get("top_events", []):
                    if t not in seen:
                        seen.append(t)
        return seen[-cap:]

    # ── the scorecard ──
    def scorecard(self) -> dict:
        resolved = [(f, self.resolutions[fid]) for fid, f in self.forecasts.items()
                    if fid in self.resolutions and self.resolutions[fid].get("outcome") is not None]
        briers = [(f["probability"] - r["outcome"]) ** 2 for f, r in resolved]
        hits = [1 for f, r in resolved
                if (f["probability"] >= 0.5) == (r["outcome"] >= 0.5)]

        per_horizon: dict[str, dict] = {}
        for h in HORIZON_MS:
            hs = [(f, r) for f, r in resolved if f["horizon"] == h]
            if hs:
                per_horizon[h] = {
                    "resolved": len(hs),
                    "brier": round(sum((f["probability"] - r["outcome"]) ** 2 for f, r in hs) / len(hs), 3),
                    "hit_rate": round(sum(1 for f, r in hs
                                          if (f["probability"] >= 0.5) == (r["outcome"] >= 0.5)) / len(hs), 2),
                }

        calibration = []
        for lo, hi in CAL_BINS:
            bin_ = [(f, r) for f, r in resolved if lo <= f["probability"] < hi]
            if bin_:
                calibration.append({
                    "bin": f"{int(lo * 100)}–{int(min(hi, 1.0) * 100)}%",
                    "n": len(bin_),
                    "avg_predicted": round(sum(f["probability"] for f, _ in bin_) / len(bin_), 2),
                    "observed": round(sum(r["outcome"] for _, r in bin_) / len(bin_), 2),
                })

        personas: dict[str, dict] = {}
        for f, r in resolved:
            for a in f.get("agents", []):
                s = personas.setdefault(a["name"], {"n": 0, "sum": 0.0})
                s["n"] += 1
                s["sum"] += (a["probability"] - r["outcome"]) ** 2
        persona_scores = {name: {"resolved": s["n"], "brier": round(s["sum"] / s["n"], 3)}
                          for name, s in personas.items() if s["n"]}

        # per-MODEL accuracy — votes carry the model that cast them, so the
        # scorecard doubles as a local model bake-off on real-world forecasting
        by_model: dict[str, dict] = {}
        for f, r in resolved:
            for a in f.get("agents", []):
                m = a.get("model") or "(main)"
                s = by_model.setdefault(m, {"n": 0, "sum": 0.0})
                s["n"] += 1
                s["sum"] += (a["probability"] - r["outcome"]) ** 2
        model_scores = {m: {"resolved": s["n"], "brier": round(s["sum"] / s["n"], 3)}
                        for m, s in by_model.items() if s["n"]}

        unresolved = [fid for fid in self.resolutions
                      if self.resolutions[fid].get("outcome") is None]
        recent = sorted(
            ({"statement": f["statement"], "horizon": f["horizon"],
              "probability": f["probability"], "location": f.get("location", ""),
              "outcome": r["outcome"], "verdict": r["verdict"],
              "evidence": r.get("evidence", ""), "resolved_ms": r["resolved_ms"], "ts": f["ts"]}
             for f, r in resolved),
            key=lambda x: x["resolved_ms"], reverse=True)[:12]

        from . import calibration as cal
        pairs = [(f["probability"], r["outcome"]) for f, r in resolved]
        quality = {"final": cal.score_set(pairs)}
        raw_pairs = [(f["raw_probability"], r["outcome"]) for f, r in resolved
                     if f.get("raw_probability") is not None]
        if raw_pairs:
            quality["council_raw"] = cal.score_set(raw_pairs)
        mk = [(f, r) for f, r in resolved if f.get("market_probability") is not None]
        if mk:
            quality["vs_market"] = cal.score_set([(f["probability"], r["outcome"]) for f, r in mk],
                                                 baseline=[f["market_probability"] for f, _ in mk])
        quality["per_horizon"] = {h: cal.score_set([(f["probability"], r["outcome"])
                                                    for f, r in resolved if f["horizon"] == h])
                                  for h in HORIZON_MS if any(f["horizon"] == h for f, _ in resolved)}
        methods: dict[str, int] = {}
        for r in self.resolutions.values():
            m = r.get("method", "llm" if r.get("outcome") is not None else "void")
            methods[m] = methods.get(m, 0) + 1
        low_ev = [(f["probability"], r["outcome"]) for f, r in resolved if f.get("low_evidence")]

        return {
            "quality": quality,
            "resolution_methods": methods,
            "audit_pending": len(self.audit_queue(limit=10_000)),
            "low_evidence": cal.score_set(low_ev) if low_ev else {"n": 0},
            "resolved": len(resolved),
            "open": len(self.forecasts) - len(self.resolutions),
            "due": len(self.due()),
            "unresolvable": len(unresolved),
            "brier": round(sum(briers) / len(briers), 3) if briers else None,
            "hit_rate": round(len(hits) / len(resolved), 2) if resolved else None,
            "per_horizon": per_horizon,
            "calibration": calibration,
            "personas": persona_scores,
            "models": model_scores,
            "recent": recent,
        }


def _chain_hash(rec: dict) -> str:
    body = {k: v for k, v in rec.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
