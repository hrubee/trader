#!/bin/bash
# UNIFIED TRADING BRAIN — one headless-agent iteration, two phases:
#   PHASE 1 (AI): analyze the master account + market ONCE → write decisions.json (coin/side/SL/TP/grade).
#   PHASE 2 (deterministic Python): execute-decisions mirrors that JSON across ALL accounts in
#           brain_accounts.json — programmatic risk-sizing per account, grade-gated, full bracket, isolated ledgers.
# PORTABLE: everything derives from this script's location ($REPO) — no hardcoded host paths.
# AGENT-AGNOSTIC: the AI CLI + model are env-configurable (BRAIN_CLI / BRAIN_MODEL), so it runs under
#                 `claude`, `hermes`, or any compatible `-p "<prompt>"` agent.
set -u
# cron-safety: cron's minimal PATH (/usr/bin:/bin) misses /usr/local/bin where claude/hermes usually live.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"
export HOME="${HOME:-/root}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO" || exit 1

BRAIN_CLI="${BRAIN_CLI:-claude}"                       # agent CLI; override e.g. BRAIN_CLI=hermes
case "$(basename "$BRAIN_CLI")" in                     # model default is per-CLI:
  hermes*) BRAIN_MODEL="${BRAIN_MODEL-}" ;;            #   hermes -> empty = use its own configured model (e.g. nemotron via NIM)
  *)       BRAIN_MODEL="${BRAIN_MODEL:-claude-sonnet-4-6}" ;;
esac
BRAIN="$REPO/loop_trader_data_brain"
mkdir -p "$BRAIN"
LOG="$BRAIN/cron.log"
PROMPT_FILE="$BRAIN/vps_brain_prompt.txt"
DEC="$BRAIN/decisions.json"
ACCTS="$BRAIN/brain_accounts.json"
PY="$REPO/.venv/bin/python3"; [ -x "$PY" ] || PY="python3"

run_agent() {                                          # PHASE 1: emit decisions.json, print summary to stdout
  local prompt; prompt="$(cat "$PROMPT_FILE")"
  case "$(basename "$BRAIN_CLI")" in
    hermes*)   # Hermes: one-shot (-z), model (-m), --yolo bypasses approval for headless tool-use.
      timeout 900 "$BRAIN_CLI" --yolo ${BRAIN_MODEL:+-m "$BRAIN_MODEL"} ${BRAIN_TOOLSETS:+-t "$BRAIN_TOOLSETS"} -z "$prompt" 2>&1 ;;
    *)         # Claude Code (default): -p prompt, --model when supported.
      if [ -n "$BRAIN_MODEL" ] && "$BRAIN_CLI" --help 2>&1 | grep -q -- "--model"; then
        timeout 900 "$BRAIN_CLI" --allowed-tools "Bash Read Write Edit Grep Glob" --model "$BRAIN_MODEL" -p "$prompt" < /dev/null 2>&1
      else
        timeout 900 "$BRAIN_CLI" --allowed-tools "Bash Read Write Edit Grep Glob" -p "$prompt" < /dev/null 2>&1
      fi ;;
  esac
}

exec 9>/tmp/vps_brain.lock
flock -n 9 || { echo "$(date -u +%FT%TZ) SKIP — prior brain iteration still running" >> "$LOG"; exit 0; }

echo "==== $(date -u +%FT%TZ) BRAIN iteration START ($BRAIN_CLI / $BRAIN_MODEL) ====" >> "$LOG"
rm -f "$DEC"                                           # never execute a stale decision set

# PHASE 0 (deterministic, NO AI): PREPARE ALL DECISION INPUTS for the brain, then feed them in.
# The AI does NOT fetch anything itself — it only reads these prepared files and judges.
ACCT_CTX="$BRAIN/account.json"
SPIKES="$BRAIN/spikes.json"; SPIKES_PREV="$BRAIN/spikes_prev.json"

# (a) ACCOUNT/WALLET + LIVE EXCHANGE POSITIONS (ground truth, master/demo account).
if ac_out="$(LOOP_TRADER_DATADIR="$REPO/loop_trader_data" timeout 120 "$PY" "$REPO/loop_trader.py" state 2>>"$LOG")" && [ -n "$ac_out" ]; then
  printf '%s\n' "$ac_out" > "$ACCT_CTX"
  echo "$(date -u +%FT%TZ) account prepared ($(printf '%s' "$ac_out" | grep -o '\"n_positions\":[0-9]*' | head -1); $(printf '%s' "$ac_out" | grep -o '\"total\":[0-9.]*' | head -1))" >> "$LOG"
else
  echo "$(date -u +%FT%TZ) account fetch FAILED — writing error context" >> "$LOG"
  echo '{"error":"account fetch failed","wallet":null,"positions":[],"n_positions":0}' > "$ACCT_CTX"
fi

# (b) VOLUME-SPIKE CANDIDATE LIST (prior run's list kept for cross-iteration confirmation).
[ -f "$SPIKES" ] && cp -f "$SPIKES" "$SPIKES_PREV"
if vs_out="$(LOOP_TRADER_DATADIR="$REPO/loop_trader_data" timeout 180 "$PY" "$REPO/loop_trader.py" volspike --spike-tf 15m --top 20 --min-spike 5 --confirm-frac 0.9 --min-vol 5e6 --max-scan 400 2>>"$LOG")" && [ -n "$vs_out" ]; then
  printf '%s\n' "$vs_out" > "$SPIKES"
  echo "$(date -u +%FT%TZ) volspike list computed ($(printf '%s' "$vs_out" | grep -o '\"n\":[0-9]*' | head -1))" >> "$LOG"
else
  echo "$(date -u +%FT%TZ) volspike scan FAILED — empty candidate list this run" >> "$LOG"
  echo '{"coins":[],"n":0,"error":"scan failed"}' > "$SPIKES"
fi

rc=0
for attempt in 1 2; do
  out="$(run_agent)"; rc=$?
  printf '%s\n' "$out" >> "$LOG"
  if [ "$attempt" -eq 1 ] && printf '%s' "$out" | grep -qiE 'API Error: (429|5[0-9][0-9])'; then
    echo "==== $(date -u +%FT%TZ) transient API error — retrying once in 45s ====" >> "$LOG"; sleep 45; continue
  fi
  break
done

if [ ! -s "$DEC" ]; then                               # SALVAGE: AI printed the JSON but forgot to write the file
  if printf '%s' "$out" | "$PY" "$REPO/salvage_decisions.py" "$DEC" >> "$LOG" 2>&1 && [ -s "$DEC" ]; then
    echo "$(date -u +%FT%TZ) salvaged decisions.json from agent stdout (AI printed instead of writing)" >> "$LOG"
  fi
fi

if [ -s "$DEC" ]; then                                 # PHASE 2: deterministic, no AI
  echo "---- $(date -u +%FT%TZ) executing decisions across accounts ----" >> "$LOG"
  "$PY" "$REPO/loop_trader.py" execute-decisions --decisions "$DEC" --accounts "$ACCTS" >> "$LOG" 2>&1
else
  echo "---- $(date -u +%FT%TZ) no decisions.json emitted — nothing to execute ----" >> "$LOG"
fi
# Telegram trade alerts: entry chart + threaded exit reply (reads demo ledger, new events only)
TG_ALERT_DATADIR="$REPO/loop_trader_data" "$PY" "$REPO/tg_alerter.py" >> "$LOG" 2>&1 || true
echo "==== $(date -u +%FT%TZ) BRAIN iteration END (exit $rc) ====" >> "$LOG"
