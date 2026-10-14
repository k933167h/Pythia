import math
import random

import pytest

from engine import calibration as cal
from engine.models import WorldEvent


def test_logit_expit_roundtrip():
    for p in (0.01, 0.2, 0.5, 0.77, 0.99):
        assert cal.expit(cal.logit(p)) == pytest.approx(p, rel=1e-9)
    assert cal.expit(800) == pytest.approx(1.0)
    assert cal.expit(-800) == pytest.approx(0.0, abs=1e-300)


def test_aggregate_methods():
    ps = [0.6, 0.7, 0.8]
    assert cal.aggregate(ps, method="mean") == pytest.approx(0.7)
    assert cal.aggregate(ps, method="median") == pytest.approx(0.7)
    lo = cal.aggregate(ps, method="logodds")
    assert 0.69 < lo < 0.72                      # log-odds mean of symmetric-ish votes
    # extremizing pushes away from 0.5, never across it
    assert cal.aggregate(ps, method="logodds", extremize=2.0) > lo
    assert cal.aggregate([0.3, 0.4], method="logodds", extremize=2.0) < 0.35


def test_logodds_respects_confident_votes():
    # one confident voice should move the pool more in log-odds than in plain mean
    assert cal.aggregate([0.5, 0.99], method="logodds") > cal.aggregate([0.5, 0.99], method="mean") - 0.05
    assert cal.aggregate([0.5, 0.5, 0.99], method="median") == pytest.approx(0.5)


def test_weights_and_validation():
    assert cal.aggregate([0.2, 0.8], [3, 1], method="mean") == pytest.approx(0.35)
    with pytest.raises(ValueError):
        cal.aggregate([])
    with pytest.raises(ValueError):
        cal.aggregate([0.5], [1, 2])


def test_weighted_median_even_split():
    assert cal.weighted_median([0.2, 0.4], [1, 1]) == pytest.approx(0.3)


def test_blend_with_market():
    assert cal.blend_with_market(0.8, 0.8, 0.5) == pytest.approx(0.8)
    b = cal.blend_with_market(0.9, 0.3, 0.5)
    assert 0.3 < b < 0.9
    assert cal.blend_with_market(0.9, 0.3, 0.0) == pytest.approx(0.9)
    assert cal.blend_with_market(0.9, 0.3, 1.0) == pytest.approx(0.3)


def test_platt_recovers_known_miscalibration():
    random.seed(7)
    # true process: an overconfident forecaster — real prob = expit(0.5*logit(p))
    probs, ys = [], []
    for _ in range(4000):
        p = random.uniform(0.02, 0.98)
        true = cal.expit(0.5 * cal.logit(p) + 0.3)
        probs.append(p)
        ys.append(1.0 if random.random() < true else 0.0)
    a, b = cal.fit_platt(probs, ys)
    assert a == pytest.approx(0.5, abs=0.08)
    assert b == pytest.approx(0.3, abs=0.1)
    before = cal.score_set(list(zip(probs, ys)))["brier"]
    after = cal.score_set([(cal.apply_platt(p, a, b), y) for p, y in zip(probs, ys)])["brier"]
    assert after < before


def test_platt_identity_on_degenerate_data():
    assert cal.fit_platt([0.5, 0.5], [1, 0]) == pytest.approx((1.0, 0.0), abs=0.2)


def test_platt_stays_bounded_on_pathological_data():
    # regression: undamped Newton returned a≈5e4, b≈-3e10 here and squashed everything to 1%
    probs = [0.9, 0.95, 0.99] * 20
    ys = [1 if i % 10 < 3 else 0 for i in range(60)]
    a, b = cal.fit_platt(probs, ys)
    assert cal.A_RANGE[0] <= a <= cal.A_RANGE[1] and cal.B_RANGE[0] <= b <= cal.B_RANGE[1]
    before = sum(cal.log_loss(p, y) for p, y in zip(probs, ys))
    after = sum(cal.log_loss(cal.apply_platt(p, a, b), y) for p, y in zip(probs, ys))
    assert after <= before
    a, b = cal.fit_platt([0.2, 0.4, 0.6, 0.8] * 20, [0] * 80)           # all-NO horizon
    assert cal.B_RANGE[0] <= b and cal.apply_platt(0.5, a, b) > 0.04


