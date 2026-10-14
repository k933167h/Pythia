"""Optional Korean public-data feeds (공공데이터포털, data.go.kr) — EXPERIMENTAL.

PYTHIA's global feeds are keyless; Korean official feeds are not. Each one below is
enabled only when DATA_GO_KR_KEY (the portal's 일반 인증키, URL-decoded form) is set,
after applying for the specific API (활용신청) on data.go.kr.

* 기상청 지진정보 조회서비스 (EqkInfoService/getEqkMsg) → seismic events with magnitude,
  so the deterministic resolver can settle Korean-peninsula quake forecasts.
* 한국환경공단 에어코리아 대기오염정보 (ArpltnInforInqireSvc/getCtprvnRltmMesureDnsty)
  → air-quality signals for metropolitan areas.

Endpoints and field names follow the providers' published guides; verify them against
your approved API's 상세기능 page before production use — the parsers are defensive
and drop anything they do not recognise.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .config import CONFIG, HTTPX_VERIFY
from .models import WorldEvent

log = logging.getLogger("pythia.korea")

EQK_URL = "https://apis.data.go.kr/1360000/EqkInfoService/getEqkMsg"
AIR_URL = "https://apis.data.go.kr/B552584/ArpltnInforInqireSvc/getCtprvnRltmMesureDnsty"
AIR_SIDO = ["서울", "부산", "인천", "대구", "광주", "대전", "울산", "경기", "세종"]

# Korean peninsula + surrounding seas (for KOREA_FOCUS salience boost)
KOREA_BBOX = (32.5, 124.0, 39.5, 132.0)     # lat_min, lng_min, lat_max, lng_max


def in_korea(lat, lng) -> bool:
    if lat is None or lng is None:
        return False
    a, b, c, d = KOREA_BBOX
    return a <= lat <= c and b <= lng <= d


def _items(data: Any) -> list[dict]:
    """data.go.kr JSON: response.body.items(.item) — a list, a dict, or ''."""
    try:
        body = data["response"]["body"]
    except (KeyError, TypeError):
        return []
    items = body.get("items") if isinstance(body, dict) else None
    if isinstance(items, dict):
        items = items.get("item", [])
    if isinstance(items, dict):
        items = [items]
    return [i for i in (items or []) if isinstance(i, dict)]


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_earthquakes(data: Any) -> list[WorldEvent]:
    out = []
    for it in _items(data):
        mag = _f(it.get("mt"))
        lat, lng = _f(it.get("lat")), _f(it.get("lon"))
        loc = str(it.get("loc") or "").strip()
        if mag is None or not loc:
            continue
        when = str(it.get("tmEqk") or it.get("tmFc") or "")
        out.append(WorldEvent(
            title=f"M{mag} — {loc} (기상청)"[:240],
            summary=f"기상청 지진정보 · 발생 {when} · 진원깊이 {it.get('dep', '?')}km. {it.get('rem', '')}"[:2000],
            category="seismic", source="kma", lat=lat, lng=lng,
            salience=round(min(1.0, 0.45 + max(0.0, mag - 2.0) * 0.12), 3),
            raw={"magnitude": mag, "tmEqk": when, "loc": loc},
        ))
    return out


_GRADE = {"1": "좋음", "2": "보통", "3": "나쁨", "4": "매우나쁨"}


def parse_air(data: Any, sido: str) -> list[WorldEvent]:
    vals = []
    for it in _items(data):
        pm25 = _f(it.get("pm25Value"))
        if pm25 is not None:
            vals.append((pm25, it))
    if not vals:
        return []
    vals.sort(key=lambda x: x[0], reverse=True)
    worst, it = vals[0]
    avg = sum(v for v, _ in vals) / len(vals)
    grade = _GRADE.get(str(it.get("pm25Grade") or ""), "")
    return [WorldEvent(
        title=f"{sido} 초미세먼지 최고 {int(worst)}㎍/㎥ ({it.get('stationName', '')}) {grade}".strip()[:240],
        summary=f"에어코리아 실시간 · {len(vals)}개 측정소 평균 PM2.5 {avg:.0f}㎍/㎥ · {it.get('dataTime', '')}"[:2000],
        category="air-quality", source="airkorea", lat=None, lng=None,
        # WHO 24h guideline 15 µg/m³; Korean 'bad' starts at 36
        salience=round(min(0.9, 0.3 + worst / 150), 3),
        raw={"pm25_max": worst, "pm25_avg": round(avg, 1)},
    )]


async def fetch() -> list[WorldEvent]:
    key = CONFIG.airkorea_key
    if not key:
        return []
    out: list[WorldEvent] = []
    today = time.strftime("%Y%m%d")
    week_ago = time.strftime("%Y%m%d", time.localtime(time.time() - 7 * 86400))
    async with httpx.AsyncClient(verify=HTTPX_VERIFY, timeout=10) as c:
        try:
            r = await c.get(EQK_URL, params={"serviceKey": key, "dataType": "JSON", "numOfRows": 20,
                                             "pageNo": 1, "fromTmFc": week_ago, "toTmFc": today})
            if r.status_code < 400:
                out.extend(parse_earthquakes(r.json()))
        except (httpx.HTTPError, ValueError) as e:
            log.debug("KMA earthquake feed failed: %s", e)
        for sido in AIR_SIDO:
            try:
                r = await c.get(AIR_URL, params={"serviceKey": key, "returnType": "json", "numOfRows": 100,
                                                 "pageNo": 1, "sidoName": sido, "ver": "1.0"})
                if r.status_code < 400:
                    out.extend(parse_air(r.json(), sido))
            except (httpx.HTTPError, ValueError) as e:
                log.debug("AirKorea %s failed: %s", sido, e)
    return out
