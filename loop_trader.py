#!/usr/bin/env python3
"""loop_trader.py — trading infrastructure for the Claude /loop crypto-decision agent.

PURPOSE: give the in-loop agent (you, Claude) clean commands to (1) FETCH + summarize market
data, (2) read ACCOUNT state, and (3) PLACE / MANAGE risk-sized trades with protective stops,
on BINANCE FUTURES TESTNET. You read `scan`/`state`, reason about whether to enter, then call
`enter`/`close`. Every command prints JSON to stdout.

ARCHITECTURE (mirrors the live demo bots): market DATA comes from Binance **mainnet** public
klines/tickers (testnet's own data is thin/unreliable), while ORDERS go to **testnet** (your
DEMO keys). So your decisions use real price action; your fills are paper.

SAFETY:
  - Defaults to TESTNET (sandbox). Going live needs BOTH `--live` AND env LOOP_TRADER_ALLOW_LIVE=1.
  - `enter` is risk-sized, ALWAYS attaches a reduce-only stop, and ABORTS+closes if the stop fails
    (never leaves a naked position). Refuses to stack a second position on a symbol (use --add).
  - Stop orders are fetched/cancelled with the binanceusdm `{"stop":True}` param (plain calls give
    false -2011/-2013 on trigger orders — the bug that orphaned stops on the live bot).
  - `--dry-run` prints intended actions without sending.

KEYS: read from the repo `.env`. testnet -> BINANCE_DEMO_API_KEY/SECRET ; live -> BINANCE_LIVE_*.

COMMANDS (all accept --json; run with no args for this help):
  scan   [--tf 1h] [--top 30] [--symbols BTC,ETH,...] [--lookback 250]
         Market snapshot per coin (price, EMA9/50/200 + %dist, ATR/ATR%, ADX, N-bar hi/lo dist,
         vol ratio, 24h%, last 5 candles). THE command you analyze each loop.
  price  <SYMBOL> [--tf 1h] [--n 60]      Deeper single-coin view (more candles + indicators).
  state                                   Wallet, open positions (entry/mark/uPnL/lev), open orders+stops.
  enter  --symbol SYM --side long|short --stop PRICE [--tp PRICE] [--risk-pct 1.0]
         [--leverage 5] [--reason "..."] [--add] [--dry-run]
         Set isolated leverage, market-enter sized so the stop = risk-pct of wallet, attach a
         reduce-only stop (+ optional reduce-only TP). Verifies the stop is live or aborts.
  close  --symbol SYM [--reason "..."] [--dry-run]   Market-close + cancel all stops + journal the result.
  cancel-stops --symbol SYM                          Cancel all reduce-only stops for a symbol.
  journal [--n 40]   READ THIS FIRST EACH LOOP: stats (win%/avgR/PnL) + open trades + recent narrative.
                     Auto-reconciles stop-outs. The file is loop_trader_data/JOURNAL.md (pairs every
                     trade's thesis with its realized R/PnL outcome) — learn from it before deciding.
  note   "..."                                       Append your own reflection/lesson to the journal.
  log    [--n 20]                                     Tail the raw decision log (JSON).

Run:  uv run --no-sync python loop_trader.py scan --tf 1h --top 25
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")   # keep stdout clean JSON for the loop to parse

REPO = os.path.dirname(os.path.abspath(__file__))
ENVF = os.path.join(REPO, ".env")
DATADIR = os.environ.get("LOOP_TRADER_DATADIR") or os.path.join(REPO, "loop_trader_data")
TRADES_CSV = os.path.join(DATADIR, "trades.csv")
DECISIONS = os.path.join(DATADIR, "decisions.jsonl")
JOURNAL = os.path.join(DATADIR, "JOURNAL.md")        # human/agent-readable trading journal (read each loop)
OPEN_TR = os.path.join(DATADIR, "open_trades.json")  # live ledger: entry/stop/reason per open trade
CLOSED_TR = os.path.join(DATADIR, "closed_trades.jsonl")  # structured closed-trade records (for stats)
QUOTE = "USDT"
INST_SUFFIX = "/USDT:USDT"


# ── env / clients ──────────────────────────────────────────────────────────────
def load_env():
    try:
        for ln in open(ENVF):
            ln = ln.strip()
            if ln and "=" in ln and not ln.startswith("#"):
                k, v = ln.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        pass


def market_client():
    """Public MAINNET client for real market data (no keys needed)."""
    import ccxt
    ex = ccxt.binanceusdm({"options": {"defaultType": "future"}, "enableRateLimit": True})
    ex.load_markets()
    return ex


def account_client(live=False):
    """Authenticated futures client via the PROVEN BinanceExchangeAdapter (same code the live demo bots
    use). testnet (default) routes to Binance Demo Trading (demo-fapi.binance.com via the adapter's
    enable_demo_trading + URL overrides — ccxt deprecated set_sandbox_mode for futures). live is gated."""
    if live:
        if os.environ.get("LOOP_TRADER_ALLOW_LIVE") != "1":
            die("refusing --live without env LOOP_TRADER_ALLOW_LIVE=1 (this toolkit is for the demo account)")
        os.environ["BINANCE_SANDBOX"] = "0"
    else:
        os.environ["BINANCE_SANDBOX"] = "1"          # adapter -> BINANCE_DEMO_* keys + demo-fapi URLs
    sys.path.insert(0, os.path.join(REPO, "platforms", "binance"))
    from adapter import BinanceExchangeAdapter
    ex = BinanceExchangeAdapter()._exchange
    ex.options["defaultType"] = "future"
    ex.options["warnOnFetchOpenOrdersWithoutSymbol"] = False
    if not ex.apiKey:
        die("missing API keys in .env (%s)" % ("BINANCE_LIVE_*" if live else "BINANCE_DEMO_*"))
    ex.load_markets()
    return ex


def die(msg):
    print(json.dumps({"error": msg})); sys.exit(1)


def out(obj):
    print(json.dumps(obj, default=str, separators=(",", ":")))


# ── indicators (vendored, self-contained) ──────────────────────────────────────
def ema(c, p):
    return pd.Series(c).ewm(span=p, adjust=False).mean().values


def atr(h, l, c, p=14):
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    a = np.full(len(tr), np.nan)
    if len(tr) > p:
        a[p] = tr[1:p + 1].mean()
        for i in range(p + 1, len(tr)):
            a[i] = (a[i - 1] * (p - 1) + tr[i]) / p
    return a


def adx(h, l, c, period=14):
    n = len(c)
    tr = np.zeros(n); pdm = np.zeros(n); mdm = np.zeros(n)
    for i in range(1, n):
        up = h[i] - h[i - 1]; dn = l[i - 1] - l[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))

    def rma(x):
        o = np.zeros(n)
        if n <= period:
            return o
        o[period] = x[1:period + 1].sum()
        for i in range(period + 1, n):
            o[i] = o[i - 1] - o[i - 1] / period + x[i]
        return o
    atr_s, pdm_s, mdm_s = rma(tr), rma(pdm), rma(mdm)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100 * np.where(atr_s > 0, pdm_s / atr_s, 0)
        mdi = 100 * np.where(atr_s > 0, mdm_s / atr_s, 0)
        dx = 100 * np.where((pdi + mdi) > 0, np.abs(pdi - mdi) / (pdi + mdi), 0)
    ax = np.zeros(n)
    if n > 2 * period:
        ax[2 * period] = dx[period + 1:2 * period + 1].mean()
        for i in range(2 * period + 1, n):
            ax[i] = (ax[i - 1] * (period - 1) + dx[i]) / period
    return ax, (pdi[-1] if n else 0), (mdi[-1] if n else 0)


def rnd(x, s=5):
    try:
        return float("%.*g" % (s, float(x)))
    except Exception:
        return x


# ── universe / data ─────────────────────────────────────────────────────────────
def universe(mkt, top, min_vol=3e7):
    """Top-N liquid CRYPTO USDT-perps by 24h quote volume — excludes tokenized stocks / index
    perps (underlyingType != COIN), so the agent reasons over crypto only."""
    tick = mkt.fetch_tickers()
    cand = []
    for sym, t in tick.items():
        if not sym.endswith(INST_SUFFIX):
            continue
        m = mkt.markets.get(sym) or {}
        if (m.get("info", {}) or {}).get("underlyingType") not in (None, "COIN"):
            continue                                  # drop INDEX/stock-token perps
        qv = float(t.get("quoteVolume") or 0)
        if qv >= min_vol:
            cand.append((qv, sym.split("/")[0]))
    cand.sort(reverse=True)
    return [b for _, b in cand[:top]]


def klines(mkt, base, tf, limit):
    try:
        o = mkt.fetch_ohlcv(base + INST_SUFFIX, timeframe=tf, limit=limit)
        if not o or len(o) < 60:
            return None
        a = np.array(o, dtype=float)
        return a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5]  # t,o,h,l,c,v
    except Exception:
        return None


def snapshot(base, k):
    t, o, h, l, c, v = k
    j = len(c) - 1                      # last (forming) bar; use closed bar for signals
    cb = len(c) - 2                     # last CLOSED bar
    e9, e50, e200 = ema(c, 9), ema(c, 50), ema(c, 200)
    a = atr(h, l, c, 14)
    ax, pdi, mdi = adx(h, l, c, 14)
    vavg = pd.Series(v).rolling(20).mean().values
    price = c[cb]
    n40hi = h[max(0, cb - 40):cb].max() if cb > 1 else h[cb]
    n40lo = l[max(0, cb - 40):cb].min() if cb > 1 else l[cb]
    last5 = [[rnd(o[i]), rnd(h[i]), rnd(l[i]), rnd(c[i])] for i in range(max(0, cb - 4), cb + 1)]
    chg24 = None
    # 24h change via bars if tf small enough handled by caller; approximate from first/last available
    return {
        "coin": base, "price": rnd(price),
        "ema9": rnd(e9[cb]), "ema50": rnd(e50[cb]), "ema200": rnd(e200[cb]),
        "pct_vs_ema9": rnd((price / e9[cb] - 1) * 100, 3) if e9[cb] else None,
        "pct_vs_ema50": rnd((price / e50[cb] - 1) * 100, 3) if e50[cb] else None,
        "pct_vs_ema200": rnd((price / e200[cb] - 1) * 100, 3) if e200[cb] else None,
        "trend": ("up" if (e9[cb] > e50[cb] > e200[cb]) else "down" if (e9[cb] < e50[cb] < e200[cb]) else "mixed"),
        "atr": rnd(a[cb]), "atr_pct": rnd(a[cb] / price * 100, 3) if price else None,
        "adx": rnd(ax[cb], 4), "di": ("+" if pdi > mdi else "-"),
        "vol_ratio": rnd(v[cb] / vavg[cb], 3) if (cb < len(vavg) and vavg[cb]) else None,
        "n40_high": rnd(n40hi), "n40_low": rnd(n40lo),
        "dist_to_40hi_pct": rnd((n40hi / price - 1) * 100, 3) if price else None,
        "dist_to_40lo_pct": rnd((price / n40lo - 1) * 100, 3) if (price and n40lo) else None,
        "last5_ohlc": last5,
    }


# ── stop-order helpers (the {"stop":True} gotcha baked in) ───────────────────────
def open_stops(ex, sym):
    """All resting reduce-only STOP orders for sym (queries both books)."""
    oo = []
    for params in ({}, {"stop": True}):
        try:
            oo += ex.fetch_open_orders(sym, params=params)
        except Exception:
            pass
    seen = {}
    for o in oo:
        typ = (o.get("type") or "").upper()
        is_stop = ("STOP" in typ) or o.get("triggerPrice") or o.get("stopPrice")
        ro = o.get("reduceOnly")
        if is_stop and (ro is None or ro):
            seen[o.get("id")] = o
    return list(seen.values())


def open_tps(ex, sym):
    """All resting reduce-only LIMIT take-profit orders for sym (regular book)."""
    tps = []
    try:
        for o in ex.fetch_open_orders(sym):
            if (o.get("type") or "").upper() == "LIMIT" and o.get("reduceOnly"):
                tps.append(o)
    except Exception:
        pass
    return tps


def cancel_one(ex, sym, oid):
    for params in ({}, {"stop": True}):       # trigger orders need the stop param to cancel
        try:
            ex.cancel_order(oid, sym, params)
            return True
        except Exception:
            continue
    return False


def cancel_all_stops(ex, sym, keep=None):
    n = 0
    for o in open_stops(ex, sym):
        if o.get("id") == keep:
            continue
        if cancel_one(ex, sym, o.get("id")):
            n += 1
    return n


def cancel_orphan_orders(ex, live_syms):
    """Cancel orphaned reduce-only orders — bracket legs left behind when a position closes via a fill
    (Binance does NOT always auto-cancel the surviving static/trailing/TP legs). SAFE BY CONSTRUCTION: only
    cancels reduce-only orders on symbols with NO open position; a reduce-only order with no underlying
    position can never execute, so cancelling is always correct, and a symbol that HAS an open position is
    never touched (live protection is never reduced). Sweeps both the regular and trigger order books.
    Returns [(symbol, oid), ...] cancelled. (An orphan on a symbol that currently has a *new* position is
    left in place — it's a same-direction redundant reduce-only and gets swept once that symbol goes flat.)"""
    seen = {}
    for params in ({}, {"stop": True}):               # regular book + trigger/conditional book
        try:
            for o in ex.fetch_open_orders(params=params):
                seen[o.get("id")] = o
        except Exception:
            pass
    cancelled = []
    for oid, o in seen.items():
        sym = o.get("symbol")
        if not sym or sym in live_syms or not o.get("reduceOnly"):
            continue
        if cancel_one(ex, sym, oid):
            cancelled.append((sym, oid))
    return cancelled


def get_position(ex, sym):
    for p in ex.fetch_positions([sym]):
        if abs(float(p.get("contracts") or 0)) > 0:
            return p
    return None


# ── logging ─────────────────────────────────────────────────────────────────────
def record(action, d):
    os.makedirs(DATADIR, exist_ok=True)
    ts = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(DECISIONS, "a") as f:
        f.write(json.dumps({"ts": ts, "action": action, **d}, default=str) + "\n")
    newf = not os.path.exists(TRADES_CSV)
    with open(TRADES_CSV, "a", newline="") as f:
        w = csv.writer(f)
        if newf:
            w.writerow(["ts", "action", "symbol", "side", "price", "qty", "stop", "tp", "reason", "extra"])
        w.writerow([ts, action, d.get("symbol"), d.get("side"), d.get("price"), d.get("qty"),
                    d.get("stop"), d.get("tp"), d.get("reason", ""), d.get("extra", "")])


# ── trading journal (auto-maintained; the loop reads JOURNAL.md to learn from past trades) ──────
def _ts():
    return dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%MZ")


def journal_line(text):
    os.makedirs(DATADIR, exist_ok=True)
    if not os.path.exists(JOURNAL):
        open(JOURNAL, "w").write(
            "# Loop Trader — Trading Journal\n\n"
            "> Auto-maintained by loop_trader.py. **Read the tail of this file at the start of every /loop**\n"
            "> to learn from prior trades + the reasoning behind them. Oldest->newest. ENTER pairs a thesis\n"
            "> with the trade; CLOSE pairs it with the realized R/PnL outcome; NOTE = your own reflections.\n\n")
    open(JOURNAL, "a").write(text.rstrip() + "\n")


def load_open():
    try:
        return json.load(open(OPEN_TR))
    except Exception:
        return {}


def save_open(d):
    os.makedirs(DATADIR, exist_ok=True)
    json.dump(d, open(OPEN_TR, "w"))


def realized_pnl_since(ex, sym, ts_iso):
    """Actual realized PnL for sym since the entry ts (so stop-outs/TPs are booked with the REAL number,
    not an assumption). Returns float or None if the income endpoint isn't reachable."""
    try:
        start = int(dt.datetime.strptime(ts_iso, "%Y-%m-%dT%H:%MZ").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
        mid = ex.market(sym)["id"]
        inc = ex.fapiPrivateGetIncome({"symbol": mid, "incomeType": "REALIZED_PNL", "startTime": start, "limit": 200})
        return sum(float(e.get("income") or 0) for e in inc)
    except Exception:
        return None


def book_closed(rec, sym, exit_px, R, pnl, exit_reason):
    base = sym.split("/")[0]
    rt = _ts()
    os.makedirs(DATADIR, exist_ok=True)
    open(CLOSED_TR, "a").write(json.dumps({
        "ts": rt, "symbol": sym, "side": rec.get("side"), "entry": rec.get("entry"), "exit": exit_px,
        "R": round(R, 3), "pnl": round(pnl, 3), "exit_reason": exit_reason,
        "entry_reason": rec.get("reason", ""), "entry_ts": rec.get("ts")}) + "\n")
    retro = "WIN — thesis held" if R > 0.05 else ("LOSS — stopped/thesis failed" if R < -0.05 else "scratch")
    journal_line("[%s] CLOSE %s %s%s | result %+.2fR / $%+.2f | exit:%s | thesis was: %s | retro: %s" % (
        rt, base, rec.get("side", ""), (" @%g" % exit_px if exit_px else ""), R, pnl,
        exit_reason, rec.get("reason") or "(none)", retro))


def reconcile(ex):
    """Book any open trade whose position has vanished (stopped out / TP / external) into the journal with
    its REAL realized PnL, so the journal stays accurate without a manual `close`. ALSO auto-sweeps orphaned
    reduce-only orders (bracket legs Binance leaves behind on a fill-close). Returns #booked."""
    live = {p["symbol"] for p in ex.fetch_positions() if abs(float(p.get("contracts") or 0)) > 0}
    orphans = cancel_orphan_orders(ex, live)
    if orphans:
        journal_line("[%s] ORPHAN-CLEANUP cancelled %d leftover reduce-only order(s) on flat symbol(s): %s" % (
            _ts(), len(orphans), ", ".join("%s:%s" % (s, o) for s, o in orphans)))
    ot = load_open()
    if not ot:
        return 0
    booked = 0
    for sym, rec in list(ot.items()):
        if sym in live:
            continue
        risk = rec.get("risk_usd") or 0
        pnl = realized_pnl_since(ex, sym, rec.get("ts", ""))
        if pnl is None:                      # income unavailable -> assume the stop fired (designed -1R)
            pnl = -risk; R = -1.0; xr = "presumed-stop"
        else:
            R = (pnl / risk) if risk else 0.0; xr = "stop/external"
        book_closed(rec, sym, None, R, pnl, xr)
        del ot[sym]; booked += 1
    save_open(ot)
    return booked


def journal_stats():
    if not os.path.exists(CLOSED_TR):
        return {"closed": 0}
    rows = [json.loads(x) for x in open(CLOSED_TR).read().strip().splitlines() if x.strip()]
    if not rows:
        return {"closed": 0}
    Rs = [r["R"] for r in rows]; wins = [r for r in Rs if r > 0]
    return {"closed": len(rows), "win_pct": round(100 * len(wins) / len(rows), 1),
            "avg_R": round(sum(Rs) / len(Rs), 3), "total_pnl": round(sum(r["pnl"] for r in rows), 2)}


# ── commands ─────────────────────────────────────────────────────────────────────
def cmd_scan(args):
    mkt = market_client()
    bases = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else universe(mkt, args.top)
    rows = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        for b, k in zip(bases, ex.map(lambda b: klines(mkt, b, args.tf, args.lookback), bases)):
            if k:
                rows[b] = k
    snaps = [snapshot(b, k) for b, k in rows.items()]
    ranked_by = "volume_universe"
    if getattr(args, "sort", "") == "vol":            # VOLUME-SURGE first: rank by current-bar vol_ratio desc
        snaps.sort(key=lambda s: (s.get("vol_ratio") if s.get("vol_ratio") is not None else 0), reverse=True)
        ranked_by = "vol_ratio(surge)"
    out({"mode": "testnet" if not args.live else "LIVE", "tf": args.tf, "ranked_by": ranked_by,
         "data_source": "binance-mainnet",
         "n": len(snaps), "ts": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "coins": snaps})


def cmd_gainers(args):
    """The day's biggest MOVERS: top-N crypto USDT-perps ranked by 24h % change (use --losers for the
    bottom). Same per-coin snapshot as `scan` PLUS pct_24h, so you can hunt early-momentum breakouts on
    coins that are actually running today. min_vol keeps it liquid (no illiquid micro-cap junk)."""
    mkt = market_client()
    tick = mkt.fetch_tickers()
    cand = []
    for sym, t in tick.items():
        if not sym.endswith(INST_SUFFIX):
            continue
        m = mkt.markets.get(sym) or {}
        if (m.get("info", {}) or {}).get("underlyingType") not in (None, "COIN"):
            continue
        qv = float(t.get("quoteVolume") or 0)
        pct = t.get("percentage")
        if qv >= args.min_vol and pct is not None:
            cand.append((float(pct), qv, sym.split("/")[0]))
    cand.sort(reverse=not args.losers)                 # gainers: % desc; losers: % asc
    sel = cand[:args.top]
    pctmap = {b: p for p, _, b in cand}
    bases = [b for _, _, b in sel]
    rows = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        for b, k in zip(bases, ex.map(lambda b: klines(mkt, b, args.tf, args.lookback), bases)):
            if k:
                rows[b] = k
    snaps = []
    for b, k in rows.items():
        s = snapshot(b, k)
        p = pctmap.get(b)
        s["pct_24h"] = round(p, 2) if p is not None else None
        snaps.append(s)
    snaps.sort(key=lambda s: (s.get("pct_24h") if s.get("pct_24h") is not None else 0), reverse=not args.losers)
    out({"mode": "testnet" if not args.live else "LIVE", "tf": args.tf, "ranked_by": ("24h_pct_loss" if args.losers else "24h_pct_gain"),
         "data_source": "binance-mainnet", "n": len(snaps),
         "ts": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "coins": snaps})


def _spike_info(k, win=60, confirm_frac=0.8):
    """TWO-CANDLE PERSISTENCE filter. The SPIKE candle (vol >> avg of the prior `win` bars) must be
    CONFIRMED by the NEXT candle holding a close-to-spike volume level: confirm_vol >= confirm_frac *
    spike_vol. Only confirmed spikes are tradeable (confirm_frac=0 disables the check). Returns the SPIKE
    candle's OHLC + vr + the confirmation ratio + the confirmation candle's close (the entry reference),
    or None if not enough bars.
      indices: len(c)-1 = forming bar ; len(c)-2 = confirmation candle (last CLOSED) ; len(c)-3 = spike candle."""
    if not k:
        return None
    _, o, h, l, c, v = k
    confirm = len(c) - 2                 # most recent CLOSED bar = the confirmation candle
    spike = confirm - 1                  # the bar before it = the spike candle
    if spike < win + 1:
        return None
    avg = sum(v[spike - win:spike]) / float(win)
    if not avg:
        return None
    spike_vol = v[spike]
    confirm_ratio = (v[confirm] / spike_vol) if spike_vol else 0.0
    return {"vr": spike_vol / avg, "confirm_ratio": confirm_ratio,
            "confirmed": (confirm_frac <= 0) or (confirm_ratio >= confirm_frac),
            "o": float(o[spike]), "h": float(h[spike]), "l": float(l[spike]), "c": float(c[spike]),
            "confirm_c": float(c[confirm])}


def cmd_volspike(args):
    """MARKET-WIDE volume-spike scanner. Detects spikes on the FAST tf (--spike-tf, default 1m) across EVERY
    liquid USDT-perp, and for the biggest spikes emits a READY-TO-TRADE row — side/stop/tp computed
    deterministically so the agent just COPIES them (no arithmetic, no direction guessing):
      side  = the spike candle's COLOR: green/up bar -> long ; red/down bar -> short
      stop  = the spike candle's LOW (long) or HIGH (short)
      tp    = 1:4 risk:reward from entry_ref off that stop
    Only the agent's job: pick rows with spike_vol_ratio >= the threshold (10x) and pass side/stop/tp through."""
    mkt = market_client()
    tick = mkt.fetch_tickers()
    cand = []
    for sym, t in tick.items():
        if not sym.endswith(INST_SUFFIX):
            continue
        m = mkt.markets.get(sym) or {}
        if (m.get("info", {}) or {}).get("underlyingType") not in (None, "COIN"):
            continue
        b = sym.split("/")[0]
        if not b.isascii():                              # drop CJK/exotic-named listings (model can't reference them; BadSymbol)
            continue
        qv = float(t.get("quoteVolume") or 0)
        if qv >= args.min_vol:
            cand.append((qv, b))
    cand.sort(reverse=True)
    bases = [b for _, b in cand[:args.max_scan]]        # whole liquid market (safety-capped)
    info = {}
    with ThreadPoolExecutor(max_workers=24) as ex:
        for b, k in zip(bases, ex.map(lambda b: klines(mkt, b, args.spike_tf, args.spike_lookback), bases)):
            d = _spike_info(k, args.avg_bars, getattr(args, "confirm_frac", 0.8))
            if d is not None and d["vr"] >= args.min_spike and d["confirmed"]:
                info[b] = d
    ranked = sorted(info, key=lambda b: info[b]["vr"], reverse=True)[:args.top]
    coins = []
    for b in ranked:
        d = info[b]
        up = d["c"] >= d["o"]                            # green spike -> long ; red spike -> short
        side = "long" if up else "short"
        entry = d["confirm_c"]                           # entry AFTER the confirmation candle closes (~current)
        stop = d["l"] if up else d["h"]                  # rule #3: stop at spike candle low/high
        # confirmation candle can close past the spike extreme -> stop ends up on the wrong side of entry
        # (e.g. up-spike but price already fell below the spike low) = setup invalidated; skip such rows
        if (up and stop >= entry) or ((not up) and stop <= entry):
            continue
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        tp = entry + 4 * risk if up else entry - 4 * risk   # rule #4: 1:4 RR
        coins.append({"coin": b, "spike_vol_ratio": round(d["vr"], 2),
                      "confirm_vol_ratio": round(d["confirm_ratio"], 2), "spike_dir": "up" if up else "down",
                      "side": side, "entry_ref": rnd(entry), "stop": rnd(stop), "tp": rnd(tp),
                      "spike_high": rnd(d["h"]), "spike_low": rnd(d["l"]),
                      "rr": "1:4", "spike_tf": args.spike_tf})
    coins.sort(key=lambda s: s["spike_vol_ratio"], reverse=True)
    out({"mode": "testnet" if not args.live else "LIVE", "spike_tf": args.spike_tf, "min_spike": args.min_spike,
         "ranked_by": "spike_vol_ratio@" + args.spike_tf, "data_source": "binance-mainnet", "n": len(coins),
         "note": "side/stop/tp are READY — copy them. side=spike color (green=long,red=short); stop=spike candle low/high; tp=1:4.",
         "ts": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "coins": coins})


def cmd_price(args):
    mkt = market_client()
    b = args.symbol.upper().replace("/USDT:USDT", "").replace("USDT", "")
    k = klines(mkt, b, args.tf, max(args.n + 30, 250))
    if not k:
        die("no data for %s" % b)
    t, o, h, l, c, v = k
    snap = snapshot(b, k)
    tail = [{"t": dt.datetime.utcfromtimestamp(t[i] / 1000).strftime("%m-%d %H:%M"),
             "o": rnd(o[i]), "h": rnd(h[i]), "l": rnd(l[i]), "c": rnd(c[i]), "v": rnd(v[i], 4)}
            for i in range(len(c) - args.n, len(c))]
    out({"summary": snap, "candles": tail})


def cmd_state(args):
    ex = account_client(args.live)
    try:
        reconcile(ex)        # auto-book any stopped-out/closed trades into the journal before reporting
    except Exception:
        pass
    bal = ex.fetch_balance()
    u = bal.get(QUOTE, {})
    poss = []
    for p in ex.fetch_positions():
        if abs(float(p.get("contracts") or 0)) > 0:
            e = float(p.get("entryPrice") or 0); m = float(p.get("markPrice") or 0)
            sym = p["symbol"]
            stops = open_stops(ex, sym)
            tps = open_tps(ex, sym)
            poss.append({"symbol": sym, "side": p.get("side"), "qty": p.get("contracts"),
                         "entry": rnd(e), "mark": rnd(m), "move_pct": rnd((m - e) / e * 100, 3) if e else 0,
                         "uPnL": rnd(p.get("unrealizedPnl")), "leverage": p.get("leverage"),
                         "stops": [{"trig": rnd(o.get("triggerPrice") or o.get("stopPrice")), "qty": o.get("amount")} for o in stops],
                         "tps": [{"px": rnd(o.get("price")), "qty": o.get("amount")} for o in tps]})
    out({"mode": "testnet" if not args.live else "LIVE",
         "wallet": {"total": rnd(u.get("total")), "free": rnd(u.get("free")), "used": rnd(u.get("used"))},
         "positions": poss, "n_positions": len(poss)})


def cmd_enter(args):
    if args.side not in ("long", "short"):
        die("side must be long or short")
    mkt = market_client()
    b = args.symbol.upper().replace("/USDT:USDT", "").replace("USDT", "")
    sym = b + INST_SUFFIX
    px = float(mkt.fetch_ticker(sym)["last"])
    stop = float(args.stop)
    is_long = args.side == "long"
    if (is_long and stop >= px) or (not is_long and stop <= px):
        die("stop %.8g on wrong side of price %.8g for a %s" % (stop, px, args.side))
    ex = account_client(args.live)
    bal = ex.fetch_balance(); wallet = float(bal.get(QUOTE, {}).get("total") or 0)
    if wallet <= 0:
        die("wallet 0 — cannot size")
    existing = get_position(ex, sym)
    if existing and not args.add:
        die("position already open on %s (%s %s) — use --add to override" % (sym, existing.get("side"), existing.get("contracts")))
    stop_frac = abs(px - stop) / px
    risk_usd = args.risk_pct / 100.0 * wallet
    notional = risk_usd / stop_frac
    notional = min(notional, args.leverage * wallet)        # leverage cap
    qty = notional / px
    try:                                                    # cap to the symbol's max order qty (avoid -4005)
        mx = (((ex.market(sym) or {}).get("limits") or {}).get("amount") or {}).get("max")
        if mx and qty > float(mx):
            qty = float(mx) * 0.98
    except Exception:
        pass
    qty = float(ex.amount_to_precision(sym, qty))
    stop_px = float(ex.price_to_precision(sym, stop))
    tp_px = float(ex.price_to_precision(sym, args.tp)) if args.tp else None
    plan = {"symbol": sym, "side": args.side, "ref_px": rnd(px), "stop": rnd(stop_px), "tp": rnd(tp_px),
            "qty": qty, "notional": rnd(qty * px), "risk_pct": args.risk_pct, "risk_usd": rnd(risk_usd),
            "leverage": args.leverage, "wallet": rnd(wallet), "reason": args.reason}
    if qty <= 0:
        die("computed qty 0 (notional %.2f too small vs min) — %s" % (notional, json.dumps(plan)))
    if args.dry_run:
        out({"dry_run": True, "would_enter": plan}); return
    # set isolated leverage (best-effort; fails harmlessly if a position is open or unsupported)
    try:
        ex.set_margin_mode("isolated", sym)
    except Exception:
        pass
    try:
        ex.set_leverage(args.leverage, sym)
    except Exception:
        pass
    side = "buy" if is_long else "sell"
    order = ex.create_order(sym, "market", side, qty)
    fill = float(order.get("average") or order.get("price") or px)
    # Protective stops — EVERY trade gets BOTH (operator: "static SL + trailing SL at every trade"):
    #   1. STATIC STOP_MARKET at stop_px — the immutable disaster floor. MANDATORY: if it fails -> close + abort (never naked).
    #   2. native TRAILING_STOP_MARKET (callbackRate = the stop distance %) — locks profit as the trade runs.
    #      Real on LIVE; frozen/cosmetic on the demo venue (known bug, operator-accepted). --fixed-stop skips it (static only).
    # Plus a reduce-only TP LIMIT at tp_px. All three are reduce-only + full-qty; Binance auto-cancels the survivors
    # when the position closes (whichever of static/trailing/TP fills first wins). Coexistence verified on Binance.
    exit_side = "sell" if is_long else "buy"
    # trailing callbackRate: use the AI-specified --trail-pct when given, else derive from the stop distance
    _cr_src = args.trail_pct if getattr(args, "trail_pct", None) else abs(fill - stop_px) / fill * 100
    cr = max(0.1, min(10.0, round(_cr_src, 1)))   # Binance callbackRate 0.1–10%
    static_oid = None
    try:
        so = ex.create_order(sym, "STOP_MARKET", exit_side, qty, None, {"stopPrice": stop_px, "reduceOnly": True})
        static_oid = so.get("id")
    except Exception as e:
        ex.create_order(sym, "market", exit_side, qty, None, {"reduceOnly": True})
        record("ENTER_ABORT", {**plan, "price": rnd(fill), "extra": "static STOP placement failed -> closed: %r" % e})
        die("STATIC STOP placement FAILED -> position closed (no naked). %r" % e)
    trail_oid = None
    if not args.fixed_stop:
        try:
            tso = ex.create_order(sym, "TRAILING_STOP_MARKET", exit_side, qty, None, {"callbackRate": cr, "reduceOnly": True})
            trail_oid = tso.get("id")
        except Exception:
            trail_oid = None
    sl_kind = ("static+trailing %.1f%%" % cr) if trail_oid else ("static-only (trailing %s)" % ("skipped" if args.fixed_stop else "REJECTED"))
    # verify the static stop (the never-naked floor) is actually live (stop-param query)
    live_stop = any(o.get("id") == static_oid for o in open_stops(ex, sym))
    tp_oid = None
    if tp_px:
        try:
            to = ex.create_order(sym, "LIMIT", exit_side, qty, tp_px, {"reduceOnly": True})
            tp_oid = to.get("id")
        except Exception:
            pass
    res = {**plan, "filled": rnd(fill), "qty": qty, "static_oid": static_oid, "trail_oid": trail_oid,
           "trail_callback_pct": (cr if trail_oid else None), "stop_type": sl_kind, "stop_live": live_stop,
           "tp_oid": tp_oid, "actual_risk_pct": rnd(qty * abs(fill - stop_px) / wallet * 100, 3)}
    record("ENTER", {"symbol": sym, "side": args.side, "price": rnd(fill), "qty": qty,
                     "stop": rnd(stop_px), "tp": rnd(tp_px), "reason": args.reason,
                     "extra": "static=%s trail=%s(%.1f%%) tp=%s stop_live=%s" % (static_oid, trail_oid, cr, tp_oid, live_stop)})
    # journal: pair the trade with its thesis + add to the open-trades ledger (so close/reconcile can score it)
    ot = load_open()
    ot[sym] = {"side": args.side, "entry": rnd(fill), "stop": rnd(stop_px), "qty": qty,
               "risk_usd": rnd(risk_usd), "reason": args.reason, "ts": _ts(),
               "oid": static_oid, "static_oid": static_oid, "trail_oid": trail_oid, "stop_type": sl_kind,
               "tp": rnd(tp_px), "tp_oid": tp_oid}
    save_open(ot)
    journal_line("[%s] ENTER %s %s @%g | $%.0f notl | static %g + trailing %.1f%% [%s] (%.2f%%r,%dx) | thesis: %s" % (
        _ts(), b, args.side, fill, qty * fill, stop_px, cr, sl_kind, args.risk_pct, args.leverage, args.reason or "(none given)"))
    out({"entered": res})


def cmd_close(args):
    ex = account_client(args.live)
    b = args.symbol.upper().replace("/USDT:USDT", "").replace("USDT", "")
    sym = b + INST_SUFFIX
    p = get_position(ex, sym)
    if not p:
        cancel_all_stops(ex, sym)
        out({"closed": False, "note": "no open position; cleared any stray stops"}); return
    qty = abs(float(p["contracts"])); is_long = p.get("side") == "long"
    entry_px = float(p.get("entryPrice") or 0); mark = float(p.get("markPrice") or 0)
    if args.dry_run:
        out({"dry_run": True, "would_close": {"symbol": sym, "side": p.get("side"), "qty": qty}}); return
    order = ex.create_order(sym, "market", "sell" if is_long else "buy", qty, None, {"reduceOnly": True})
    exit_px = float(order.get("average") or order.get("price") or mark or entry_px)
    ncxl = cancel_all_stops(ex, sym)
    record("CLOSE", {"symbol": sym, "side": p.get("side"), "qty": qty, "price": rnd(exit_px),
                     "reason": args.reason, "extra": "cancelled %d stops" % ncxl})
    # journal: score the round-trip against its entry thesis
    ot = load_open(); rec = ot.pop(sym, None); save_open(ot)
    res = {"closed": True, "symbol": sym, "qty": qty, "exit": rnd(exit_px), "stops_cancelled": ncxl}
    if rec:
        entry = rec.get("entry") or entry_px; stop = rec.get("stop"); risk = rec.get("risk_usd") or 0
        diff = (exit_px - entry) if is_long else (entry - exit_px)
        rdist = abs(entry - stop) if stop else 0
        R = diff / rdist if rdist else 0.0
        pnl = (diff * qty) if not risk or rdist else 0.0
        book_closed(rec, sym, rnd(exit_px), R, pnl, args.reason or "manual")
        res["R"] = round(R, 3); res["pnl"] = round(pnl, 3)
    else:
        journal_line("[%s] CLOSE %s @%g | (no recorded entry — external/pre-existing position) | exit:%s" % (
            _ts(), b, exit_px, args.reason or "manual"))
    out(res)


def cmd_cancel_stops(args):
    ex = account_client(args.live)
    b = args.symbol.upper().replace("/USDT:USDT", "").replace("USDT", "")
    sym = b + INST_SUFFIX
    out({"cancelled": cancel_all_stops(ex, sym), "symbol": sym})


def cmd_retrail(args):
    """Convert an existing position's stop to a TRAILING_STOP_MARKET: place the trailing stop FIRST, then
    cancel the old fixed stop(s) — never naked. callbackRate from --pct, else the existing stop distance, else 2%."""
    ex = account_client(args.live)
    b = args.symbol.upper().replace("/USDT:USDT", "").replace("USDT", "")
    sym = b + INST_SUFFIX
    p = get_position(ex, sym)
    if not p:
        die("no open position on %s" % sym)
    qty = abs(float(p["contracts"])); is_long = p.get("side") == "long"
    entry = float(p.get("entryPrice") or 0)
    cr = args.pct
    if cr is None:
        stops = open_stops(ex, sym)
        if stops and entry:
            trig = float(stops[0].get("triggerPrice") or stops[0].get("stopPrice") or 0)
            if trig:
                cr = abs(trig - entry) / entry * 100
    cr = max(0.1, min(10.0, round(cr or 2.0, 1)))
    exit_side = "sell" if is_long else "buy"
    try:
        so = ex.create_order(sym, "TRAILING_STOP_MARKET", exit_side, qty, None, {"callbackRate": cr, "reduceOnly": True})
        noid = so.get("id")
    except Exception as e:
        die("trailing stop placement FAILED (old stop kept, position still protected): %r" % e)
    ncxl = cancel_all_stops(ex, sym, keep=noid)
    live = any(o.get("id") == noid for o in open_stops(ex, sym))
    ot = load_open()
    if sym in ot:
        ot[sym]["stop_type"] = "trailing %.1f%%" % cr; save_open(ot)
    journal_line("[%s] RETRAIL %s -> trailing %.1f%% (cancelled %d fixed stop(s))" % (_ts(), b, cr, ncxl))
    out({"retrailed": sym, "callback_rate": cr, "new_oid": noid, "live": live, "old_stops_cancelled": ncxl})


def cmd_set_tp(args):
    """Attach a reduce-only LIMIT take-profit to an existing position at --tp (replaces any prior TP;
    leaves the STOP untouched). TP must be on the profit side of the mark (long: above; short: below) so
    gains get captured automatically the instant price hits it — no reliance on the 10-min loop or the trail."""
    ex = account_client(args.live)
    b = args.symbol.upper().replace("/USDT:USDT", "").replace("USDT", "")
    sym = b + INST_SUFFIX
    p = get_position(ex, sym)
    if not p:
        die("no open position on %s" % sym)
    qty = abs(float(p["contracts"])); is_long = p.get("side") == "long"
    mark = float(p.get("markPrice") or p.get("entryPrice") or 0)
    tp_px = float(ex.price_to_precision(sym, float(args.tp)))
    if (is_long and tp_px <= mark) or (not is_long and tp_px >= mark):
        die("TP %.8g on the wrong side of mark %.8g for a %s (would fill immediately)" % (tp_px, mark, p.get("side")))
    exit_side = "sell" if is_long else "buy"
    ncxl = 0                                  # cancel any existing reduce-only LIMIT (TP); never touch the STOP
    try:
        for o in ex.fetch_open_orders(sym):
            if (o.get("type") or "").upper() == "LIMIT" and (o.get("reduceOnly") in (True, None)):
                try:
                    ex.cancel_order(o.get("id"), sym); ncxl += 1
                except Exception:
                    pass
    except Exception:
        pass
    try:
        to = ex.create_order(sym, "LIMIT", exit_side, qty, tp_px, {"reduceOnly": True})
        tp_oid = to.get("id")
    except Exception as e:
        die("TP placement FAILED: %r" % e)
    live = any(o.get("id") == tp_oid for o in ex.fetch_open_orders(sym))
    ot = load_open()
    if sym in ot:
        ot[sym]["tp"] = tp_px; save_open(ot)
    journal_line("[%s] SET-TP %s %s -> %g (reduce-only limit; replaced %d prior)" % (_ts(), b, p.get("side"), tp_px, ncxl))
    out({"set_tp": sym, "tp": tp_px, "oid": tp_oid, "live": live, "replaced": ncxl})


def cmd_log(args):
    if not os.path.exists(DECISIONS):
        out({"log": []}); return
    lines = open(DECISIONS).read().strip().splitlines()
    out({"log": [json.loads(x) for x in lines[-args.n:]]})


def cmd_note(args):
    """Append a SHORT reflection (1 sentence, hard-capped at 280 chars) to the journal. Keep it terse — your
    durable rules belong in PLAYBOOK.md, not here. Long narrative is unnecessary and wastes tokens."""
    txt = " ".join((args.text or "").split())[:280]
    journal_line("[%s] NOTE: %s" % (_ts(), txt))
    out({"noted": txt})


def cmd_journal(args):
    """COMPACT decision input (token-lean by design): aggregate stats + open trades + a LIST of the last
    `--days` (default 7) closed trades, each with a 1-sentence why-won/lost. NOT verbose narrative — your
    durable RULES live in PLAYBOOK.md; read that for reasoning. Run at the START of each loop before deciding."""
    try:
        reconcile(account_client(args.live))      # book any stop-outs first so stats are current
    except Exception:
        pass
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)

    def _recent(ts):
        try:
            return dt.datetime.strptime(ts, "%Y-%m-%dT%H:%MZ").replace(tzinfo=dt.timezone.utc) >= cutoff
        except Exception:
            return True

    trades = []
    if os.path.exists(CLOSED_TR):
        for l in open(CLOSED_TR):
            if not l.strip():
                continue
            t = json.loads(l)
            ts = t.get("ts") or t.get("entry_ts") or ""
            if not _recent(ts):
                continue
            pnl = t.get("pnl") or 0
            why = t.get("exit_reason") or ""
            if (not why) or why.lower() in ("stop/external", "presumed-stop", "stop", "external"):
                why = t.get("entry_reason") or why    # generic exit -> show the setup thesis instead
            trades.append({"t": ts, "sym": t.get("symbol", "").split("/")[0], "side": t.get("side"),
                           "R": t.get("R"), "pnl": rnd(pnl),
                           "result": "WON" if pnl > 0 else ("LOST" if pnl < 0 else "FLAT"),
                           "why": " ".join(why.split())[:150]})
    out({"stats": journal_stats(), "open_trades": load_open(), "recent_trades": trades, "window_days": args.days,
         "note": "recent_trades = your last %dd closed trades (result + 1-line why each). Your RULES are in PLAYBOOK.md — read that. "
                 "Be terse: do NOT write long notes/summaries; record only a 1-sentence why per close + concise playbook edits." % args.days})


def cmd_review(args):
    """Net-of-fee performance review for self-improvement: pairs the recorded closed trades (which are GROSS,
    pre-fee) with the exchange's ACTUAL realized PnL + commission, so the agent learns from NET outcomes — the
    only number that matters. Read-only (no orders). Use this in the post-trade retrospective to update PLAYBOOK.md."""
    ex = account_client(args.live)
    start = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)).timestamp() * 1000)
    try:
        inc = ex.fapiPrivateGetIncome({"startTime": start, "limit": 1000})
    except Exception:
        inc = []

    def _b(s):
        if "/" in s:                 # ccxt form e.g. DOGE/USDT:USDT -> DOGE
            return s.split("/")[0]
        return s[:-4] if s.endswith("USDT") else s   # exchange id DOGEUSDT -> DOGE

    by_sym = {}
    tg = tf = 0.0
    for e in inc:
        s = _b(e.get("symbol") or "")
        ty = e.get("incomeType")
        v = float(e.get("income") or 0)
        d = by_sym.setdefault(s, {"gross": 0.0, "fees": 0.0})
        if ty == "REALIZED_PNL":
            d["gross"] += v
            tg += v
        elif ty in ("COMMISSION", "FUNDING_FEE"):
            d["fees"] += v
            tf += v
    closed = []
    loop_syms = set()
    if os.path.exists(CLOSED_TR):
        for l in open(CLOSED_TR):
            if l.strip():
                t = json.loads(l)
                b = _b(t.get("symbol", "")); loop_syms.add(b)
                closed.append({"symbol": b, "side": t.get("side"),
                               "ts": t.get("ts") or t.get("entry_ts"), "recorded_gross_pnl": t.get("pnl"),
                               "recorded_R": t.get("R"), "thesis": (t.get("entry_reason") or "")[:160]})
    for s in load_open().keys():
        loop_syms.add(_b(s))
    persym = {s: {"gross": rnd(d["gross"]), "fees": rnd(d["fees"]), "net": rnd(d["gross"] + d["fees"]), "loop": s in loop_syms}
              for s, d in sorted(by_sym.items())}
    out({"days": args.days,
         "net_summary": {"gross": rnd(tg), "fees": rnd(tf), "net": rnd(tg + tf)},
         "by_symbol_net": persym, "closed_trades": closed,
         "note": "LEARN FROM closed_trades (YOUR trades: thesis + recorded_R, which is GROSS/pre-fee) — that is your primary source. "
                 "net_summary = account-level NET incl fees = the truth. by_symbol_net: loop:true marks a symbol you traded, but its "
                 "net AGGREGATES ALL account activity on that symbol in the window (other bots/manual on shared symbols inflate it) — treat as rough only. "
                 "To net a specific trade: recorded_R MINUS ~0.2R fees (round-trip taker at current size). Judge what works by NET, never gross."})


