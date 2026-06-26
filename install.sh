#!/bin/bash
# One-command bootstrap for the trading brain on a fresh VPS:
#   git clone https://github.com/hrubee/trader.git && cd trader && bash install.sh
# Sets up the venv + deps, materializes per-machine config (paths resolved to THIS clone), seeds the
# playbook, creates the .env you fill with keys, and installs the cron schedule. Idempotent + safe to re-run.
set -eu
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"
echo "[install] repo = $REPO"

# 1) python venv + deps (prefer uv, fall back to python venv)
if [ ! -x "$REPO/.venv/bin/python3" ]; then
  if command -v uv >/dev/null 2>&1; then uv venv "$REPO/.venv"; else python3 -m venv "$REPO/.venv"; fi
fi
"$REPO/.venv/bin/python3" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
"$REPO/.venv/bin/python3" -m pip install -q -r "$REPO/requirements.txt"
echo "[install] python deps installed"

# 2) per-machine data dirs
mkdir -p "$REPO/loop_trader_data" "$REPO/loop_trader_data_live" "$REPO/loop_trader_data_brain"
BRAIN="$REPO/loop_trader_data_brain"

# 3) materialize config from templates (substitute {{REPO}} -> this clone's path)
sed "s#{{REPO}}#$REPO#g" "$REPO/brain/vps_brain_prompt.txt" > "$BRAIN/vps_brain_prompt.txt"
[ -f "$BRAIN/brain_accounts.json" ] || sed "s#{{REPO}}#$REPO#g" "$REPO/brain/brain_accounts.example.json" > "$BRAIN/brain_accounts.json"
[ -f "$BRAIN/PLAYBOOK.md" ] || cp "$REPO/brain/PLAYBOOK.seed.md" "$BRAIN/PLAYBOOK.md"
echo "[install] config materialized (prompt, accounts, playbook)"

# 4) .env from template (NEVER overwrite an existing one)
[ -f "$REPO/.env" ] || { cp "$REPO/.env.example" "$REPO/.env"; echo "[install] created .env — EDIT IT WITH YOUR KEYS before running live"; }

chmod +x "$REPO/vps_brain_run.sh"

# 5) cron (every 20 min, offset :5,:25,:45). Re-runnable: only adds if absent.
CRON_LINE="5,25,45 * * * * $REPO/vps_brain_run.sh"
if crontab -l 2>/dev/null | grep -qF "$REPO/vps_brain_run.sh"; then
  echo "[install] cron already present"
else
  ( crontab -l 2>/dev/null; echo "$CRON_LINE" ) | crontab -
  echo "[install] cron installed: $CRON_LINE"
fi

cat <<EOF

[install] DONE. Next:
  1. Edit  $REPO/.env  with your Binance keys (demo and/or live).
  2. Review $BRAIN/brain_accounts.json — set live "enabled" + risk_pct/min_grade per account.
  3. Smoke test one iteration:   bash $REPO/vps_brain_run.sh ; tail -40 $BRAIN/cron.log
  4. The cron runs it every 20 min. Use a different AGENT with:  BRAIN_CLI=hermes  (and BRAIN_MODEL=...).
SAFETY: live trading requires BINANCE_LIVE_* keys AND each live account's "live":true in brain_accounts.json.
EOF
