"""Assemble all Osiris feeds into one prose 'world brief' for the oracle."""
from __future__ import annotations

from collections import defaultdict

from .models import WorldBrief, WorldEvent
from .sanitize import clean


def build_brief(events: list[WorldEvent]) -> WorldBrief:
    by_cat: dict[str, list[WorldEvent]] = defaultdict(list)
    for e in events:
        by_cat[e.category].append(e)

    domains = {c: len(v) for c, v in by_cat.items()}
    lines: list[str] = []
    top: list[str] = []

    # most-active domains first
    for cat in sorted(by_cat, key=lambda c: -len(by_cat[c])):
        evs = sorted(by_cat[cat], key=lambda e: -e.salience)[:8]
        lines.append(f"\n[{cat.upper()}] ({len(by_cat[cat])} signals)")
        for e in evs:
            loc = f"  @{e.lat:.1f},{e.lng:.1f}" if e.lat is not None else ""
            title = clean(e.title, 240)
            summary = clean(e.summary, 140)
            extra = f" — {summary}" if summary else ""
            lines.append(f"  • {title}{extra}{loc}")
            top.append(title)

    text = "\n".join(lines).strip()
    return WorldBrief(
        event_count=len(events),
        domains=domains,
        text=text[:6500],
        top_events=top[:24],
    )
