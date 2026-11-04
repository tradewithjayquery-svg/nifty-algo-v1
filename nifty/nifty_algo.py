#!/usr/bin/env python3
"""
nifty - Nifty 5 min EMA 9/21 crossover, option buying PAPER trade (no real orders).

Usage:  python nifty_algo.py 0   -> LIVE (paper) : follows market window 09:20-15:15
        python nifty_algo.py 1   -> TEST         : runs right now, any time, checks
                                                   everything and sends a TEST report
                                                   to Telegram. No state / CSV saved.
"""
import os
import sys
import json
import csv
import time
import signal
import datetime as dt
from zoneinfo import ZoneInfo
from urllib.parse import quote

import requests

# ====================== SETTINGS (yahan edit karo) ======================
FAST_EMA = 9
SLOW_EMA = 21
TARGET_DELTA = 0.7        # CE ke liye +0.7, PE ke liye -0.7 ke sabse paas wala strike
SL_CANDLES = 2            # SL = pichhli 2 candle (5 min) ka low (CE) / high (PE), spot par
SL_INCLUDE_ENTRY_CANDLE = False   # False = entry (crossover) candle se PEHLE ki 2 candle
                                  # True  = entry candle + uski pichhli 1 candle
RR = 2.0                  # target = risk ka 2 guna (1:2), spot par
WINDOW_START = "09:20"    # is time ke baad shuru hue candle ka signal liya jayega
WINDOW_END = "15:15"      # sab position force close
LAST_CANDLE_START = "15:10"   # ye candle 15:15 par close hota hai (last entry)
MAX_TRADES = 2            # poore din mein total (CE + PE)
LOTS = 1
DEFAULT_LOT_SIZE = 65     # sirf tab use hoga jab Upstox lot size na de
POLL_SECONDS = 15         # position khuli ho to spot kitne second mein check hoga
# ========================================================================

IST = ZoneInfo("Asia/Kolkata")
BASE = "https://api.upstox.com"
SPOT_KEY = "NSE_INDEX|Nifty 50"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
CSV_FILE = os.path.join(HERE, "trades.csv")

