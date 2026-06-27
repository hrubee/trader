#!/usr/bin/env python3
"""spike_ft.py — DETERMINISTIC forward-test bot (Binance DEMO/testnet, paper money).

Locked config (from the optimization lab, with operator's 1m-low stop):
  ENTRY:  15m vol >= 7x the 20-bar avg -> spike candle up (LONG-ONLY) -> next 15m closes above spike close
          -> enter market long at the confirmation close (~now).
  STOP:   low of the just-closed 1m candle at entry (operator override; static STOP_MARKET, never naked).
  RISK:   1% of demo wallet per trade.
  EXIT:   take 70% at +2.5R (resting reduce-only limit) + trail the remaining 30% at 5R (cron-managed
          STOP_MARKET, cancel/replace as the high-water mark rises; static 1m-low stop is the floor).
Isolated from the AI brain: own datadir/state/log. Signals from Binance mainnet, fills on the demo venue.
Run from cron every 5 min:  manage open positions, then scan for new signals.
"""
import sys, os, json
sys.path.insert(0, "/root/trader")
import loop_trader as lt

SPIKE = 7.0; AVG = 20; TP_R = 2.5; PARTIAL = 0.7; TRAIL_R = 5.0
RISK_PCT = 1.0; LEV = 20; LONG_ONLY = False   # BOTH directions: green spike -> long, red spike -> short
MIN_VOL = 5e6; MAX_SCAN = 60
FRESH_MIN = 5     # only ENTER within this many minutes of the confirmation 15m close (no chasing stale signals)
DATADIR = "/root/trader/loop_trader_data_spikeft"
STATE = DATADIR + "/positions.json"
LOG = DATADIR + "/ft.log"


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
    tmp = STATE + ".tmp"                     # atomic write: never leave a half-written (corrupt) state file
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, STATE)


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
    now = mkt.milliseconds()
    try:
        tick = mkt.fetch_tickers()
    except Exception as e:
        log("ticker fetch failed: %r" % e); return sigs
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
        qv = float(t.get("quoteVolume") or 0)
        if qv >= MIN_VOL:
            cand.append((qv, b))
    cand.sort(reverse=True)
    for b in [x[1] for x in cand[:MAX_SCAN]]:
        try:
            k = lt.klines(mkt, b, "15m", 80)     # klines requires >=60 bars or returns None
            if not k:
                continue
            t, o, h, l, c, v = k
            if len(c) < AVG + 3:
                continue
            sp = len(c) - 3; cf = len(c) - 2          # spike, confirmation (both closed); -1 is forming
            avg = sum(v[sp - AVG:sp]) / AVG
            if avg <= 0 or v[sp] < SPIKE * avg:
                continue
            up = c[sp] >= o[sp]
            if LONG_ONLY and not up:
                continue
            if up and not c[cf] > c[sp]:
                continue
            if (not up) and not c[cf] < c[sp]:
                continue
            entry_ts = int(t[cf]) + 900_000            # confirmation 15m bar CLOSE time (ms)
            if now - entry_ts > FRESH_MIN * 60_000:     # only act right after the close; don't chase stale signals
                continue
            k1 = lt.klines(mkt, b, "1m", 60)          # klines requires >=60 bars or returns None
            if not k1:
                continue
            t1, _, h1, l1, _, _ = k1
            stop = None                                 # the 1m candle that CLOSED at the confirmation close
            for ii in range(len(t1) - 1, -1, -1):
                if int(t1[ii]) == entry_ts - 60_000:
                    stop = (l1[ii] if up else h1[ii]); break
            if stop is None:
                stop = l1[-2] if up else h1[-2]         # fallback: latest just-closed 1m
            ref = c[cf]
            rd = (ref - stop) if up else (stop - ref)
            if rd <= 0:
                continue
            sigs.append({"base": b, "sym": b + lt.INST_SUFFIX, "up": bool(up), "ref": float(ref),
                         "stop": float(stop), "vr": float(round(v[sp] / avg, 1))})   # native types (no numpy -> JSON ok)
        except Exception as e:
            log("scan %s err %r" % (b, e))
    return sigs


