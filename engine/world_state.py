"""Assemble all Osiris feeds into one prose 'world brief' for the oracle.

The brief has a character budget (the model's context is finite). v0.2 listed domains
by *how many* events they had and then cut the text at 6,500 characters — mid-line —
so a quiet but critical domain (one M7 quake, one outbreak) fell off the end while
routine weather warnings filled the page. Now every domain gets its most salient event
first, then its second, and so on (round-robin by salience) until the budget is spent,
and cuts only ever happen at line boundaries. `compact()` re-applies the same rule to a
finished brief when a prompt needs a smaller one.
"""
from __future__ import annotations

from collections import defaultdict

from .models import WorldBrief, WorldEvent
from .sanitize import clean

BUDGET = 6500
PER_DOMAIN = 8


def _line(e: WorldEvent) -> str:
    loc = f"  @{e.lat:.1f},{e.lng:.1f}" if e.lat is not None and e.lng is not None else ""
    title = clean(e.title, 240)
    summary = clean(e.summary, 140)
    extra = f" — {summary}" if summary else ""
    return f"  • {title}{extra}{loc}"


def _select(domains: list[tuple[str, str, list[str]]], budget: int) -> dict[str, list[int]]:
    """Round-robin pick: (name, header, lines best-first) → {name: [line indexes kept]}."""
    kept: dict[str, list[int]] = {}
    used = 0
    depth = max((len(lines) for _, _, lines in domains), default=0)
    for r in range(depth):
        for name, header, lines in domains:
            if r >= len(lines):
                continue
            cost = len(lines[r]) + 1 + (len(header) + 2 if name not in kept else 0)
            if used + cost > budget:
                continue          # a shorter line from another domain may still fit
            kept.setdefault(name, []).append(r)
            used += cost
    return kept


def _render(domains: list[tuple[str, str, list[str]]], kept: dict[str, list[int]]) -> str:
    out: list[str] = []
    for name, header, lines in domains:
        if name in kept:
            out.append("")
            out.append(header)
            out.extend(lines[i] for i in kept[name])
    return "\n".join(out).strip()


def build_brief(events: list[WorldEvent], budget: int = BUDGET) -> WorldBrief:
    by_cat: dict[str, list[WorldEvent]] = defaultdict(list)
    for e in events:
        by_cat[e.category].append(e)
    domains_count = {c: len(v) for c, v in by_cat.items()}

    ranked = {c: sorted(v, key=lambda e: -e.salience)[:PER_DOMAIN] for c, v in by_cat.items()}
    # domains ordered by their single most salient event, then by activity
    order = sorted(ranked, key=lambda c: (-ranked[c][0].salience, -len(by_cat[c]), c))
    domains = [(c, f"[{c.upper()}] ({len(by_cat[c])} signals)", [_line(e) for e in ranked[c]])
               for c in order]
    kept = _select(domains, budget)

    # evidence archive for the judge: the most salient picks, interleaved across domains
    top: list[str] = []
    depth = max((len(v) for v in kept.values()), default=0)
    for r in range(depth):
        for c in order:
            if c in kept and r < len(kept[c]):
                top.append(clean(ranked[c][kept[c][r]].title, 240))

    return WorldBrief(
        event_count=len(events),
        domains=domains_count,
        text=_render(domains, kept),
        top_events=top[:24],
    )


def compact(text: str, budget: int) -> str:
    """Shrink a rendered brief to `budget` characters with the same round-robin rule,
    so every domain keeps its lead signal and no line is cut in half."""
    if len(text) <= budget:
        return text
    domains: list[tuple[str, str, list[str]]] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if raw.startswith("[") and not raw.startswith("  "):
            domains.append((raw, raw, []))
        elif domains:
            domains[-1][2].append(raw)
        else:                                # prose before any header — keep as a domain
            domains.append(("", "", [raw]))
    if not domains:
        return text[:budget].rsplit("\n", 1)[0]
    return _render(domains, _select(domains, budget))