TOKEN = os.environ.get("UPSTOX_TOKEN", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

CSV_COLS = ["date", "trade_no", "side", "entry_time", "spot_entry", "strike",
            "expiry", "delta", "option_entry", "qty", "exit_time", "spot_exit",
            "option_exit", "exit_reason", "pnl"]


# ------------------------------ helpers ------------------------------
def now():
    return dt.datetime.now(IST)


def hm(s):
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def at(d, s):
    return dt.datetime.combine(d, hm(s), tzinfo=IST)


def log(*a):
    print(now().strftime("%H:%M:%S"), *a, flush=True)


def tg(msg):
    log("TG>", msg.replace("\n", " | ")[:200])
    if not TG_TOKEN or not TG_CHAT:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": msg}, timeout=20)
        return r.status_code == 200
    except Exception as e:
        log("telegram error", type(e).__name__)
        return False


def api(path, params=None, tries=3):
    last = None
    for _ in range(tries):
        try:
            r = requests.get(
                BASE + path, params=params, timeout=20,
                headers={"Authorization": "Bearer " + TOKEN,
                         "Accept": "application/json"})
            if r.status_code == 200:
                return r.json().get("data")
            last = f"HTTP {r.status_code}: {r.text[:150]}"
            if r.status_code in (401, 403):
                break
        except Exception as e:
            last = type(e).__name__
        time.sleep(2)
    raise RuntimeError(f"{path} -> {last}")


# ------------------------------ market data ------------------------------
def parse_candles(rows):
    out = []
    for r in rows or []:
        ts = dt.datetime.fromisoformat(r[0]).astimezone(IST)
        out.append({"ts": ts, "o": float(r[1]), "h": float(r[2]),
                    "l": float(r[3]), "c": float(r[4])})
    return out


def get_candles(days=10):
    """Nifty spot 5 min candles (sirf close ho chuke), purane se naye."""
    d = {}
    today = now().date()
    k = quote(SPOT_KEY, safe="")
    try:
        data = api(f"/v3/historical-candle/{k}/minutes/5/{today}/"
                   f"{today - dt.timedelta(days=days)}")
        for c in parse_candles((data or {}).get("candles")):
            d[c["ts"]] = c
    except Exception as e:
        log("historical warn:", e)
    try:
        data = api(f"/v3/historical-candle/intraday/{k}/minutes/5")
        for c in parse_candles((data or {}).get("candles")):
            d[c["ts"]] = c
    except Exception as e:
        log("intraday warn:", e)
    n = now() + dt.timedelta(seconds=1)
    return [d[x] for x in sorted(d) if x + dt.timedelta(minutes=5) <= n]


def ema_series(values, n):
    k = 2.0 / (n + 1)
    out = []
    for i, v in enumerate(values):
        out.append(v if i == 0 else v * k + out[-1] * (1 - k))
    return out


def cross_at(e_fast, e_slow, i):
    """'CE' = upar cross, 'PE' = niche cross, None = koi cross nahi."""
    if i < 1:
        return None
    if e_fast[i - 1] <= e_slow[i - 1] and e_fast[i] > e_slow[i]:
        return "CE"
    if e_fast[i - 1] >= e_slow[i - 1] and e_fast[i] < e_slow[i]:
        return "PE"
    return None


def latest_signal(candles):
    closes = [c["c"] for c in candles]
    if len(closes) < SLOW_EMA + 2:
        return None
    ef = ema_series(closes, FAST_EMA)
    es = ema_series(closes, SLOW_EMA)
    return cross_at(ef, es, len(closes) - 1)


def ltp(key):
    data = api("/v2/market-quote/ltp", {"instrument_key": key})
    if not data:
        raise RuntimeError("ltp empty")
    for v in data.values():
        if v.get("instrument_token") == key:
            return float(v["last_price"])
    return float(list(data.values())[0]["last_price"])


def contract_info():
    data = api("/v2/option/contract", {"instrument_key": SPOT_KEY})
    today = str(now().date())
    exps = sorted({str(c.get("expiry"))[:10] for c in data or []})
    exps = [e for e in exps if e >= today]
    if not exps:
        raise RuntimeError("expiry nahi mili")
    exp = exps[0]  # current week (aaj expiry ho to aaj ki)
    lot = DEFAULT_LOT_SIZE
    for c in data:
        if str(c.get("expiry"))[:10] == exp and c.get("lot_size"):
            lot = int(c["lot_size"])
            break
    return {"expiry": exp, "lot": lot}


def pick_strike(side, expiry):
    rows = api("/v2/option/chain",
               {"instrument_key": SPOT_KEY, "expiry_date": expiry})
    best = None
    for r in rows or []:
        o = r.get("call_options" if side == "CE" else "put_options") or {}
        g = o.get("option_greeks") or {}
        m = o.get("market_data") or {}
        d, p = g.get("delta"), m.get("ltp")
        if d is None or not p or not o.get("instrument_key"):
            continue
        diff = abs(abs(float(d)) - TARGET_DELTA)
        if best is None or diff < best["diff"]:
            best = {"diff": diff, "strike": r.get("strike_price"),
                    "key": o["instrument_key"], "delta": float(d),
                    "ltp": float(p)}
    return best


# ------------------------------ state / csv ------------------------------
def fresh_state(day):
    return {"date": str(day), "trades": 0, "last_candle": "", "pos": None,
            "done": [], "holiday_checked": False}


def load_state(day):
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
        if st.get("date") == str(day):
            return st
    except Exception:
        pass
    return fresh_state(day)


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=1)


def append_csv(row):
    new = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        if new:
            w.writeheader()
        w.writerow(row)


def levels(side, entry, candles, k):
    """candles[k] = entry candle. SL pichhli SL_CANDLES candle ke low (CE) /
    high (PE) par, target = RR x risk. Sab spot par. Valid na ho to None."""
    end = k + 1 if SL_INCLUDE_ENTRY_CANDLE else k
    prior = candles[max(0, end - SL_CANDLES):end]
    if not prior:
        return None
    if side == "CE":
        sl = min(c["l"] for c in prior)
        risk = entry - sl
        if risk <= 0:
            return None
        return sl, entry + RR * risk
    sl = max(c["h"] for c in prior)
    risk = sl - entry
    if risk <= 0:
        return None
    return sl, entry - RR * risk


