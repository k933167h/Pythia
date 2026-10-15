"""World brief budgeting: every domain keeps its lead signal; no mid-line cuts."""
from engine.fsutil import atomic_write_json
from engine.models import WorldEvent
from engine.world_state import build_brief, compact


def _events():
    evs = []
    # 16 noisy domains with many long, low-salience items
    for d in range(16):
        for i in range(40):
            evs.append(WorldEvent(title=f"routine item {i} in noisy domain {d} " + "x" * 120,
                                  category=f"noisy{d:02d}", salience=0.4 + i / 1000))
    # 4 quiet but critical domains with one event each
    for cat, title in [("seismic", "M7.4 — off Hokkaido"), ("health", "Ebola outbreak confirmed in Goma"),
                       ("market-odds", "Kalshi 81%: Fed cuts in November"), ("cyber", "Ransomware hits national grid")]:
        evs.append(WorldEvent(title=title, category=cat, salience=0.95))
    return evs


def test_critical_quiet_domains_survive_budget():
    b = build_brief(_events())
    assert len(b.text) <= 6500
    for t in ("M7.4", "Ebola", "Kalshi 81%", "Ransomware"):
        assert t in b.text
    # critical domains come first and lead the judge's evidence archive
    assert b.text.startswith("[")
    assert set(b.top_events[:4]) == {"M7.4 — off Hokkaido", "Ebola outbreak confirmed in Goma",
                                     "Kalshi 81%: Fed cuts in November", "Ransomware hits national grid"}
    assert sum(1 for c in b.domains) == 20


def test_no_line_is_cut():
    b = build_brief(_events())
    for line in b.text.splitlines():
        assert line == "" or line.startswith("[") or line.startswith("  • ")
        if line.startswith("  • routine"):
            assert line.endswith("x" * 120)


def test_compact_keeps_every_domain_lead():
    b = build_brief(_events())
    small = compact(b.text, 2600)
    assert len(small) <= 2600
    for t in ("M7.4", "Ebola", "Kalshi 81%", "Ransomware"):
        assert t in small
    assert all(line == "" or line.startswith(("[", "  • ")) for line in small.splitlines())
    assert compact("short", 100) == "short"


def test_atomic_write_never_leaves_partial(tmp_path):
    p = tmp_path / "rules.json"
    atomic_write_json(p, [{"id": 1}])

    class Boom:
        pass
    try:
        atomic_write_json(p, [Boom()])          # serialisation fails mid-way
    except TypeError:
        pass
    import json
    assert json.loads(p.read_text()) == [{"id": 1}]           # old content intact
    assert [x.name for x in tmp_path.iterdir()] == ["rules.json"]   # no temp litter
    atomic_write_json(p, {"k": "v"}, mode=0o600)
    assert (p.stat().st_mode & 0o777) == 0o600
