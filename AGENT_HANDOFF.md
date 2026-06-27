# AGENT HANDOFF — Autonomous Crypto Trading Brain (`/root/trader`)

> You are the next agent taking over this system on the VPS. Read this whole file before changing
> anything. **This system trades REAL MONEY on two live accounts.** Be careful, verify against exchange
> ground truth, and never leave a position naked.

---

## 0. TL;DR — what this is

A **decision/execution-split** crypto trading brain:

1. **PHASE 0 (deterministic):** a wrapper prepares all decision inputs (account state + a volume-spike
   candidate list) and writes them to files.
2. **PHASE 1 (AI):** a headless LLM agent (Claude Code) reads those files, judges which spikes to
   trade and which open positions to close, and writes `decisions.json`.
3. **PHASE 2 (deterministic):** a Python executor mirrors that one decision set across **multiple
   accounts** (Binance demo, Binance live, Delta live), each **risk-sized and grade-gated per account**,
   every order **fully bracketed** (static stop + TP; Binance also trailing).

A cron runs the whole loop every 15 minutes. The AI never places orders directly — it only writes JSON.

---

## 1. Host / locations

- **VPS:** `ssh root@187.127.132.39` (default key). Runs as **root**.
- **Repo on VPS:** `/root/trader` — this IS a git checkout of `github.com/hrubee/trader` (origin/main).
- **GitHub:** `https://github.com/hrubee/trader` (auth: `gh` as `hrubee`).
- **Python:** `/root/trader/.venv/bin/python3` (`.venv` is a symlink). ccxt 4.5.x.
- **Secrets:** `/root/trader/.env` (gitignored — NEVER commit). Holds Binance + Delta keys.

### Git topology (important)
All three — local dev checkout, GitHub `origin/main`, and the VPS `/root/trader` — are kept at the
**same commit**. The VPS once diverged (local-only commits that were never pushed); it's now aligned.
To reconcile after editing files directly on the VPS:
```bash
# 1) bring the VPS's canonical file(s) into a dev checkout, commit, push to origin
# 2) on the VPS: realign HEAD without touching the working tree:
cd /root/trader && git fetch origin -q && git reset --mixed origin/main
git checkout origin/main -- .gitignore   # if .gitignore changed
```
`git reset --mixed` moves HEAD + index to origin but **does NOT touch working files** (safe on a live
box — no deletions, no reverts of running code). Runtime dirs (`loop_trader_data*`) and `.env` are
gitignored so they never show as changes.

---

## 2. The files that matter

| File | Role |
|---|---|
| `loop_trader.py` | **The core CLI.** All commands: `state`, `enter`, `close`, `volspike`, `price`, `journal`, `review`, `execute-decisions`, `cancel-stops`, `retrail`. Multi-exchange. |
| `vps_brain_run.sh` | **The cron wrapper.** Phase 0 (prep) → Phase 1 (agent) → salvage → Phase 2 (execute). |
| `salvage_decisions.py` | Recovers `decisions.json` from agent stdout if the AI printed it instead of writing the file. |
| `brain/vps_brain_prompt.txt` | **Prompt template** (`{{REPO}}` placeholder). Tracked in git. |
| `brain/PLAYBOOK.seed.md` | Seed for a fresh playbook. |
| `brain/brain_accounts.example.json` | Sanitized accounts template. |
| `platforms/binance/adapter.py` | Binance adapter (used by `account_client` for binance). |
| `loop_trader_data_brain/` | **RUNTIME (gitignored).** Materialized prompt, `decisions.json`, `spikes.json`, `account.json`, `cron.log`, `PLAYBOOK.md`, **`brain_accounts.json`**. |
| `loop_trader_data/` | Binance **demo** ledger (open_trades.json, journal, state). |
| `loop_trader_data_live/` | Binance **live** ledger. |
| `loop_trader_data_delta/` | Delta **live** ledger. |