# ------------------------------ live trading ------------------------------
def enter(st, side, cs, ts):
    spot = cs[-1]["c"]
    lv = levels(side, spot, cs, len(cs) - 1)
    if not lv:
        tg(f"⚠️ nifty: {side} signal aaya par SL valid nahi (spot SL ke "
           f"galat side par). Trade skip.")
        return
    sl, tgt = lv
    info = contract_info()
    pick = pick_strike(side, info["expiry"])
    if not pick:
        tg(f"⚠️ nifty: {side} signal aaya par {TARGET_DELTA} delta wala strike "
           f"nahi mila (option chain data). Trade skip.")
        return
    st["trades"] += 1
    st["pos"] = {
        "no": st["trades"], "side": side, "strike": pick["strike"],
        "key": pick["key"], "expiry": info["expiry"], "delta": pick["delta"],
        "qty": info["lot"] * LOTS, "spot_entry": spot, "sl": sl, "tgt": tgt,
        "opt_entry": pick["ltp"], "entry_time": now().strftime("%H:%M:%S"),
    }
    save_state(st)
    p = st["pos"]
    tg(f"🟢 NIFTY ENTRY #{p['no']} — {side} BUY\n"
       f"Candle: {ts.strftime('%H:%M')} close, EMA {FAST_EMA}/{SLOW_EMA} "
       f"{'upar' if side == 'CE' else 'niche'} cross\n"
       f"Spot: {spot:.2f}\n"
       f"Strike: {p['strike']} {side} (delta {p['delta']:.2f}), "
       f"expiry {p['expiry']}\n"
       f"Premium entry: {p['opt_entry']:.2f} | Qty: {p['qty']}\n"
       f"SL spot: {sl:.2f} ({abs(spot - sl):.1f} pts) | "
       f"Target spot: {tgt:.2f} ({abs(tgt - spot):.1f} pts) | RR 1:{RR:g}")


def exit_pos(st, reason):
    p = st["pos"]
    spot = ltp(SPOT_KEY)
    opt = ltp(p["key"])
    pnl = (opt - p["opt_entry"]) * p["qty"]
    row = {
        "date": st["date"], "trade_no": p["no"], "side": p["side"],
        "entry_time": p["entry_time"], "spot_entry": round(p["spot_entry"], 2),
        "strike": p["strike"], "expiry": p["expiry"],
        "delta": round(p["delta"], 3), "option_entry": round(p["opt_entry"], 2),
        "qty": p["qty"], "exit_time": now().strftime("%H:%M:%S"),
        "spot_exit": round(spot, 2), "option_exit": round(opt, 2),
        "exit_reason": reason, "pnl": round(pnl, 2),
    }
    append_csv(row)
    st["done"].append(row)
    st["pos"] = None
    save_state(st)
    icon = "✅" if pnl >= 0 else "🔴"
    tg(f"{icon} NIFTY EXIT #{p['no']} — {p['side']} ({reason})\n"
       f"Strike: {p['strike']} | Spot: {p['spot_entry']:.2f} → {spot:.2f}\n"
       f"Premium: {p['opt_entry']:.2f} → {opt:.2f} | Qty: {p['qty']}\n"
       f"P&L: Rs {pnl:,.2f}")


def day_summary(st):
    done = st["done"]
    total = sum(r["pnl"] for r in done)
    wins = sum(1 for r in done if r["pnl"] > 0)
    tg(f"📊 NIFTY DAY SUMMARY {st['date']}\n"
       f"Trades: {len(done)} | Win: {wins} | Loss: {len(done) - wins}\n"
       f"Total P&L: Rs {total:,.2f}")


