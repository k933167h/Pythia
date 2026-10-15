"""Feed parsing: salience scoring, coordinates, ticker themes."""
import pytest

from engine.models import Prediction
from engine.osiris_intake import _coord, _salience
from engine.tickers import watch_from_predictions


@pytest.mark.parametrize("text", [
    "Microsoft releases software update", "Oscar award ceremony tonight", "Forward guidance unchanged",
    "Stewart wins race", "Coupon savings this weekend", "Patriot League final", "Skilled workers wanted",
    "Default browser settings change", "핵심 산업 동향", "Brainstorm session",
])
def test_no_false_positive_keywords(text):
    assert _salience(text, "", {}) == 0.4


@pytest.mark.parametrize("text,expected", [
    ("Russia launches missile attack on Kyiv", 0.95),
    ("War breaks out along the border", 1.0),
    ("Flood Warning issued for Harris County", 0.8),     # a flood, not a war
    ("Thousands evacuated as wildfire spreads", 0.85),
    ("Argentina defaults on sovereign bonds", 0.7),
    ("북한 탄도미사일 발사", 0.95),
    ("경북 경주 지진 발생", 0.7),
    ("태풍 북상, 남부 대피령", 0.9),
    ("비상계엄 선포", 0.95),
])
def test_true_positive_keywords(text, expected):
    assert _salience(text, "", {}) == pytest.approx(expected)


def test_upstream_scores():
    assert _salience("x", "", {"risk_score": True}) == 0.4          # bool is not a score
    assert _salience("x", "", {"risk_score": 1}) == 0.4             # 1 out of 100
    assert _salience("x", "", {"risk_score": 85}) == 0.85
    assert _salience("x", "", {"severity_score": 0.9}) == 0.9
    assert _salience("x", "", {"risk_score": 0, "severity_score": 70}) == 0.4   # 0 is a real score


def test_zero_coordinates_survive():
    assert _coord({"lat": 0.0, "lng": 32.5}) == (0.0, 32.5)
    assert _coord({"latitude": -1.2, "lon": 0}) == (-1.2, 0.0)
    assert _coord({"geometry": {"coordinates": [0, 51.48]}}) == (51.48, 0.0)
    assert _coord({"lat": 200, "lng": 1}) == (None, None)
    assert _coord({"lat": "", "lng": 3}) == (None, None)
    assert _coord({}) == (None, None)


def _syms(statement):
    return {w["symbol"] for w in watch_from_predictions([Prediction(statement=statement, horizon="week",
                                                                    probability=0.6)])}


def test_ticker_themes_use_word_boundaries():
    assert _syms("Quarterly earnings report beats; software award; soil quality study") == set()
    s = _syms("War escalates and oil spikes as Hormuz ports close")
    assert {"ITA", "CL=F", "ZIM"} <= s


def test_alert_keywords_whole_words():
    from engine.alerts import term_hit
    assert term_hit("war", "war breaks out")
    assert term_hit("war", "two wars")
    assert not term_hit("war", "flood warning issued")
    assert not term_hit("war", "new software release")
    assert term_hit("evacuat*", "residents evacuated")
    assert term_hit("지진", "경주에서 지진이 발생")
    assert term_hit("taiwan strait", "tension in the taiwan strait rises")


def test_korean_false_positives_and_punctuated_alert_terms():
    from engine.alerts import term_hit
    assert _salience("회의 분위기 좋아", "", {}) == 0.4
    assert _salience("폭발적인 인기", "", {}) == 0.4
    assert _salience("금융 위기 우려", "", {}) == 0.75
    assert term_hit("u.s.", "new u.s. sanctions")
    assert term_hit("m7+", "an m7+ quake")
    assert term_hit("$tsla", "$tsla jumps")
    assert not term_hit("war", "warning")


def test_compact_never_empty():
    from engine.world_state import compact
    text = "[SEISMIC] (1 signals)\n  • " + "M7 " * 200
    assert compact(text, 300) != ""