> RUNTIME ≠ TEMPLATE. The repo holds `brain/vps_brain_prompt.txt` (with `{{REPO}}`). The **running**
> prompt is `loop_trader_data_brain/vps_brain_prompt.txt` (with `{{REPO}}`→`/root/trader` substituted).
> If you change the prompt: edit the template, commit it, then re-materialize:
> `sed "s#{{REPO}}#/root/trader#g" brain/vps_brain_prompt.txt > loop_trader_data_brain/vps_brain_prompt.txt`

---

## 3. The cron loop (`vps_brain_run.sh`)

Cron: `*/15 * * * * BRAIN_CLI=claude /root/trader/vps_brain_run.sh`

Per run:
1. `flock /tmp/vps_brain.lock` — anti-overlap (skips if a prior run is still going).
2. `rm decisions.json` — never execute a stale decision.
3. **Phase 0:** writes `account.json` (binance demo `state`) and `spikes.json` (volspike), rotating the
   prior list to `spikes_prev.json` for cross-iteration confirmation.
4. **Phase 1:** runs the agent on the prompt; one retry on transient API error.
5. **Salvage:** if `decisions.json` wasn't written but the agent printed valid JSON, recover it.
6. **Phase 2:** `execute-decisions --decisions ... --accounts ...` mirrors across accounts.
7. Logs everything to `loop_trader_data_brain/cron.log`.

### The engine (Claude Code, headless, as root)
`run_agent()` calls: `claude --allowed-tools "Bash Read Write Edit Grep Glob" --model claude-sonnet-4-6 -p "$prompt" < /dev/null`
- `--allowed-tools` pre-approves the tools so it runs **headless with no permission prompts**. This is
  the clean approach — do NOT use `--dangerously-skip-permissions` (Claude refuses it as root; an
  `IS_SANDBOX=1` hack exists but the allowlist is better).
- `BRAIN_CLI` is swappable (`claude` / `hermes`). This system was migrated **from hermes (nemotron via
  NVIDIA NIM) to Claude Code**. The hermes branch still exists in the wrapper.
- `BRAIN_MODEL` defaults to `claude-sonnet-4-6`; override env to use Opus etc.

---

## 4. Accounts & exchanges — ⚠️ REAL MONEY

`loop_trader_data_brain/brain_accounts.json` (runtime, gitignored):

| name | exchange | venue | live | risk_pct | leverage | min_grade | enabled |
|---|---|---|---|---|---|---|---|
| `demo` (master) | binance | testnet | false | 1.0% | 20x | A | true |
| `live` | binance | **LIVE $** | true | 1.0% | 5x | A | true |
| `delta-live` | delta | **Delta India LIVE $** | true | 0.5% | 5x | A | true |

- The **master** (`demo`) is what the AI inspects. Decisions mirror to ALL enabled accounts.
- `min_grade: A` means only the AI's **A-grade** (highest-conviction) decisions reach the live accounts.
  `B` grade (if any account allowed it) is demo/experimental.
- **`live.enabled` is operator-controlled.** The prompt forbids the AI from flipping it. Don't enable
  more real-money exposure without the operator's explicit say-so.
- Sizing is per-account: `risk_pct % of that account's wallet = the loss if the stop hits`.

### Binance
- Adapter: `platforms/binance/adapter.py`. Keys: `BINANCE_DEMO_*` (testnet) / `BINANCE_LIVE_*`.
- Symbols: `COIN/USDT:USDT`. Wallet currency: USDT.
- Live requires env `LOOP_TRADER_ALLOW_LIVE=1` (the executor sets it for live accounts).
- Quirk: testnet trailing stops are frozen/cosmetic (real on live). Two max-qty filters — `LOT_SIZE`
  AND the smaller `MARKET_LOT_SIZE` (market orders); `enter` caps to the min of both (`-4005` fix).

### Delta Exchange (India) — integrated this session
- Routed via ccxt `delta` with **India endpoints** (`https://api.india.delta.exchange` live,
  `https://cdn-ind.testnet.deltaex.org` sandbox). Keys: `DELTA_LIVE_*` / `DELTA_DEMO_*`.
