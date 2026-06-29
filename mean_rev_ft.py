#!/usr/bin/env python3
"""mean_rev_ft.py — DETERMINISTIC mean-reversion forward-test bot (Binance DEMO2/testnet, paper).

Mirrors backtest/mean_reversion_lab.py BEST config (arm-then-confirm z-score MR), intended timeframe 4h:
  ARM (long):  z<=-K below the SMA AND RSI<=RSI_LO, only when ADX<ADX_MAX (ranging).  Mirror for short.
  CONFIRM:     enter only after a reversal bar (green for long / red for short) within CONFIRM_WIN bars.
  ENTRY:       market, at signal.   STOP: STOP_ATR x ATR (resting, mandatory, never naked).
  EXIT:        price reverts and overshoots the mean by EXIT_Z sigma (bot-managed market close), OR the
               ATR stop, OR MAX_HOLD bars (time stop).  Both directions.  RISK_PCT of equity per trade.
⚠️ HONEST: on a 2yr multi-regime sample this entry does NOT beat a random-entry control (drift/structure
   harvester, not validated alpha). This is a PAPER forward-test to observe, not a proven edge.
Isolated from the AI brain: own DEMO2 account (key-swap), own datadir/state/lock. Signals = Binance
mainnet; fills = testnet. Run from cron.
"""
import sys, os, json, time
import numpy as np
import pandas as pd
sys.path.insert(0, "/root/trader")
import loop_trader as lt

TF_HOURS = 4

TF = "4h"; SMA_N = 20; K = 3.0; RSI_LO = 30; RSI_HI = 75; ADX_MAX = 18
STOP_ATR = 3.0; MAX_HOLD = 16; EXIT_Z = 0.5; CONFIRM_WIN = 2; DIRECTION = "both"
RISK_PCT = 2.0; LEV = 10; MIN_VOL = 5e6; MAX_SCAN = 60; BARS = 160
DATADIR = "/root/trader/loop_trader_data_meanrev"
STATE = DATADIR + "/positions.json"; LOG = DATADIR + "/mr.log"; LOCK = "/tmp/mean_rev_ft.botlock"


def log(m):
    line = "%s %s" % (lt._ts(), m)
    with open(LOG, "a") as f:
        f.write(line + "\n")
    print(line)


def load():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save(s):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, STATE)


def indicators(ohlcv):
    df = pd.DataFrame(ohlcv, columns=["t", "o", "h", "l", "c", "v"]).astype(float)
    c, h, l = df["c"], df["h"], df["l"]
    sma = c.rolling(SMA_N).mean(); sd = c.rolling(SMA_N).std(ddof=0)
    z = (c - sma) / sd
    d = c.diff(); g = d.clip(lower=0).rolling(14).mean(); ls = (-d.clip(upper=0)).rolling(14).mean()
    rsi = 100 - 100 / (1 + g / ls.replace(0, np.nan))
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    up = h.diff(); dn = -l.diff()
    pdm = ((up > dn) & (up > 0)) * up; mdm = ((dn > up) & (dn > 0)) * dn
    atr14 = tr.ewm(alpha=1 / 14, adjust=False).mean()
    pdi = 100 * pdm.ewm(alpha=1 / 14, adjust=False).mean() / atr14
    mdi = 100 * mdm.ewm(alpha=1 / 14, adjust=False).mean() / atr14
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / 14, adjust=False).mean()
    return (df["o"].values, df["c"].values, sma.values, z.values, rsi.values, atr.values, adx.values)


def fetch(mkt, base):
    try:
        o = mkt.fetch_ohlcv(base + lt.INST_SUFFIX, timeframe=TF, limit=BARS)
        return o if o and len(o) >= SMA_N + 30 else None
    except Exception:
        return None


def cap_qty(ex, sym, qty):
    try:
        m = ex.market(sym) or {}
        caps = []
        lim = (((m.get("limits") or {}).get("amount") or {}).get("max"))
        if lim:
            caps.append(float(lim))
        for fl in ((m.get("info") or {}).get("filters") or []):
            if fl.get("filterType") in ("MARKET_LOT_SIZE", "LOT_SIZE"):
                mq = fl.get("maxQty")
                if mq and float(mq) > 0:
                    caps.append(float(mq))
        if caps:
            qty = min(qty, min(caps) * 0.97)
    except Exception:
        pass
    return float(ex.amount_to_precision(sym, qty))


def detect(mkt, held):
    sigs = []
    try:
        tick = mkt.fetch_tickers()
    except Exception as e:
        log("ticker fetch failed %r" % e); return sigs
    cand = []
    for sym, t in tick.items():
        if not sym.endswith(lt.INST_SUFFIX):
            continue
        m = mkt.markets.get(sym) or {}
        if (m.get("info", {}) or {}).get("underlyingType") not in (None, "COIN"):
            continue
        b = sym.split("/")[0]
        if not b.isascii() or b in held:
            continue
        if float(t.get("quoteVolume") or 0) >= MIN_VOL:
            cand.append((float(t["quoteVolume"]), b))
    cand.sort(reverse=True)
    for b in [x[1] for x in cand[:MAX_SCAN]]:
        k = fetch(mkt, b)
        if not k:
            continue
        o, c, sma, z, rsi, atr, adx = indicators(k)
        cb = len(c) - 2                                   # last CLOSED bar (-1 is forming)
        if cb < SMA_N + 2 or np.isnan(z[cb]) or np.isnan(adx[cb]) or atr[cb] <= 0:
            continue
        for up in (True, False):
            if DIRECTION == "long" and not up:
                continue
            if DIRECTION == "short" and up:
                continue
            armed = False                                 # an arm-extreme within the confirm window before cb
            for j in range(cb - CONFIRM_WIN, cb):
                if j < 0 or np.isnan(z[j]) or np.isnan(rsi[j]) or np.isnan(adx[j]):
                    continue
                if adx[j] >= ADX_MAX:
                    continue
                if up and z[j] <= -K and rsi[j] <= RSI_LO:
                    armed = True; break
                if (not up) and z[j] >= K and rsi[j] >= RSI_HI:
                    armed = True; break
            if not armed:
                continue
            confirm = (c[cb] > o[cb]) if up else (c[cb] < o[cb])   # reversal bar
            still_below = (z[cb] < EXIT_Z) if up else (z[cb] > -EXIT_Z)
            if confirm and still_below:
                sigs.append({"base": b, "sym": b + lt.INST_SUFFIX, "up": bool(up),
                             "stop_atr_px": float(STOP_ATR * atr[cb]), "z": float(round(z[cb], 2))})
                break
    return sigs


