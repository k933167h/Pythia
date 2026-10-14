# PYTHIA baseline validation

This fork preserves the original world-intelligence forecasting project. Baseline verification must not modify prediction behavior.

## Inventory
- Python engine: engine/
- Osiris integration: integrations/osiris/
- Startup: run-all.sh
- Python project: pyproject.toml and uv.lock
- Initial main commit: ab81a7dfb0840de3bea97e5f6032702db0b1aa45

## Validation gates
1. Compile all Python engine modules.
2. Parse pyproject.toml and validate dependency installation.
3. Run shell syntax checks for run-all.sh.
4. Start Ollama and verify forecasts for all four horizons.
5. Verify Osiris live feeds, timestamps, and dashboard.
6. Verify four-agent council, dissent, and forecast ledger.
7. Verify scorecard against independently resolved events.
8. Verify REST, MCP, webhook, alerts, and what-if flows.
9. Record security and dependency audits for Python and Osiris.

Record command, revision, timestamp, result and redacted evidence for every check. A skipped or unavailable integration is NOT TESTED, not PASSED. Do not merge before CI and review.
