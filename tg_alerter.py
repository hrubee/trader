#!/usr/bin/env python3
"""tg_alerter.py — posts trade alerts to a Telegram group, decoupled from the trade engine.

Reads the per-account ledger (decisions.jsonl for ENTERs, closed_trades.jsonl for exits) and, for each NEW
event since the last run:
  ENTRY -> a setup chart (15m candles + entry/stop/TP lines) with a caption, posted to the group.
  EXIT  -> a result message (exit, R, PnL, reason) posted as a REPLY to that coin's entry message (threaded).

State (file offsets + {coin: entry_message_id}) is persisted so it only alerts on new events and can thread
exits under the right entry. First run PRIMES to current end-of-ledger (no backfill spam). Never raises into
the caller — failures are logged to stderr and swallowed.

Env (from <repo>/.env): TG_ALERT_TOKEN, TG_ALERT_CHAT. Datadir via TG_ALERT_DATADIR (default loop_trader_data).
Run:  ./.venv/bin/python3 tg_alerter.py        (normal)   |   tg_alerter.py --test   (send one sample pair)
"""
import os, sys, json, re

REPO = os.path.dirname(os.path.abspath(__file__))


def load_env():
    try:
        for ln in open(os.path.join(REPO, ".env")):
            ln = ln.strip()
            if ln and "=" in ln and not ln.startswith("#"):
                k, v = ln.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        pass


load_env()
TOKEN = os.environ.get("TG_ALERT_TOKEN", "")
CHAT = os.environ.get("TG_ALERT_CHAT", "")
DATADIR = os.environ.get("TG_ALERT_DATADIR") or os.path.join(REPO, "loop_trader_data")
DEC = os.path.join(DATADIR, "decisions.jsonl")
CLOSED = os.path.join(DATADIR, "closed_trades.jsonl")
STATE = os.path.join(DATADIR, "tg_alert_state.json")
API = "https://api.telegram.org/bot%s" % TOKEN


def log(*a):
    print("[tg_alerter]", *a, file=sys.stderr)


def fmt(x):
    try:
        x = float(x)
        return ("%.8g" % x)
    except Exception:
        return str(x)


def base(sym):
    return (sym or "").split("/")[0]


# ── telegram ──────────────────────────────────────────────────────────────────
import requests