def cmd_execute_decisions(args):
    """DETERMINISTIC executor — the AI brain emits a decisions JSON (which coin, side, stop, tp, grade);
    THIS replays those decisions across ALL accounts in `--accounts`, programmatically: sizes by each
    account's risk_pct, gates entries by each account's min_grade (so a 'live' mirror can take only the
    A-grade subset of the master's trades), and reuses the existing bracketed `enter`/`close` (full
    static+trailing+TP, never naked, per-account ISOLATION via LOOP_TRADER_DATADIR). No AI tokens here —
    mirroring N accounts is a free Python loop. Archives the decisions file after, so it can't re-execute stale."""
    import subprocess
    GRADE = {"A": 3, "B": 2, "C": 1}

    def _b(s):
        if "/" in s:
            return s.split("/")[0]
        return s[:-4] if s.endswith("USDT") else s

    try:
        accts = json.load(open(args.accounts)).get("accounts", [])
    except Exception as e:
        die("cannot read accounts config %s: %r" % (args.accounts, e))
    dec = {}
    if os.path.exists(args.decisions):
        try:
            dec = json.load(open(args.decisions))
        except Exception:
            dec = {}
    entries = dec.get("entries") or []
    manage = dec.get("manage") or []
    # spike-direction guard: an entry's side MUST match the prepared spike candle's direction (never long a red spike)
    spike_side = {}
    try:
        _spk = json.load(open(os.path.join(os.path.dirname(os.path.abspath(args.decisions)), "spikes.json")))
        for _c in _spk.get("coins", []):
            spike_side[_b(str(_c.get("coin", "")))] = _c.get("side")
    except Exception:
        spike_side = {}
    self_path = os.path.abspath(__file__)
    report = []
    for a in accts:
        if not a.get("enabled", True):
            continue
        env = dict(os.environ)
        env["LOOP_TRADER_DATADIR"] = a["datadir"]
        live = bool(a.get("live"))
        if live:
            env["LOOP_TRADER_ALLOW_LIVE"] = "1"
        pre = [sys.executable, self_path] + (["--live"] if live else [])

        def run(extra, to=120):
            try:
                r = subprocess.run(pre + extra, env=env, capture_output=True, text=True, timeout=to)
                return r.returncode == 0, (r.stdout or r.stderr or "")[-200:]
            except Exception as ex:
                return False, repr(ex)[-200:]

        run(["state"], to=90)                      # triggers reconcile + orphan-sweep on this account
        ot = {}
        otp = os.path.join(a["datadir"], "open_trades.json")
        if os.path.exists(otp):
            try:
                ot = json.load(open(otp))
            except Exception:
                ot = {}
        held = {_b(k) for k in ot.keys()}
        acts = []
        for m in manage:
            if m.get("action") == "close":
                sym = _b(m.get("symbol", ""))
                if sym and sym in held:
                    ok, o = run(["close", "--symbol", sym, "--reason", (m.get("reason") or "brain close")[:160]], to=90)
                    acts.append({"close": sym, "ok": ok})
        gmin = GRADE.get(str(a.get("min_grade", "B")).upper(), 2)
        max_pos = a.get("max_positions")                 # optional concurrency cap; None/absent = UNLIMITED
        for e in entries:
            sym = _b(e.get("symbol", ""))
            g = GRADE.get(str(e.get("grade") or "B").upper(), 2)
            side = e.get("side")
            stop = e.get("stop")
            tp = e.get("tp")
            if not sym or side not in ("long", "short") or stop in (None, "", 0):
                continue
            want = spike_side.get(sym)
            if want and side != want:
                acts.append({"skip": sym, "why": "side %s contradicts spike direction %s -- refusing to trade against the spike candle" % (side, want)})
                continue
            if g < gmin:
                acts.append({"skip": sym, "why": "grade %s < min %s" % (e.get("grade"), a.get("min_grade"))})
                continue
            if sym in held:                              # de-dup: never stack a 2nd position on a coin (incl. same-run dup)
                acts.append({"skip": sym, "why": "already held / dup"})
                continue
            if max_pos is not None and len(held) >= int(max_pos):   # cap only if explicitly configured (default: unlimited)
                acts.append({"skip": sym, "why": "max %s concurrent positions" % max_pos})
                continue
            cmd = ["enter", "--symbol", sym, "--side", side, "--stop", str(stop),
                   "--risk-pct", str(a.get("risk_pct", 1.0)), "--leverage", str(a.get("leverage", 5)),
                   "--reason", (e.get("reason") or "brain entry")[:160]]
            if tp not in (None, "", 0):
                cmd += ["--tp", str(tp)]
            tpct = e.get("trail_pct") or e.get("trailing_pct")
            if tpct not in (None, "", 0):
                cmd += ["--trail-pct", str(tpct)]
            ok, o = run(cmd)
            if ok:
                held.add(sym)                            # count it + block any duplicate later in this run
            acts.append({"enter": sym, "side": side, "grade": e.get("grade"), "ok": ok, "out": o})
        report.append({"account": a.get("name"), "live": live, "actions": acts})
    if os.path.exists(args.decisions):                 # archive so a stale file can't re-execute next run
        try:
            os.replace(args.decisions, args.decisions + ".last")
        except Exception:
            pass
    out({"executed": report, "entries_considered": len(entries), "manage_considered": len(manage)})


