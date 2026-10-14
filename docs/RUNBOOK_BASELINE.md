# PYTHIA baseline runbook

## 1. Preparation
Install Python 3.11 or newer, uv, Ollama, and the prerequisites listed in the upstream README and integrations/osiris/INSTALL.md. Use a test machine; do not expose the local API publicly before checking authentication and network boundaries.

## 2. Offline baseline
Run `python scripts/check_baseline.py`. PASS means Python files parse and expected repository paths exist. It does not prove the services run.

## 3. Runtime setup
Copy `.env.example` to `.env` and configure local values. Do not commit `.env`, credentials or personal data. Review `run-all.sh` before running it. Start Ollama and obtain the model specified in the upstream README. Follow the upstream launcher instructions.

## 4. End-to-end checks
- Confirm Osiris renders live feeds with current timestamps.
- Confirm a local model returns a forecast for each of the four horizons.
- Inspect all four council personas and record consensus and dissent.
- Check forecast ledger persistence and scorecard using an independently resolved outcome.
- Check REST, MCP, webhook, what-if, alerts and morning brief.

## 5. Evidence and failures
For each step record date, Git revision, command, result, duration and redacted log. Mark external dependencies unavailable as NOT TESTED. On failure, capture the failing component and minimal reproduction before changing original logic. Do not claim production readiness from the offline baseline.