def send_text(text, reply_to=None):
    data = {"chat_id": CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_to:
        data["reply_to_message_id"] = reply_to
    try:
        j = requests.post(API + "/sendMessage", data=data, timeout=20).json()
        if not j.get("ok"):
            log("sendMessage failed:", j.get("description"))
        return j.get("result", {}).get("message_id")
    except Exception as e:
        log("sendMessage error:", repr(e)[:160]); return None


def send_photo(png, caption, reply_to=None):
    data = {"chat_id": CHAT, "caption": caption, "parse_mode": "HTML"}
    if reply_to:
        data["reply_to_message_id"] = reply_to
    try:
        with open(png, "rb") as f:
            j = requests.post(API + "/sendPhoto", data=data, files={"photo": f}, timeout=40).json()
        if not j.get("ok"):
            log("sendPhoto failed:", j.get("description")); return None
        return j.get("result", {}).get("message_id")
    except Exception as e:
        log("sendPhoto error:", repr(e)[:160]); return None


# ── polished render (style of b2_render_test.png) + volume panel + entry/exit markers ────────
UP, DN = "#1aa179", "#e2574c"          # teal up / red down candles


def render(coin, side, entry, stop, tp, tf="15m", exit_px=None, R=None, pnl=None, n=36):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
        import ccxt, numpy as np
        ex = ccxt.binanceusdm({"options": {"defaultType": "future"}, "enableRateLimit": True})
        ex.load_markets()
        o = ex.fetch_ohlcv(coin + "/USDT:USDT", tf, limit=n)
        if not o or len(o) < 5:
            return None
        a = np.array(o, float)
        O, H, L, C, V = a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5]
        m = len(C)
        entry = float(entry) if entry not in (None, "", 0) else None
        stop = float(stop) if stop not in (None, "", 0) else None
        tp = float(tp) if tp not in (None, "", 0) else None
        is_long = side == "long"

        fig = plt.figure(figsize=(11, 7), dpi=110)
        gs = fig.add_gridspec(2, 1, height_ratios=[4, 1], hspace=0.05)
        axp = fig.add_subplot(gs[0]); axv = fig.add_subplot(gs[1], sharex=axp)
        fig.patch.set_facecolor("white")
        for ax in (axp, axv):
            ax.set_facecolor("white"); ax.grid(alpha=0.12, zorder=0)
            for sp in ("top", "right"):
                ax.spines[sp].set_visible(False)

        # candles
        for i in range(m):
            col = UP if C[i] >= O[i] else DN
            axp.plot([i, i], [L[i], H[i]], color=col, linewidth=1.0, zorder=2)
            lo = min(O[i], C[i]); ht = abs(C[i] - O[i]) or (H[i] - L[i]) * 0.002
            axp.add_patch(Rectangle((i - 0.32, lo), 0.64, ht, color=col, zorder=3))
            axv.bar(i, V[i], width=0.64, color=col, alpha=0.85, zorder=2)

        # reward / risk shaded zones + dashed level lines with right-edge labels
        xr = m - 0.3
        def line(level, color, label):
            if level is None:
                return
            axp.axhline(level, color=color, linestyle="--", linewidth=1.4, zorder=4)
            axp.text(m - 0.2, level, "  " + label, color=color, va="center", ha="left",
                     fontsize=10, fontweight="bold", clip_on=False)
        if entry and stop:
            axp.axhspan(min(entry, stop), max(entry, stop), color=DN, alpha=0.07, zorder=1)   # risk zone
        if entry and tp:
            axp.axhspan(min(entry, tp), max(entry, tp), color=UP, alpha=0.07, zorder=1)        # reward zone
        rr = None
        if entry and stop and tp:
            risk = abs(entry - stop)
            rr = (abs(tp - entry) / risk) if risk else None
        line(tp, "#2e7d32", "TP %s%s" % (fmt(tp), ("  +%dR" % round(rr)) if rr else ""))
        line(entry, "#1565c0", "ENTRY %s" % fmt(entry))
        line(stop, "#c62828", "SL %s  -1R" % fmt(stop))

        # highlight the setup (last 3 closed candles = spike + confirmation)
        axp.axvspan(m - 3.5, m - 0.5, color="#1aa179", alpha=0.10, zorder=0)
        axv.axvspan(m - 3.5, m - 0.5, color="#1aa179", alpha=0.10, zorder=0)

        # exit marker (for the result image)
        title_extra = ""
        if exit_px not in (None, "", 0):
            exf = float(exit_px)
            axp.axhline(exf, color="#6a1b9a", linestyle=":", linewidth=1.6, zorder=5)
            axp.scatter([m - 1], [exf], marker="X", s=120, color="#6a1b9a", zorder=6)
            axp.text(m - 0.2, exf, "  EXIT %s" % fmt(exf), color="#6a1b9a", va="center",
                     ha="left", fontsize=10, fontweight="bold", clip_on=False)
            if R is not None and pnl is not None:
                title_extra = "   →  %+.2fR / $%+.2f" % (float(R), float(pnl))

        arrow = "▲ LONG" if is_long else "▼ SHORT"
        tcol = UP if is_long else DN
        axp.set_title("volspike   %s   %s/USDT   ·   %s%s" % (arrow, coin, tf, title_extra),
                      fontsize=13, fontweight="bold", color=tcol, loc="left", pad=10)
        axp.set_ylabel("price", fontsize=9); axv.set_ylabel("vol", fontsize=8)
        axp.margins(x=0.04); axp.set_xticks([]); axv.set_xticks([])
        axv.tick_params(labelsize=7); axp.tick_params(labelsize=8)
        plt.subplots_adjust(right=0.85, left=0.07, top=0.92, bottom=0.04)
        path = "/tmp/tg_%s.png" % re.sub(r"[^A-Za-z0-9]", "", coin)
        fig.savefig(path, facecolor="white"); plt.close(fig)
        return path
    except Exception as e:
        log("render error for %s:" % coin, repr(e)[:200]); return None


