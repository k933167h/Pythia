#!/usr/bin/env bash
# PYTHIA oracle — one machine:  Osiris UI (:3000)  +  PYTHIA engine (:8088)
# The oracle uses your local LLM (Ollama, :11434) as its brain. Ctrl-C stops all.
#
# Exposure (v0.3.1):
#   default        UI on 127.0.0.1 only; dashboard proxy in "operator" mode (full controls).
#   PYTHIA_LAN=1   UI on 0.0.0.0 (LAN / Tailscale / phone); proxy in "read" mode unless you
#                  set PYTHIA_PROXY_MODE yourself — anyone who can open the page could
#                  otherwise switch models, toggle the loop or edit alert rules.
set -euo pipefail

PYTHIA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OSIRIS_DIR="${OSIRIS_DIR:-$HOME/osiris}"
RUN_DIR="$PYTHIA_DIR/runs"
mkdir -p "$RUN_DIR"

if [ "${PYTHIA_LAN:-0}" = "1" ]; then
  UI_HOST="0.0.0.0"; export PYTHIA_PROXY_MODE="${PYTHIA_PROXY_MODE:-read}"
else
  UI_HOST="127.0.0.1"; export PYTHIA_PROXY_MODE="${PYTHIA_PROXY_MODE:-operator}"
fi

# Tailscale URL (for your phone), if available.
TS_BIN="$(command -v tailscale || true)"
[ -z "$TS_BIN" ] && [ -x "/Applications/Tailscale.app/Contents/MacOS/Tailscale" ] && TS_BIN="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
TS_IP=""; [ -n "$TS_BIN" ] && TS_IP="$("$TS_BIN" ip -4 2>/dev/null | head -n1 || true)"

pids=()
cleanup() {
  echo; echo "[pythia] shutting down..."
  for p in ${pids[@]+"${pids[@]}"}; do kill "$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
  rm -f "$RUN_DIR/engine.pid" "$RUN_DIR/ui.pid"
}
trap cleanup INT TERM EXIT

# Stop only the servers a previous run of THIS script started (recorded PIDs) — never
# whatever else happens to listen on :3000/:8088 or match a pattern.
stop_previous() {
  local f="$1" pid
  [ -f "$f" ] || return 0
  pid="$(cat "$f" 2>/dev/null || true)"
  # a stale PID file (crash, reboot) may now point at an unrelated process: only stop it
  # if it still looks like ours
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null \
     && ps -p "$pid" -o command= 2>/dev/null | grep -Eq 'engine\.run|uv run|next|npm'; then
    echo "[pythia] stopping previous $(basename "$f" .pid) (pid $pid)"
    kill "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  fi
  rm -f "$f"
}
stop_previous "$RUN_DIR/engine.pid"
stop_previous "$RUN_DIR/ui.pid"

port_busy() { (command -v lsof >/dev/null && lsof -ti "tcp:$1" -sTCP:LISTEN >/dev/null 2>&1) || return 1; }
for p in 3000 8088; do
  if port_busy "$p"; then
    echo "[pythia] ✗ port $p is in use by another program — stop it (lsof -i :$p) and retry."; exit 1
  fi
done

# the oracle needs a local LLM (Ollama). Warn if it's not up.
if ! curl -s -o /dev/null --max-time 3 http://localhost:11434/v1/models; then
  echo "[pythia] ⚠  Ollama not reachable on :11434 — start it (open the Ollama app, or run 'ollama serve')."
fi

echo "[pythia] PYTHIA engine  -> http://127.0.0.1:8088"
( cd "$PYTHIA_DIR" && PYTHIA_DEV_ORIGINS="$TS_IP" exec uv run python -m engine.run ) &
pids+=($!); echo "$!" > "$RUN_DIR/engine.pid"

echo "[pythia] Osiris/PYTHIA UI -> binding $UI_HOST:3000 (proxy mode: $PYTHIA_PROXY_MODE)"
( cd "$OSIRIS_DIR" && PYTHIA_ENGINE_URL="http://127.0.0.1:8088" PYTHIA_DEV_ORIGINS="$TS_IP" \
    exec npm run dev -- -H "$UI_HOST" -p 3000 ) &
pids+=($!); echo "$!" > "$RUN_DIR/ui.pid"

opener="$(command -v open || command -v xdg-open || true)"
# open the dashboard once it's serving
( for _ in $(seq 1 90); do curl -s -o /dev/null "http://127.0.0.1:3000" && { [ -n "$opener" ] && "$opener" "http://localhost:3000"; break; }; sleep 2; done ) &

echo
echo "════════════════════════════════════════════════════════"
echo "  PYTHIA oracle — open the dashboard:"
echo "    • this computer : http://localhost:3000"
if [ "$UI_HOST" = "0.0.0.0" ] && [ -n "$TS_IP" ]; then
  echo "    • phone         : http://$TS_IP:3000  (Tailscale, read-only unless PYTHIA_PROXY_MODE is set)"
elif [ -n "$TS_IP" ]; then
  echo "    • phone         : run with PYTHIA_LAN=1 to reach it over Tailscale"
fi
echo "════════════════════════════════════════════════════════"
wait
