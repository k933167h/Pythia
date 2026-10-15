"""The oracle pass: sense the world (Osiris) -> think (LLM) -> predictions."""
from __future__ import annotations

import asyncio
import difflib
import logging

from .models import Prediction, RunRecord
from .state import STATE
from .world_state import build_brief

log = logging.getLogger("pythia.pipeline")

# one oracle pass at a time (the local model is single-stream)
_lock = asyncio.Lock()


def hydrate_from_ledger() -> int:
    """Wake up remembering: reload still-live forecasts from the ledger so the
    deck and rings aren't blank while the first pass runs. Capped to the same
    per-horizon budget as a real pass — the raw ledger tail spans several passes,
    and uncapped it floods the deck with ~2× the forecasts after every restart."""
    from .config import CONFIG
    from .models import AgentView
    from .runtime import ledger
    if STATE.predictions:
        return 0
    per_horizon: dict[str, int] = {}
    preds = []
    for f in ledger.open_recent(limit=48):     # newest first; wide net, then cap
        n = per_horizon.get(f["horizon"], 0)
        if n >= CONFIG.predictions_per_horizon:
            continue
        per_horizon[f["horizon"]] = n + 1
        preds.append(Prediction(
            id=f["id"], statement=f["statement"], horizon=f["horizon"],
            probability=f.get("last_probability", f["probability"]),
            base_probability=f.get("base_probability"),
            reasoning=f.get("reasoning") or "", location=f.get("location") or "",
            lat=f.get("lat"), lng=f.get("lng"), split=bool(f.get("split")),
            agents=[AgentView(**a) for a in f.get("agents", [])],
            ts=f["ts"], brief_id=f.get("brief_id"),
        ))
    if preds:
        STATE.predictions = preds   # don't touch last_run_ms — no fresh pass happened
        STATE.publish("predictions", [p.model_dump() for p in preds])
        log.info("hydrated %d live forecasts from the ledger", len(preds))
    return len(preds)


def _carry_momentum(old: list[Prediction], new: list[Prediction]) -> None:
    """Match each fresh forecast to last run's nearest statement (same horizon)
    so the UI can show probability drift (▲▼) between passes."""
    for np_ in new:
        best, best_ratio = None, 0.0
        for op in old:
            if op.horizon != np_.horizon:
                continue
            r = difflib.SequenceMatcher(None, op.statement.lower(), np_.statement.lower()).ratio()
            if r > best_ratio:
                best, best_ratio = op, r
        if best is not None and best_ratio >= 0.6:
            np_.prev_probability = best.probability


async def refresh_world() -> int:
    """Cheap sensing pass — refresh live events + brief WITHOUT calling the LLM.
    Keeps the agent view and oracle context current between forecasts."""
    from .config import CONFIG
    from .runtime import intake, ledger
    try:
        events = await intake.fetch(limit=CONFIG.event_cap)
        STATE.events = events
        brief = build_brief(events)
        STATE.set_world(brief)
        ledger.maybe_record_brief(brief, events=events)   # ~hourly world archive for the judge
        from . import webhooks
        webhooks.fire_events(events)       # push fresh high-salience events to subscribers
        return len(events)
    except Exception as e:  # noqa: BLE001
        log.warning("sense refresh failed: %s", e)
        return 0


async def run_prediction(trigger: str = "manual") -> RunRecord:
    from .runtime import intake, oracle

    run = RunRecord(trigger=trigger, stage="queued")
    STATE.upsert_run(run)

    async def stage(name: str, info: str = "") -> None:
        run.touch(name)
        if info:
            log.info("[%s] %s: %s", run.id, name, info)
        STATE.upsert_run(run)

    async with _lock:
        STATE.set_generating(True)
        try:
            await stage("sensing", "reading Osiris feeds")
            # high cap so no single source (weather alerts, news) starves the others;
            # build_brief then takes the top few per domain.
            from .config import CONFIG
            events = await intake.fetch(limit=CONFIG.event_cap)
            STATE.events = events
            brief = build_brief(events)
            run.brief = brief
            STATE.set_world(brief)
            await stage("thinking", f"{brief.event_count} signals -> oracle")

            preds = await oracle.predict(brief, on_stage=stage)

            # swarm deliberation: a council of personas re-weighs each forecast
            from .config import CONFIG
            if CONFIG.swarm_enabled and preds:
                from .swarm import deliberate
                try:
                    preds = await deliberate(oracle, brief, preds, on_stage=stage)
                except Exception as e:  # noqa: BLE001 — never let the swarm sink a run
                    log.warning("swarm deliberation skipped: %s", e)

            # quality: market anchoring, recalibration, evidence gate, provenance + AI label
            from .quality import finalize
            preds = finalize(preds, events, brief, oracle.model)

            # ledger first: a forecast restating an open question becomes a revision of it
            # and takes that question's id, so the deck, drift and webhooks see one question
            from .runtime import ledger
            if CONFIG.track_enabled:
                ledger.record_forecasts(preds, brief)
                ledger.maybe_record_brief(brief, events=events)

            _carry_momentum(STATE.predictions, preds)
            STATE.set_predictions(preds)
            run.prediction_ids = [p.id for p in preds]

            from . import webhooks
            webhooks.fire_forecasts(preds)
            webhooks.fire_events(events)

            await stage("done", f"{len(preds)} predictions")
            import hashlib
            import json as _json
            from . import observability as obs
            from .audit import AUDIT
            from .config import PROMPT_VERSION
            from .quality import input_hash
            obs.inc("pythia_passes_total", outcome="ok", trigger=trigger)
            AUDIT.record("forecast_pass", actor=trigger, run_id=run.id, model=oracle.model,
                         prompt_version=PROMPT_VERSION, input_hash=input_hash(brief),
                         output_hash=hashlib.sha256(_json.dumps(
                             [(p.statement, p.horizon, p.probability) for p in preds]).encode()).hexdigest()[:16],
                         n_events=len(events), n_predictions=len(preds),
                         low_evidence=sum(1 for p in preds if p.low_evidence),
                         market_anchored=sum(1 for p in preds if p.market_anchor))
        except Exception as e:  # noqa: BLE001
            run.error = str(e)
            await stage("error", str(e))
            log.exception("oracle pass failed")
            from . import observability as obs
            obs.inc("pythia_passes_total", outcome="error", trigger=trigger)
        finally:
            STATE.set_generating(False)
    return run