def alert_entry(rec):
    coin = base(rec.get("symbol")); side = (rec.get("side") or "").lower()
    entry, stop, tp = rec.get("price"), rec.get("stop"), rec.get("tp")
    arrow = "⬆️ <b>LONG</b>" if side == "long" else "⬇️ <b>SHORT</b>"
    reason = (rec.get("reason") or "").strip()
    cap = ("%s  <b>%s</b>\nentry <code>%s</code> · stop <code>%s</code> · TP <code>%s</code>%s" % (
        arrow, coin, fmt(entry), fmt(stop), fmt(tp), ("\n<i>%s</i>" % reason[:280]) if reason else ""))
    png = render(coin, side, entry, stop, tp)
    mid = send_photo(png, cap) if png else send_text(cap)
    log("ENTRY %s -> msg_id %s" % (coin, mid))
    return coin, mid


def alert_exit(rec, reply_to, entry_hint=None):
    coin = base(rec.get("symbol")); side = (rec.get("side") or "").lower()
    R = rec.get("R") or 0; pnl = rec.get("pnl") or 0
    emoji = "✅" if pnl > 0 else ("❌" if pnl < 0 else "➖")
    entry = rec.get("entry") or entry_hint
    exitpx = rec.get("exit")
    why = (rec.get("exit_reason") or "").strip()
    xline = ("exit <code>%s</code> · " % fmt(exitpx)) if exitpx not in (None, "", 0) else ""
    txt = ("%s <b>Closed %s</b> %s\n%s<b>%+.2fR / $%+.2f</b>%s" % (
        emoji, coin, side, xline, float(R), float(pnl), ("\n<i>%s</i>" % why[:200]) if why else ""))
    png = render(coin, side, entry, rec.get("stop"), rec.get("tp"), exit_px=exitpx, R=R, pnl=pnl)
    if png:
        mid = send_photo(png, txt, reply_to=reply_to)
        if not mid:
            send_text(txt, reply_to=reply_to)
    else:
        send_text(txt, reply_to=reply_to)
    log("EXIT %s -> reply_to %s" % (coin, reply_to))


# ── state + main ──────────────────────────────────────────────────────────────
def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_state(s):
    tmp = STATE + ".tmp"
    json.dump(s, open(tmp, "w")); os.replace(tmp, STATE)


def count_lines(p):
    try:
        return sum(1 for _ in open(p))
    except Exception:
        return 0


def read_from(p, off):
    try:
        return [json.loads(l) for l in open(p).read().splitlines()[off:] if l.strip()]
    except Exception:
        return []


def main():
    if not TOKEN or not CHAT:
        log("missing TG_ALERT_TOKEN/TG_ALERT_CHAT — nothing to do"); return
    if "--test" in sys.argv:
        rows = read_from(CLOSED, 0)
        if rows:
            r = rows[-1]
            coin, mid = alert_entry({"symbol": r.get("symbol"), "side": r.get("side"), "price": r.get("entry"),
                                     "stop": None, "tp": None, "reason": "(TEST) " + (r.get("entry_reason") or "")})
            alert_exit(r, mid)
            log("test pair sent")
        else:
            send_text("✅ tg_alerter test — no closed trades yet, but the channel works.")
        return

    s = load_state()
    if not s:  # PRIME: skip history, only alert on new events from now on
        s = {"dec_off": count_lines(DEC), "closed_off": count_lines(CLOSED), "msgids": {}}
        save_state(s); log("primed (no backfill): dec_off=%s closed_off=%s" % (s["dec_off"], s["closed_off"]))
        return

    msgids = s.get("msgids", {})
    # ENTRIES
    new_dec = read_from(DEC, s.get("dec_off", 0))
    for rec in new_dec:
        if rec.get("action") == "ENTER":
            coin, mid = alert_entry(rec)
            if mid:
                msgids[coin] = mid
    s["dec_off"] = count_lines(DEC)
    # EXITS
    new_closed = read_from(CLOSED, s.get("closed_off", 0))
    for rec in new_closed:
        coin = base(rec.get("symbol"))
        alert_exit(rec, msgids.get(coin))
        msgids.pop(coin, None)
    s["closed_off"] = count_lines(CLOSED)
    s["msgids"] = msgids
    save_state(s)


if __name__ == "__main__":
    main()