- **Symbols: `COIN/USD:USD`** (USD-settled, NOT `/USDT:USDT`). Wallet currency: **USD**.
- **~194 perps** on India — broad altcoin coverage (incl. coins absent from Binance testnet).
- **Orders are in CONTRACTS, not coin units.** `contractSize` varies wildly (1000PEPE=1000, many
  alts=100, majors=1, stocks=0.01). `_enter_delta`/`_close_delta` convert: `contracts = coin_qty / contractSize`.
- **Brackets:** Delta uses `create_order(sym,"market",side,amt,None,{"type":"future","stop_price":px,
  "stop_order_type":"stop_loss_order"|"take_profit_order","reduce_only":True})`. NOT `STOP_MARKET`,
  NOT `*_market` enum (rejected as bad_schema). `reduce_only` is **snake_case**. **No native trailing.**
- **Position side is reported as `buy`/`sell`** (not long/short) — `_close_delta` handles both.
- Delta code lives in `loop_trader.py`: `account_client(live, exchange="delta")`, `_enter_delta`,
  `_close_delta`, `_quote_ccy`. Multi-exchange threaded via the global `--exchange` flag.
- Known limitation: `state --exchange delta` does NOT display Delta's stop/TP orders (the display
  helper is Binance-specific). The stops ARE placed (enter verifies `stop_live:true`); only the
  *display* is missing. Execution/dedup don't depend on it. Extend `open_stops`/`open_tps` for Delta
  if you want the display.

---

## 5. The strategy (current)

**Market-wide volume-spike momentum, 15m timeframe.** NOT hardcoded — the AI is told it's autonomous and
can evolve its method via the playbook. But the deployed defaults are:

- **Signal:** `volspike --spike-tf 15m --top 20 --min-spike 15 --confirm-frac 1.0` (Phase 0).
  A coin qualifies if its latest 15m bar volume ≥ **15×** its 20-bar average **AND** the next candle
  holds **≥100%** of the spike volume (two-candle persistence filter — kills one-bar blips).
- **Direction is LOCKED to the spike candle:** green/up spike → long, red/down spike → short. The
  executor **rejects** any entry whose side contradicts the spike (no "long on a red candle").
- **Stop:** spike candle low (long) / high (short). **TP:** 1:4 R:R. The AI copies these from
  `spikes.json` (told not to recompute the stop).
- **Trailing (Binance only):** optional `trail_pct` per entry; default = 2× stop distance.
- **Management:** the AI reviews EVERY open position each run and may close (broken thesis / wrong-way /
  target / deeply underwater) via the `manage[]` array. Winners ride the trailing stop.