def run_live():
    t = now()
    today = t.date()
    if today.weekday() >= 5:
        tg("nifty: aaj weekend hai, kuch nahi chalega.")
        return
    st = load_state(today)
    end = at(today, WINDOW_END)
    if t >= end and not st["pos"]:
        tg("nifty: 15:15 ho chuka hai, aaj ka window khatam. Kal subah 9:20 "
           "se pehle run karo.")
        return

    stop = {"flag": False}

    def on_term(*_):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    tg(f"🚀 NIFTY LIVE (paper) shuru\n"
       f"EMA {FAST_EMA}/{SLOW_EMA} | 5 min | delta {TARGET_DELTA} | "
       f"SL pichhli {SL_CANDLES} candle H/L / Target 1:{RR:g} (spot)\n"
       f"Aaj ke trades ab tak: {st['trades']}/{MAX_TRADES}"
       + (" | position khuli hai" if st["pos"] else ""))

    errors = 0
    try:
        while not stop["flag"]:
            n = now()
            if n >= end:
                if st["pos"]:
                    exit_pos(st, "TIME 15:15")
                break
            try:
                # holiday check (ek baar, 9:40 ke baad)
                if not st["holiday_checked"] and n >= at(today, "09:40"):
                    cs = get_candles(2)
                    if not any(c["ts"].date() == today for c in cs):
                        tg("nifty: aaj market band lagta hai (candle nahi "
                           "aaye). Band kar raha hoon.")
                        return
                    st["holiday_checked"] = True
                    save_state(st)

                if st["pos"]:
                    p = st["pos"]
                    spot = ltp(SPOT_KEY)
                    if p["side"] == "CE":
                        hit_sl, hit_t = spot <= p["sl"], spot >= p["tgt"]
                    else:
                        hit_sl, hit_t = spot >= p["sl"], spot <= p["tgt"]
                    if hit_sl:
                        exit_pos(st, "SL")
                    elif hit_t:
                        exit_pos(st, "TARGET")
                    errors = 0
                    time.sleep(POLL_SECONDS)
                    # position ke dauran candle boundary bhi mark hoti rahe
                    # (neeche wala hissa chalega)

                boundary = n.replace(minute=n.minute // 5 * 5, second=0,
                                     microsecond=0)
                last_start = boundary - dt.timedelta(minutes=5)
                key = last_start.isoformat()
                age = (n - boundary).total_seconds()

                if key > st["last_candle"] and age >= 4:
                    tradable = (
                        not st["pos"] and st["trades"] < MAX_TRADES
                        and at(today, WINDOW_START) <= last_start
                        <= at(today, LAST_CANDLE_START))
                    if age > 90 or not tradable:
                        st["last_candle"] = key
                        save_state(st)
                    else:
                        cs = get_candles()
                        if not cs or cs[-1]["ts"] < last_start:
                            if age < 60:
                                time.sleep(3)
                                continue
                            st["last_candle"] = key
                            save_state(st)
                        else:
                            st["last_candle"] = key
                            save_state(st)
                            sig = latest_signal(cs)
                            log("candle", last_start.strftime("%H:%M"),
                                "signal:", sig)
                            if sig:
                                enter(st, sig, cs, last_start)
                errors = 0
            except Exception as e:
                errors += 1
                log("loop error:", e)
                if errors == 10:
                    tg(f"⚠️ nifty: baar baar error aa raha hai: {str(e)[:150]}")
            if not st["pos"]:
                nxt = (n.replace(minute=n.minute // 5 * 5, second=4,
                                 microsecond=0) + dt.timedelta(minutes=5))
                time.sleep(max(1, min(20, (nxt - now()).total_seconds())))
    finally:
        save_state(st)
        if stop["flag"]:
            tg("⛔ nifty run beech mein ruk gaya"
               + (" — POSITION KHULI HAI, jaldi dobara run karo (mode 0)."
                  if st["pos"] else ". Dobara run karo (mode 0)."))
            return
    day_summary(st)