def cur_z(mkt, base):
    k = fetch(mkt, base)
    if not k:
        return None
    o, c, sma, z, rsi, atr, adx = indicators(k)
    cb = len(c) - 2
    return float(z[cb]) if not np.isnan(z[cb]) else None


def enter(ex, sig, st):
    sym = sig["sym"]; up = sig["up"]
    try:
        bal = ex.fetch_balance(); wallet = float(bal.get("USDT", {}).get("total") or 0)
        if wallet <= 0:
            log("wallet 0 skip %s" % sym); return
        px = float(ex.fetch_ticker(sym)["last"])
        stopd = sig["stop_atr_px"]
        stop0 = px - stopd if up else px + stopd
        if stopd <= 0:
            return
        qty = cap_qty(ex, sym, (RISK_PCT / 100.0 * wallet) / stopd)
        if qty <= 0:
            log("%s qty 0 skip" % sym); return
        try:
            ex.set_margin_mode("isolated", sym)
        except Exception:
            pass
        try:
            ex.set_leverage(LEV, sym)
        except Exception:
            pass
        side = "buy" if up else "sell"; exit_side = "sell" if up else "buy"
        o = ex.create_order(sym, "market", side, qty)
        fill = float(o.get("average") or o.get("price") or px)
        stop0 = fill - stopd if up else fill + stopd
        sp = float(ex.price_to_precision(sym, stop0))
        try:
            so = ex.create_order(sym, "STOP_MARKET", exit_side, qty, None, {"stopPrice": sp, "reduceOnly": True})
            soid = so.get("id")
        except Exception as e:
            ex.create_order(sym, "market", exit_side, qty, None, {"reduceOnly": True})
            log("%s STOP failed -> closed (no naked) %r" % (sym, e)); return
        st[sig["base"]] = {"sym": sym, "up": bool(up), "entry": float(fill), "stop": sp,
                           "rd": float(stopd), "qty": float(qty), "stop_oid": soid,
                           "z0": sig["z"], "ts": lt._ts(), "ts_ms": int(time.time() * 1000)}
        save(st)
        log("ENTER %s %s @%g qty=%g stop=%g (z=%s, risk %.0f%%)" % (
            sig["base"], "long" if up else "short", fill, qty, sp, sig["z"], RISK_PCT))
    except Exception as e:
        log("enter %s failed %r" % (sym, e))


def manage(ex, mkt, st):
    for base in list(st.keys()):
        p = st[base]; sym = p["sym"]; up = p["up"]
        try:
            pos = lt.get_position(ex, sym)
            if not pos or abs(float(pos.get("contracts") or 0)) <= 0:
                lt.cancel_all_stops(ex, sym)
                log("CLOSED %s (stop hit / external)" % base)
                del st[base]; save(st); continue
            z = cur_z(mkt, base)
            reverted = (z is not None and ((z >= EXIT_Z) if up else (z <= -EXIT_Z)))
            elapsed_h = (time.time() * 1000 - p.get("ts_ms", time.time() * 1000)) / 3_600_000.0
            timeout = elapsed_h >= MAX_HOLD * TF_HOURS  # 16 bars x 4h = 64h time-stop
            if reverted or timeout:
                qty = abs(float(pos.get("contracts")))
                ex.create_order(sym, "market", "sell" if up else "buy", qty, None, {"reduceOnly": True})
                lt.cancel_all_stops(ex, sym)
                log("EXIT %s (%s) z=%s" % (base, "reverted" if reverted else "timeout", z))
                del st[base]; save(st); continue
            st[base] = p; save(st)
        except Exception as e:
            log("manage %s err %r" % (base, e))


def main():
    os.makedirs(DATADIR, exist_ok=True)
    import fcntl
    lk = open(LOCK, "w")
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    if os.environ.get("BINANCE_DEMO2_API_KEY") and os.environ.get("BINANCE_DEMO2_API_SECRET"):
        os.environ["BINANCE_DEMO_API_KEY"] = os.environ["BINANCE_DEMO2_API_KEY"]
        os.environ["BINANCE_DEMO_API_SECRET"] = os.environ["BINANCE_DEMO2_API_SECRET"]
    mkt = lt.market_client()
    ex = lt.account_client(False)
    st = load()
    manage(ex, mkt, st)
    st = load()
    held = set(st.keys())
    try:
        for p in ex.fetch_positions():
            if abs(float(p.get("contracts") or 0)) > 0:
                held.add(p["symbol"].split("/")[0])
    except Exception:
        pass
    for sig in detect(mkt, held):
        if sig["base"] in held:
            continue
        enter(ex, sig, st)
        st = load(); held.add(sig["base"])


if __name__ == "__main__":
    main()