def test_platt_never_worse_than_identity_random():
    random.seed(3)
    for _ in range(50):
        probs = [random.uniform(0.01, 0.99) for _ in range(60)]
        ys = [1.0 if random.random() < 0.4 else 0.0 for _ in range(60)]
        a, b = cal.fit_platt(probs, ys)
        ident = sum(cal.log_loss(p, y) for p, y in zip(probs, ys))
        fitted = sum(cal.log_loss(cal.apply_platt(p, a, b), y) for p, y in zip(probs, ys))
        assert fitted <= ident + 1e-9


def test_one_extreme_vote_cannot_dominate():
    assert cal.aggregate([0.6, 0.6, 0.6, 0.6, 0.0], method="logodds") > 0.4


def test_score_set_metrics():
    pairs = [(0.9, 1), (0.8, 1), (0.2, 0), (0.1, 0)]
    s = cal.score_set(pairs)
    assert s["n"] == 4
    assert s["brier"] == pytest.approx(0.025)
    assert s["base_rate"] == 0.5
    assert s["brier_skill"] == pytest.approx(1 - 0.025 / 0.25, abs=1e-3)
    assert s["yes_bias"] == pytest.approx(0.0)
    vs = cal.score_set(pairs, baseline=[0.5, 0.5, 0.5, 0.5])
    assert vs["skill_reference"] == "baseline"
    assert vs["brier_skill"] > 0.8
    assert cal.score_set([])["n"] == 0


def test_log_loss_is_capped():
    assert cal.log_loss(0.0, 1) == pytest.approx(-math.log(0.01))


def _mkt(title, **raw):
    return WorldEvent(title=title, category="market-odds", source="polymarket", raw=raw)


def test_market_probability_sources():
    assert cal.market_probability(_mkt("x", yes_prob=0.42)) == pytest.approx(0.42)
    assert cal.market_probability(_mkt("x", prob=63)) == pytest.approx(0.63)
    assert cal.market_probability(_mkt("Kalshi 71%: Will it rain?")) == pytest.approx(0.71)
    assert cal.market_probability(_mkt("no number here")) is None
    # a threshold in the question is not the crowd's odds
    assert cal.market_probability(_mkt("Will inflation exceed 3% in 2026?")) is None
    assert cal.market_probability(_mkt("x", )) is None


def test_match_market():
    mkts = [_mkt("Will Russia and Ukraine sign a ceasefire agreement by December?", yes_prob=0.12),
            _mkt("Will the Fed cut interest rates in November?", yes_prob=0.81)]
    m = cal.match_market("The Federal Reserve will cut interest rates at its November meeting", mkts, 0.45)
    assert m and m["probability"] == pytest.approx(0.81)
    assert cal.match_market("A volcano erupts in Iceland", mkts, 0.45) is None
    # polarity: a negated forecast must not inherit the YES market's price
    assert cal.match_market("Russia and Ukraine will not sign a ceasefire agreement by December",
                            mkts, 0.3) is None
    # a short title sharing one word is not "the same question"
    assert cal.match_market("Fed chair gives a speech", [_mkt("Fed hike?", yes_prob=0.1)], 0.3) is None


def test_related_evidence_counts():
    evs = [WorldEvent(title="M6.1 earthquake strikes off Japan coast"),
           WorldEvent(title="Japan earthquake aftershocks continue"),
           WorldEvent(title="Oil prices climb")]
    assert cal.related_evidence("Another strong earthquake hits Japan", "Japan", evs) >= 2


def test_feed_item_without_url_is_kept():
    # regression: a Kalshi market with no url used to raise and drop the whole feed
    from engine.osiris_intake import _kalshi_events
    evs = _kalshi_events({"markets": [{"question": "Will the Fed cut rates?", "prob": 0.85, "volume": 1}]})
    assert len(evs) == 1 and evs[0].url == ""
    assert cal.market_probability(evs[0]) == pytest.approx(0.85)
