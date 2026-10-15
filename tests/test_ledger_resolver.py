import json

import pytest

from engine import resolver
from engine.audit import AuditLog
from engine.ledger import Ledger
from engine.models import Prediction, WorldBrief, WorldEvent, now_ms


def _pred(statement, p=0.7, horizon="24h", **kw):
    return Prediction(statement=statement, horizon=horizon, probability=p, **kw)


def _brief(ts):
    return WorldBrief(ts=ts, text="x", top_events=["a", "b"])


def test_record_resolve_override_and_queue(tmp_path, monkeypatch):
    from engine.config import CONFIG
    led = Ledger(path=tmp_path / "l.jsonl")
    p = _pred("Something happens", resolution_criteria="Reuters reports it",
              raw_probability=0.6, precal_probability=0.65)
    led.record_forecasts([p], None)
    monkeypatch.setattr(CONFIG, "audit_sample_rate", 1.0)
    led.resolve(p.id, "yes", 1.0, "E3 shows it", method="llm", judge_model="qwen3")
    q = led.audit_queue()
    assert len(q) == 1 and q[0]["resolution_criteria"] == "Reuters reports it"
    led.override(p.id, "no", "misread E3", reviewer="kim")
    assert led.audit_queue() == []                       # reviewed → out of queue
    # persistence: a fresh ledger replays the append-only file, last word wins
    led2 = Ledger(path=tmp_path / "l.jsonl")
    assert led2.resolutions[p.id]["verdict"] == "no"
    assert led2.resolutions[p.id]["method"] == "human"
    assert led2.calibration_pairs()["24h"] == [(0.65, 0.0)]
    with pytest.raises(ValueError):
        led.override(p.id, "maybe", "")
    with pytest.raises(KeyError):
        led.override("nope", "yes", "")


def test_scorecard_quality_blocks(tmp_path):
    led = Ledger(path=tmp_path / "l.jsonl")
    preds = []
    for i, (p, y, mkt) in enumerate([(0.9, 1, 0.7), (0.2, 0, 0.4), (0.7, 1, 0.6), (0.4, 0, 0.5)]):
        pr = _pred(f"event {i}", p, raw_probability=p, market_anchor={"probability": mkt, "source": "kalshi"})
        preds.append((pr, y))
    led.record_forecasts([p for p, _ in preds], None)
    for p, y in preds:
        led.resolve(p.id, "yes" if y else "no", float(y), "", method="deterministic")
    sc = led.scorecard()
    assert sc["resolved"] == 4
    assert sc["quality"]["final"]["n"] == 4
    assert sc["quality"]["vs_market"]["skill_reference"] == "baseline"
    assert sc["quality"]["vs_market"]["brier_skill"] > 0       # beat the market in this toy set
    assert sc["resolution_methods"] == {"deterministic": 4}


def test_world_archive_hash_chain_detects_tampering(tmp_path):
    led = Ledger(path=tmp_path / "l.jsonl")
    t = now_ms()
    for k in range(3):
        led.maybe_record_brief(_brief(t + k * 4_000_000), events=[])
    assert led.verify_archive()["ok"]
    led.briefs[1]["top_events"] = ["forged"]
    v = led.verify_archive()
    assert not v["ok"] and 1 in v["broken"]


def test_audit_log_chain(tmp_path):
    a = AuditLog(path=tmp_path / "audit.jsonl")
    for i in range(5):
        a.record("model_switch", actor="admin@127.0.0.1", new=f"m{i}")
    assert a.verify() == {"records": 5, "ok": True, "broken": []}
    # a restarted process continues the same chain
    b = AuditLog(path=tmp_path / "audit.jsonl")
    b.record("loop_toggle", enabled=True)
    assert b.verify()["ok"]
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    rec = json.loads(lines[2])
    rec["detail"]["new"] = "evil"
    lines[2] = json.dumps(rec)
    (tmp_path / "audit.jsonl").write_text("\n".join(lines) + "\n")
    v = b.verify()
    assert not v["ok"] and 2 in v["broken"]
    assert b.tail(2)[0]["action"] == "loop_toggle"


# ── deterministic resolver ─────────────────────────────────────────────────────────

def test_quake_threshold_parsing():
    assert resolver.quake_threshold("An M6.0+ earthquake strikes near Tokyo") == 6.0
    assert resolver.quake_threshold("A magnitude 5.5 or greater quake hits Chile") == 5.5
    assert resolver.quake_threshold("Magnitude of at least 7 earthquake in Alaska") == 7.0
    assert resolver.quake_threshold("Tensions rise in the Strait of Hormuz") is None
    assert resolver.quake_threshold("An earthquake hits Peru") is None


def _quake_event(mag, lat, lng, origin, fetched=None):
    return WorldEvent(title=f"M{mag} — somewhere {origin}", category="seismic", lat=lat, lng=lng,
                      ts=fetched or origin, raw={"magnitude": mag, "time": origin})


_HEARTBEAT = WorldEvent(title="M2.1 — far away", category="seismic", lat=-60, lng=0, raw={"magnitude": 2.1})