def enter(ex, sig, st):
    sym = sig["sym"]; up = sig["up"]
    try:
        bal = ex.fetch_balance(); wallet = float(bal.get("USDT", {}).get("total") or 0)
        if wallet <= 0:
            log("wallet 0, skip %s" % sym); return
        px = float(ex.fetch_ticker(sym)["last"])
        stop0 = sig["stop"]
        rd = (px - stop0) if up else (stop0 - px)
        if rd <= 0:
            log("%s stop on wrong side now, skip" % sym); return
        qty = cap_qty(ex, sym, (RISK_PCT / 100.0 * wallet) / rd)
        if qty <= 0:
            log("%s qty 0, skip" % sym); return
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
        rd = (fill - stop0) if up else (stop0 - fill)
        if rd <= 0:
            ex.create_order(sym, "market", exit_side, qty, None, {"reduceOnly": True})
            log("%s post-fill stop wrong side -> closed" % sym); return
        sp = float(ex.price_to_precision(sym, stop0))
        # MANDATORY static stop (never naked)
        try:
            so = ex.create_order(sym, "STOP_MARKET", exit_side, qty, None, {"stopPrice": sp, "reduceOnly": True})
            soid = so.get("id")
        except Exception as e:
            ex.create_order(sym, "market", exit_side, qty, None, {"reduceOnly": True})
            log("%s STOP failed -> closed (no naked): %r" % (sym, e)); return
        # partial TP (70%) reduce-only limit at 2.5R
        tp = float(ex.price_to_precision(sym, fill + TP_R * rd if up else fill - TP_R * rd))
        tpq = cap_qty(ex, sym, qty * PARTIAL)
        tpoid = None
        try:
            to = ex.create_order(sym, "LIMIT", exit_side, tpq, tp, {"reduceOnly": True})
            tpoid = to.get("id")
        except Exception as e:
            log("%s TP place failed (continuing): %r" % (sym, e))
        st[sig["base"]] = {"sym": sym, "up": up, "entry": fill, "stop0": sp, "rd": rd, "qty": qty,
                           "hwm": fill, "stop_level": sp, "stop_oid": soid, "tp_oid": tpoid,
                           "partial_done": False, "vr": sig["vr"], "ts": lt._ts()}
        save(st)
        log("ENTER %s long @%g qty=%g stop=%g tp=%g(70%%) rd=%.4g vr=%sx" % (
            sig["base"], fill, qty, sp, tp, rd, sig["vr"]))
    except Exception as e:
        log("enter %s failed: %r" % (sym, e))


def manage(ex, st):
    for base in list(st.keys()):
        p = st[base]; sym = p["sym"]; up = p["up"]
        try:
            pos = lt.get_position(ex, sym)
            if not pos or abs(float(pos.get("contracts") or 0)) <= 0:
                lt.cancel_all_stops(ex, sym)
                try:
                    for oo in ex.fetch_open_orders(sym):
                        ex.cancel_order(oo["id"], sym)
                except Exception:
                    pass
                log("CLOSED %s (exited via stop/TP)" % base)
                del st[base]; save(st); continue
            qty = abs(float(pos.get("contracts")))
            mark = float(pos.get("markPrice") or ex.fetch_ticker(sym)["last"])
            p["hwm"] = max(p["hwm"], mark) if up else min(p["hwm"], mark)
            rd = p["rd"]
            # detect partial fill (qty dropped) -> mark partial_done
            if not p["partial_done"] and qty < p["qty"] * 0.95:
                p["partial_done"] = True; log("PARTIAL TP filled %s (qty now %g)" % (base, qty))
            # trailing stop: eff = max(stop0, hwm - 5R)
            eff = max(p["stop0"], p["hwm"] - TRAIL_R * rd) if up else min(p["stop0"], p["hwm"] + TRAIL_R * rd)
            eff = float(ex.price_to_precision(sym, eff))
            moved = (eff > p["stop_level"] + 1e-12) if up else (eff < p["stop_level"] - 1e-12)
            if moved:
                exit_side = "sell" if up else "buy"
                try:
                    lt.cancel_all_stops(ex, sym)
                    so = ex.create_order(sym, "STOP_MARKET", exit_side, qty, None, {"stopPrice": eff, "reduceOnly": True})
                    p["stop_oid"] = so.get("id"); p["stop_level"] = eff
                    log("TRAIL %s stop -> %g (hwm %g)" % (base, eff, p["hwm"]))
                except Exception as e:
                    log("trail %s failed (old stop kept): %r" % (base, e))
            p["qty"] = qty; st[base] = p; save(st)
        except Exception as e:
            log("manage %s err %r" % (base, e))


def main():
    os.makedirs(DATADIR, exist_ok=True)
    # Use the DEDICATED 2nd demo account for this forward-test (isolates it from the AI brain's demo wallet).
    # Swap the DEMO_* keys this process reads to the DEMO2_* pair, staying on the testnet venue/URLs.
    if os.environ.get("BINANCE_DEMO2_API_KEY") and os.environ.get("BINANCE_DEMO2_API_SECRET"):
        os.environ["BINANCE_DEMO_API_KEY"] = os.environ["BINANCE_DEMO2_API_KEY"]
        os.environ["BINANCE_DEMO_API_SECRET"] = os.environ["BINANCE_DEMO2_API_SECRET"]
    mkt = lt.market_client()
    ex = lt.account_client(False)      # demo/testnet (now authenticated as the 2nd demo account)
    st = load()
    manage(ex, st)
    held = set(st.keys())
    for sig in detect(mkt, held):
        if sig["base"] in st:
            continue
        enter(ex, sig, st)
        st = load()


if __name__ == "__main__":
    main()