# ------------------------------ test mode ------------------------------
def simulate_day(candles, day):
    """Spot par replay: cross -> entry at close, SL/target high-low se."""
    closes = [c["c"] for c in candles]
    ef = ema_series(closes, FAST_EMA)
    es = ema_series(closes, SLOW_EMA)
    idx = [i for i, c in enumerate(candles) if c["ts"].date() == day]
    results = []
    i = 0
    while i < len(idx) and len(results) < MAX_TRADES:
        k = idx[i]
        c = candles[k]
        ok = (at(day, WINDOW_START) <= c["ts"] <= at(day, LAST_CANDLE_START))
        side = cross_at(ef, es, k) if ok else None
        if not side:
            i += 1
            continue
        entry = c["c"]
        lv = levels(side, entry, candles, k)
        if not lv:
            i += 1
            continue
        sl, tgt = lv
        exit_px, reason, j = None, None, i + 1
        while j < len(idx):
            d = candles[idx[j]]
            if side == "CE":
                if d["l"] <= sl:
                    exit_px, reason = sl, "SL"
                elif d["h"] >= tgt:
                    exit_px, reason = tgt, "TARGET"
            else:
                if d["h"] >= sl:
                    exit_px, reason = sl, "SL"
                elif d["l"] <= tgt:
                    exit_px, reason = tgt, "TARGET"
            if reason:
                break
            if d["ts"] >= at(day, LAST_CANDLE_START):
                exit_px, reason = d["c"], "TIME"
                break
            j += 1
        if reason is None:
            last = candles[idx[-1]]
            exit_px, reason, j = last["c"], "TIME(open)", len(idx) - 1
        pts = (exit_px - entry) if side == "CE" else (entry - exit_px)
        results.append({"side": side, "t": c["ts"].strftime("%H:%M"),
                        "entry": entry, "exit": exit_px, "why": reason,
                        "pts": pts})
        i = max(j, i + 1)
    return results


def run_test():
    L = ["🧪 NIFTY TEST (mode 1) — koi trade/CSV save nahi hoga",
         f"Time: {now().strftime('%d-%b %H:%M')}"]
    L.append(f"Settings: EMA {FAST_EMA}/{SLOW_EMA}, delta {TARGET_DELTA}, "
             f"SL pichhli {SL_CANDLES} candle H/L, target 1:{RR:g}, "
             f"max {MAX_TRADES} trades")

    try:
        L.append(f"✅ Upstox token OK | Nifty spot: {ltp(SPOT_KEY):.2f}")
    except Exception as e:
        L.append(f"❌ Upstox token/spot error: {str(e)[:120]}")

    try:
        info = contract_info()
        L.append(f"✅ Expiry: {info['expiry']} | Lot size: {info['lot']}")
        for side in ("CE", "PE"):
            p = pick_strike(side, info["expiry"])
            if p:
                L.append(f"✅ {side}: strike {p['strike']}, delta "
                         f"{p['delta']:.2f}, premium {p['ltp']:.2f}")
            else:
                L.append(f"⚠️ {side}: delta/premium data nahi mila "
                         f"(market band ho to normal hai)")
    except Exception as e:
        L.append(f"❌ Option data error: {str(e)[:120]}")

    try:
        cs = get_candles(10)
        if len(cs) < SLOW_EMA + 2:
            L.append(f"⚠️ Candles kam mile: {len(cs)}")
        else:
            day = cs[-1]["ts"].date()
            L.append(f"✅ Candles OK ({len(cs)}), replay din: {day}")
            res = simulate_day(cs, day)
            if not res:
                L.append("Replay: us din koi cross nahi (window 9:20-15:15)")
            for r in res:
                L.append(f"• {r['t']} {r['side']} entry {r['entry']:.1f} → "
                         f"{r['exit']:.1f} ({r['why']}) = {r['pts']:+.1f} pts")
            if res:
                L.append(f"Replay total (spot pts): "
                         f"{sum(r['pts'] for r in res):+.1f}")
    except Exception as e:
        L.append(f"❌ Candle error: {str(e)[:120]}")

    sent = tg("\n".join(L))
    print("\n".join(L))
    print("Telegram:", "bhej diya" if sent else "NAHI gaya (token/chat id check karo)")


if __name__ == "__main__":
    mode = (sys.argv[1] if len(sys.argv) > 1 else "0").strip()
    if mode == "1":
        run_test()
    else:
        run_live()