> ⚠️ Honest note for your context: backtests of the underlying volume-spike signal were **net-negative
> to break-even** (see the operator's research). The strategy is being run/observed, not proven. Don't
> represent it as a known edge. The *plumbing* (sizing, brackets, multi-account, exchanges) is solid;
> the *edge* is unproven.

---

## 6. Safety mechanisms (do not weaken)

- **Never naked:** every `enter` places a MANDATORY static stop. If the stop fails to place → the
  position is immediately market-closed and the enter aborts (`_enter_delta` and binance both).
- **Side guard:** `execute-decisions` rejects entries whose side contradicts the spike direction.
- **Geometry guard:** `volspike` drops rows where the confirmation candle pushed price past the spike
  extreme (stop would be on the wrong side); `enter` also rejects wrong-side stops.
- **Max-qty cap (Binance):** caps order qty to `min(LOT_SIZE, MARKET_LOT_SIZE)` maxQty (`-4005` fix).
- **Pre-flight `validate_entry`:** per-account, loads that venue's market list and skips coins not
  listed there (kills BadSymbol) + stop>0.
- **Dedup / stack guard:** never stacks a 2nd position on a coin already held (binance via `held` set +
  `get_position`; delta via `_enter_delta` position check).
- **Salvage:** recovers a printed-but-not-written `decisions.json`.
- **Rate-limit retry:** `volspike` backs off on 429s.

---

## 7. Common operations

```bash
cd /root/trader
set -a; . ./.env; set +a              # load secrets into env for manual CLI use
export LOOP_TRADER_ALLOW_LIVE=1       # required for any --live command

# --- read-only inspection ---
# Binance demo (master) state:
LOOP_TRADER_DATADIR=/root/trader/loop_trader_data .venv/bin/python3 loop_trader.py state
# Binance LIVE state:
LOOP_TRADER_DATADIR=/root/trader/loop_trader_data_live .venv/bin/python3 loop_trader.py --live state
# Delta LIVE state:
LOOP_TRADER_DATADIR=/root/trader/loop_trader_data_delta .venv/bin/python3 loop_trader.py --live --exchange delta state
# Current spike candidates:
.venv/bin/python3 loop_trader.py volspike --spike-tf 15m --top 20 --min-spike 15 --confirm-frac 1.0

# --- the brain loop ---
tail -n 60 loop_trader_data_brain/cron.log        # what the brain did
cat loop_trader_data_brain/decisions.json.last    # last decision set
crontab -l | grep brain                           # cron wiring

# --- manual order (CAREFUL, real money on --live) ---
# dry-run first (no order placed):
.venv/bin/python3 loop_trader.py --live --exchange delta enter --symbol ADA --side long \
  --stop <below_px> --tp <above_px> --risk-pct 0.5 --leverage 5 --dry-run
# close:
.venv/bin/python3 loop_trader.py --live --exchange delta close --symbol ADA --reason "..."
```

> When manually testing live, the SSH command may **execute on the VPS even if you cancel the tool
> call locally** — a cancelled real-order test during this session still placed the order. Treat any
> sent `enter --live` (without `--dry-run`) as a real order.

---

## 8. Deploy discipline

- **The VPS working tree is canonical** for runtime; edit there, then reconcile to git (Section 1).
- For `loop_trader.py` multi-line edits, prefer a Python patch script (`read()` + `replace(old,new,1)`
  with `assert count==1`) over fragile sed — that's how all this session's edits were made.
- Always `py_compile` after editing: `.venv/bin/python3 -m py_compile loop_trader.py`.
- Back up before risky edits: `cp -a loop_trader.py loop_trader.py.bak-<what>-$(date -u +%Y%m%d%H%M%S)`.
  (`*.bak-*` is gitignored.)
- Python changes are picked up next cron run automatically (no restart). The cron is the only runner.
- **Never `scp` a stale local file over a VPS file** without diffing first — you'll revert live work.
  Another agent ("Antigravity") also edits this box; trust the VPS working tree and diff before
  overwriting.

---

## 9. Current state & open items (as of handoff)

- Engine: **Claude Code** (`claude-sonnet-4-6`), cron every 15 min.
- All three accounts **enabled and live-tested**. Binance live + Delta live are placing real money on
  A-grade spikes.
- **Live risk levels:** Binance live = **1.0%/trade**, Delta live = **0.5%/trade**. (Operator was
  asked whether to keep Binance at 1.0% or revert to 0.5% — unresolved; confirm before assuming.)
- Delta integration just completed + verified end-to-end (enter/stop/TP/close/flat). First live Delta
  trade happens when an A-grade spike lands on a Delta-India-listed coin.
- An **observer** workflow (a separate Claude on the operator's Mac) periodically health-checks both
  loops read-only. The VPS is the trader; the Mac is the observer — don't conflate them.

### Good first things to verify when you take over
1. `git status` clean on the VPS; HEAD == `origin/main`.
2. `crontab -l` shows `BRAIN_CLI=claude`.
3. Last few `cron.log` runs ended `exit 0` and wrote `decisions.json`.
4. Live account states match expectations (no unexpected/naked positions); every open position has a
   live stop.
5. `brain_accounts.json` live flags/risk are what the operator intends.

---

## 10. Golden rules

1. **Real money is live.** Verify against exchange ground truth (`state` / direct ccxt), not just logs.
2. **Never naked** — every position must have a live stop. Don't weaken the abort-on-stop-failure.
3. **Don't flip `live.enabled` or raise risk** without the operator's explicit instruction.
4. **Keep all three (VPS / GitHub / local) in sync**; commit your work with clear messages.
5. **Don't commit secrets** (`.env`) or runtime config (`loop_trader_data*`, `brain_accounts.json`).
6. The strategy edge is **unproven** — be honest about that; improve plumbing and observe outcomes.
