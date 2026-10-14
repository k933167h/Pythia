# PYTHIA Architecture Overview

## Objective
Preserve the original global intelligence and probabilistic forecasting product. No enterprise-specific functionality is introduced in this baseline.

## Logical components
1. Osiris: live global feeds and 3D visualization.
2. World-state and intake: normalize incoming events for the engine.
3. Oracle: local model-driven forecasts for 24 hours, one week, one month, and one year.
4. Swarm: strategist, economist, naturalist and skeptic deliberation.
5. Ledger and scoring: record forecasts and evaluate resolved outcomes.
6. Interfaces: REST API, MCP, webhook, alerts and what-if scenarios.

## Boundaries
- Local inference depends on installed and running Ollama and a compatible model.
- Live data depends on reachable upstream sources; offline source parsing does not establish feed readiness.
- Osiris dashboard includes a separate frontend/runtime dependency surface.
- External MiroFish full swarm integration may require additional configuration; do not equate the built-in local council with full MiroFish simulation.

## Baseline checks
Run `python scripts/check_baseline.py` with Python 3.11+. This checks Python parsing and repository structure only. See `docs/BASELINE_VALIDATION.md` for end-to-end acceptance gates.

## Change policy
Keep upstream attribution and MIT license. Modify code only on feature branches, document behavior changes, verify against real integrations, and review PR before merging.