def _setup_quake_ledger(tmp_path, quakes, hours=24, step_h=1):
    led = Ledger(path=tmp_path / "l.jsonl")
    p = _pred("An M6.0+ earthquake strikes near Tokyo", 0.3, lat=35.7, lng=139.7)
    led.record_forecasts([p], None)
    f = led.forecasts[p.id]
    for h in range(0, hours, step_h):
        ts = f["ts"] + h * 3_600_000 + 1
        evs = [q for q in quakes if q.ts // 3_600_000 == ts // 3_600_000] + [_HEARTBEAT]
        led.maybe_record_brief(_brief(ts), min_gap_ms=0, events=evs)
    return led, f


def test_quake_resolves_yes(tmp_path):
    t0 = now_ms()
    q = _quake_event(6.4, 35.0, 140.0, t0 + 5 * 3_600_000 + 1)
    led, f = _setup_quake_ledger(tmp_path, [q])
    v = resolver.resolve(f, led)
    assert v and v[0] == "yes" and "M6.4" in v[1]


def test_quake_far_away_resolves_no_with_coverage(tmp_path):
    t0 = now_ms()
    q = _quake_event(7.0, -33.0, -71.0, t0 + 3 * 3_600_000 + 1)       # Chile
    led, f = _setup_quake_ledger(tmp_path, [q])
    v = resolver.resolve(f, led)
    assert v and v[0] == "no"


def test_quake_no_verdict_without_coverage(tmp_path):
    led, f = _setup_quake_ledger(tmp_path, [], hours=24, step_h=6)    # only 4 snapshots / 24h
    assert resolver.resolve(f, led) is None


def test_dead_seismic_feed_is_not_coverage(tmp_path):
    led = Ledger(path=tmp_path / "l.jsonl")
    p = _pred("An M6.0+ earthquake strikes near Tokyo", 0.3, lat=35.7, lng=139.7)
    led.record_forecasts([p], None)
    f = led.forecasts[p.id]
    for h in range(24):                                   # snapshots, but no seismic data at all
        led.maybe_record_brief(_brief(f["ts"] + h * 3_600_000 + 1), min_gap_ms=0, events=[])
    assert resolver.resolve(f, led) is None


def test_quake_from_before_the_forecast_does_not_count(tmp_path):
    # regression: yesterday's quake still in the feed used to resolve new forecasts YES
    t0 = now_ms()
    old = _quake_event(6.8, 35.0, 140.0, origin=t0 - 20 * 3_600_000, fetched=t0 + 2 * 3_600_000 + 1)
    led, f = _setup_quake_ledger(tmp_path, [old])
    v = resolver.resolve(f, led)
    assert v is None or v[0] == "no"


def test_negated_quake_forecast_left_to_judge(tmp_path):
    led = Ledger(path=tmp_path / "l.jsonl")
    p = _pred("No M6.0+ earthquake will strike near Tokyo", 0.8, lat=35.7, lng=139.7)
    led.record_forecasts([p], None)
    assert resolver.resolve(led.forecasts[p.id], led) is None


def test_parse_time_formats():
    assert resolver.parse_time(1_700_000_000) == 1_700_000_000_000
    assert resolver.parse_time(1_700_000_000_000) == 1_700_000_000_000
    assert resolver.parse_time("2026-10-14T00:00:00Z") == 1_791_936_000_000
    assert resolver.parse_time("20261014090000") == 1_791_936_000_000      # KST
    assert resolver.parse_time("garbage") is None and resolver.parse_time(None) is None


def test_market_settlement(tmp_path):
    led = Ledger(path=tmp_path / "l.jsonl")
    p = _pred("Will the Fed cut interest rates in November", 0.7,
              market_anchor={"probability": 0.8, "source": "kalshi"})
    led.record_forecasts([p], None)
    f = led.forecasts[p.id]
    close = f["resolve_after"] - 3_600_000
    open_97 = WorldEvent(title="Will the Fed cut interest rates in November?", category="market-odds",
                         source="kalshi", raw={"prob": 0.97, "close": close})
    led.maybe_record_brief(_brief(close - 7_200_000), min_gap_ms=0, events=[open_97])
    assert resolver.resolve(f, led) is None               # trading at 97% is not a settlement
    settled = open_97.model_copy(update={"raw": {"prob": 0.995, "close": close, "closed": True}})
    led.maybe_record_brief(_brief(f["resolve_after"] + 1000), min_gap_ms=0, events=[settled])
    v = resolver.resolve(f, led)
    assert v and v[0] == "yes"


def test_market_closing_outside_window_does_not_resolve(tmp_path):
    led = Ledger(path=tmp_path / "l.jsonl")
    p = _pred("Will the Fed cut interest rates in November", 0.7, horizon="24h",
              market_anchor={"probability": 0.8, "source": "kalshi"})
    led.record_forecasts([p], None)
    f = led.forecasts[p.id]
    later = WorldEvent(title="Will the Fed cut interest rates in November?", category="market-odds",
                       source="kalshi", raw={"prob": 0.995, "close": f["resolve_after"] + 30 * 86_400_000,
                                              "closed": True})
    led.maybe_record_brief(_brief(f["resolve_after"] + 1000), min_gap_ms=0, events=[later])
    assert resolver.resolve(f, led) is None


def test_korea_parsers():
    from engine import korea_intake as k
    eq = {"response": {"body": {"items": {"item": [
        {"mt": "3.1", "lat": "35.77", "lon": "129.19", "loc": "경북 경주시 남남서쪽 9km 지역",
         "tmEqk": "20261012041512", "dep": "12", "rem": "피해 없음"},
        {"mt": "", "loc": "bad row"}]}}}}
    evs = k.parse_earthquakes(eq)
    assert len(evs) == 1 and evs[0].raw["magnitude"] == 3.1 and k.in_korea(evs[0].lat, evs[0].lng)
    air = {"response": {"body": {"items": [
        {"stationName": "종로구", "pm25Value": "41", "pm25Grade": "3", "dataTime": "2026-10-14 22:00"},
        {"stationName": "중구", "pm25Value": "-", "pm25Grade": ""},
        {"stationName": "강남구", "pm25Value": "18"}]}}}
    a = k.parse_air(air, "서울")
    assert len(a) == 1 and "41" in a[0].title and "나쁨" in a[0].title
    assert k.parse_air({"response": {"body": {"items": ""}}}, "부산") == []
    assert not k.in_korea(35.7, 139.7)


# ── question-level dedup (revisions) ───────────────────────────────────────────────

def test_restated_forecast_becomes_revision(tmp_path):
    led = Ledger(path=tmp_path / "l.jsonl")
    a = _pred("Israel will strike Iranian nuclear facilities within the week", 0.30, horizon="week",
              raw_probability=0.3, precal_probability=0.3)
    led.record_forecasts([a], None)
    b = _pred("Israel strikes Iran's nuclear facilities this week", 0.50, horizon="week",
              raw_probability=0.5, precal_probability=0.5)
    c = _pred("The Fed cuts interest rates in November", 0.6, horizon="week")
    led.record_forecasts([b, c], None)
    assert b.id == a.id                           # took over the open question's id
    assert c.id != a.id and len(led.forecasts) == 2
    f = led.forecasts[a.id]
    assert f["n_revisions"] == 2 and f["last_probability"] == 0.5
    assert f["probability"] == pytest.approx(0.4)             # time-averaged for scoring
    assert led.calibration_pairs() == {}                       # nothing resolved yet
    assert [x["p"] for x in led.revisions_for(a.id)] == [0.3, 0.5]
    # replay from disk gives the same state
    led2 = Ledger(path=tmp_path / "l.jsonl")
    assert led2.forecasts[a.id]["probability"] == pytest.approx(0.4)
    assert led2.forecasts[a.id]["n_revisions"] == 2


def test_revision_rules(tmp_path):
    led = Ledger(path=tmp_path / "l.jsonl")
    a = _pred("A magnitude 6 earthquake strikes near Tokyo", 0.2, horizon="week")
    led.record_forecasts([a], None)
    # opposite polarity is a different question
    neg = _pred("No magnitude 6 earthquake strikes near Tokyo", 0.8, horizon="week")
    # other horizon is a different question
    other_h = _pred("A magnitude 6 earthquake strikes near Tokyo", 0.1, horizon="24h")
    led.record_forecasts([neg, other_h], None)
    assert len({a.id, neg.id, other_h.id}) == 3
    # two restatements in one pass: only one may claim the open question
    r1 = _pred("Magnitude 6 earthquake strikes near Tokyo", 0.25, horizon="week")
    r2 = _pred("A magnitude 6 earthquake strikes near Tokyo this week", 0.3, horizon="week")
    led.record_forecasts([r1, r2], None)
    assert r1.id == a.id and r2.id != a.id
    # outside the revision window → a new question
    led.forecasts[a.id]["ts"] -= 3 * 86_400_000
    late = _pred("A magnitude 6 earthquake strikes near Tokyo", 0.2, horizon="week")
    led.record_forecasts([late], None)
    assert late.id != a.id


def test_scorecard_cached_until_change(tmp_path):
    led = Ledger(path=tmp_path / "l.jsonl")
    p = _pred("Something measurable happens", 0.7)
    led.record_forecasts([p], None)
    s1 = led.scorecard()
    assert led.scorecard() is s1                               # cached
    led.resolve(p.id, "yes", 1.0, "", method="deterministic")
    s2 = led.scorecard()
    assert s2 is not s1 and s2["resolved"] == 1


def test_append_after_torn_line(tmp_path):
    path = tmp_path / "l.jsonl"
    led = Ledger(path=path)
    led.record_forecasts([_pred("first", 0.5)], None)
    with path.open("a") as f:
        f.write('{"kind": "forecast", "id": "torn')          # crash mid-write
    led.record_forecasts([_pred("second entirely different topic", 0.5)], None)
    led2 = Ledger(path=path)
    assert {f["statement"] for f in led2.forecasts.values()} == {"first", "second entirely different topic"}
