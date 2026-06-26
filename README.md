# trader — autonomous crypto-trading brain

A portable, self-contained **AI decision brain** for Binance USDT-M futures. One headless-agent iteration
analyzes the market **once** and emits a JSON of trade decisions; a deterministic executor then **mirrors**
those decisions across any number of accounts, each sized and gated independently. The AI just decides
(coin, direction, stop, take-profit, conviction grade) — sizing, bracketing and execution are pure Python.

## Quick start (any VPS)

```bash
git clone https://github.com/hrubee/trader.git
cd trader
bash install.sh
# then edit .env with your Binance keys, review loop_trader_data_brain/brain_accounts.json, and:
bash vps_brain_run.sh           # one iteration; cron runs it every 20 min automatically
tail -40 loop_trader_data_brain/cron.log
```

`install.sh` creates the venv + installs deps, resolves all paths to this clone, seeds the playbook,
creates `.env`, and installs the cron schedule. It's idempotent.

## Architecture

- **Phase 1 — AI brain** (`vps_brain_run.sh` → calls your agent CLI with `loop_trader_data_brain/vps_brain_prompt.txt`):
  reads the shared `PLAYBOOK.md`, reviews the master account's recent net-of-fee results, scans the market once,
  and **writes `decisions.json`** `{market_view, manage:[…], entries:[{symbol,side,stop,tp,grade,reason}]}`.
- **Phase 2 — executor** (`loop_trader.py execute-decisions`): replays that JSON across `brain_accounts.json`.
  Each account: programmatic risk-sizing (`risk_pct`), conviction gate (`min_grade`), and a full reduce-only
  bracket (static stop + trailing + TP). Per-account ledgers are isolated.

## Accounts (`loop_trader_data_brain/brain_accounts.json`)

```json
{"accounts":[
  {"name":"demo","datadir":".../loop_trader_data","live":false,"risk_pct":1.0,"leverage":5,"min_grade":"B","master":true,"enabled":true},
  {"name":"live","datadir":".../loop_trader_data_live","live":true,"risk_pct":0.5,"leverage":5,"min_grade":"B","enabled":true}
]}
```

Add an account = append a block (unique `datadir`). `min_grade` `A` = only highest-conviction trades, `B` = also medium.
The AI cost is ~constant in account count — mirroring is a free Python loop.

## Self-improvement

The brain owns and evolves `loop_trader_data_brain/PLAYBOOK.md` from its **own net-of-fee outcomes** — after each
closed trade it runs a retrospective and refines one rule. `brain/PLAYBOOK.seed.md` is the starting rulebook.

## Agent-agnostic

The wrapper calls whatever agent CLI you set:

```bash
BRAIN_CLI=claude BRAIN_MODEL=claude-sonnet-4-6 bash vps_brain_run.sh    # default (claude: -p / --model)
BRAIN_CLI=hermes BRAIN_MODEL= bash vps_brain_run.sh                     # hermes: --yolo -z (-m if set); leave
                                                                       # BRAIN_MODEL empty to use hermes' own default
# optional for hermes: BRAIN_TOOLSETS=shell,files  (defaults are usually fine)
```

The wrapper auto-detects the CLI flavor by name (`hermes*` → `--yolo -z`/`-m`; otherwise claude-style `-p`/`--model`).

## Safety

- Live trading requires `BINANCE_LIVE_*` keys **and** the account's `"live": true`. Demo (testnet) is the default.
- Every entry is bracketed (static stop + trailing + reduce-only TP); the executor aborts an entry rather than
  leave a naked position. Risk is sized off the stop distance per account.
- `.env` and all live data dirs are git-ignored — **never commit keys or ledgers**.

## Commands (`loop_trader.py`)

`scan` · `price` · `state` · `enter` · `close` · `set-tp` · `cancel-stops` · `journal` · `review` ·
`note` · `execute-decisions`. Run with `--live` (gated by `LOOP_TRADER_ALLOW_LIVE=1`) for the live account.