def main():
    load_env()
    ap = argparse.ArgumentParser(description="loop_trader — Binance testnet trading infra for the /loop agent")
    ap.add_argument("--live", action="store_true", help="use LIVE account (gated by LOOP_TRADER_ALLOW_LIVE=1)")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("scan"); s.add_argument("--tf", default="1h"); s.add_argument("--top", type=int, default=30)
    s.add_argument("--symbols", default=""); s.add_argument("--lookback", type=int, default=250); s.add_argument("--sort", default="", choices=["", "vol"]); s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("gainers"); s.add_argument("--tf", default="1h"); s.add_argument("--top", type=int, default=20)
    s.add_argument("--lookback", type=int, default=250); s.add_argument("--min-vol", dest="min_vol", type=float, default=1e7)
    s.add_argument("--losers", action="store_true"); s.set_defaults(fn=cmd_gainers)

    s = sub.add_parser("volspike"); s.add_argument("--tf", default="15m"); s.add_argument("--top", type=int, default=15)
    s.add_argument("--spike-tf", dest="spike_tf", default="15m"); s.add_argument("--spike-lookback", dest="spike_lookback", type=int, default=150)
    s.add_argument("--min-spike", dest="min_spike", type=float, default=10.0)   # 10x is the viable band on 15m (backtest)
    s.add_argument("--avg-bars", dest="avg_bars", type=int, default=20)        # baseline = avg of last 20 15m candles (~5h)
    s.add_argument("--lookback", type=int, default=250); s.add_argument("--min-vol", dest="min_vol", type=float, default=5e6)
    s.add_argument("--confirm-frac", dest="confirm_frac", type=float, default=0.8, help="next candle must hold >= this fraction of the spike-candle volume to confirm (0=off)")
    s.add_argument("--max-scan", dest="max_scan", type=int, default=400); s.set_defaults(fn=cmd_volspike)

    s = sub.add_parser("price"); s.add_argument("symbol"); s.add_argument("--tf", default="1h"); s.add_argument("--n", type=int, default=60); s.set_defaults(fn=cmd_price)

    s = sub.add_parser("state"); s.set_defaults(fn=cmd_state)

    s = sub.add_parser("enter")
    s.add_argument("--symbol", required=True); s.add_argument("--side", required=True)
    s.add_argument("--stop", required=True); s.add_argument("--tp", default=None)
    s.add_argument("--risk-pct", type=float, default=1.0); s.add_argument("--leverage", type=int, default=5)
    s.add_argument("--reason", default=""); s.add_argument("--add", action="store_true")
    s.add_argument("--trail-pct", dest="trail_pct", type=float, default=None, help="trailing-stop callbackRate %% (0.1-10); default derives from the stop distance")
    s.add_argument("--fixed-stop", action="store_true", help="static stop ONLY — skip the trailing stop (default places BOTH a static STOP_MARKET + a native TRAILING_STOP_MARKET)")
    s.add_argument("--dry-run", action="store_true"); s.set_defaults(fn=cmd_enter)

    s = sub.add_parser("close"); s.add_argument("--symbol", required=True); s.add_argument("--reason", default="")
    s.add_argument("--dry-run", action="store_true"); s.set_defaults(fn=cmd_close)

    s = sub.add_parser("cancel-stops"); s.add_argument("--symbol", required=True); s.set_defaults(fn=cmd_cancel_stops)

    s = sub.add_parser("retrail"); s.add_argument("--symbol", required=True); s.add_argument("--pct", type=float, default=None); s.set_defaults(fn=cmd_retrail)

    s = sub.add_parser("set-tp"); s.add_argument("--symbol", required=True); s.add_argument("--tp", required=True); s.set_defaults(fn=cmd_set_tp)

    s = sub.add_parser("log"); s.add_argument("--n", type=int, default=20); s.set_defaults(fn=cmd_log)

    s = sub.add_parser("note"); s.add_argument("text"); s.set_defaults(fn=cmd_note)

    s = sub.add_parser("journal"); s.add_argument("--n", type=int, default=40); s.add_argument("--days", type=int, default=7); s.set_defaults(fn=cmd_journal)
    s = sub.add_parser("review"); s.add_argument("--days", type=int, default=7); s.set_defaults(fn=cmd_review)
    s = sub.add_parser("execute-decisions"); s.add_argument("--decisions", required=True); s.add_argument("--accounts", required=True); s.set_defaults(fn=cmd_execute_decisions)

    args = ap.parse_args()
    if not args.cmd:
        print(__doc__); sys.exit(0)
    args.fn(args)


if __name__ == "__main__":
    main()
