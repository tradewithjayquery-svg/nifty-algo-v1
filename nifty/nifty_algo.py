#!/usr/bin/env python3
"""
nifty - Nifty 5 min EMA 9/21 crossover, option buying PAPER trade (no real orders).

Usage:  python nifty_algo.py 0   -> LIVE (paper) : follows market window 09:20-15:15
        python nifty_algo.py 1   -> TEST         : runs right now, any time, checks
                                                   everything and sends a TEST report
                                                   to Telegram. No state / CSV saved.

Rules:
- ENTRY: 5 min candle close par crossover (upar = CE buy, niche = PE buy).
- SL = entry candle se pehle ki candle ka low (CE) / high (PE)  [spot par].
- 1:1 par 1 lot book, bacha lot cost-to-cost (SL = entry spot), target 1:2 (spot par).
- EXIT (SL / 1:1 / target): spot price touch hote hi, candle close ka wait nahi.
  Us waqt option ke live price par market-order jaisa paper exit.
- SL / target se pehle ulta crossover aaye to chalti trade band + nayi trade.
- Live price: Upstox websocket (tick by tick). Na chale to REST (har ~2 sec).
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
SL_CANDLES = 1            # SL = pichhli itni candle (5 min) ka low (CE) / high (PE), spot par
SL_INCLUDE_ENTRY_CANDLE = False   # False = entry (crossover) candle se PEHLE ki candle
                                  # True  = entry candle bhi gino
R1 = 1.0                  # is RR par pehla lot book (1:1), spot par
RR = 2.0                  # bache lot ka target (1:2), spot par
LOTS = 2                  # kul lot (kam se kam 2 rakhna)
BOOK_LOTS = 1             # R1 par kitne lot book honge; baaki cost-to-cost (SL = entry)
WINDOW_START = "09:20"    # is time ke baad shuru hue candle ka signal liya jayega
WINDOW_END = "15:15"      # sab position force close
LAST_CANDLE_START = "15:10"   # ye candle 15:15 par close hota hai (last entry)
MAX_TRADES = 2            # poore din mein total (CE + PE)
DEFAULT_LOT_SIZE = 65     # sirf tab use hoga jab Upstox lot size na de
TICK_SECONDS = 0.2        # position khuli ho to trigger kitni jaldi check hoga
STALE_SECONDS = 5         # websocket tick itna purana ho to REST se price lo
REST_FALLBACK_SECONDS = 2 # REST fallback mein kitne second mein ek baar price
PNL_UPDATE_MINUTES = 5    # position khuli ho to har itne minute mein Telegram PnL
# ========================================================================

IST = ZoneInfo("Asia/Kolkata")
BASE = "https://api.upstox.com"
SPOT_KEY = "NSE_INDEX|Nifty 50"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
CSV_FILE = os.path.join(HERE, "trades.csv")
PNL_LOG_FILE = os.path.join(HERE, "pnl_log.csv")

TOKEN = os.environ.get("UPSTOX_TOKEN", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

CSV_COLS = ["date", "trade_no", "side", "entry_time", "spot_entry", "sl_spot",
            "target_spot", "strike", "expiry", "delta", "option_entry", "lots",
            "qty_per_lot",
            "leg1_lots", "leg1_time", "leg1_spot", "leg1_option", "leg1_reason",
            "leg1_pnl",
            "leg2_lots", "leg2_time", "leg2_spot", "leg2_option", "leg2_reason",
            "leg2_pnl",
            "exit_time", "total_pnl"]

PNL_COLS = ["date", "time", "trade_no", "side", "strike", "spot", "option_cmp",
            "option_entry", "pts_diff", "lots_open", "qty_open", "live_pnl",
            "booked_pnl", "total_pnl"]

FEED = None   # run_live / run_test mein set hota hai


# ------------------------------ helpers ------------------------------
def now():
    return dt.datetime.now(IST)


def hm(s):
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def at(d, s):
    return dt.datetime.combine(d, hm(s), tzinfo=IST)


def bnd(t):
    """t ka pichhla 5 min boundary."""
    return t.replace(minute=t.minute // 5 * 5, second=0, microsecond=0)


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


def ltp_multi(keys):
    """Ek hi REST call mein kai instrument ka price: {key: price}."""
    data = api("/v2/market-quote/ltp", {"instrument_key": ",".join(keys)})
    out = {}
    for v in (data or {}).values():
        tok = v.get("instrument_token")
        if tok and v.get("last_price") is not None:
            out[tok] = float(v["last_price"])
    return out


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


# ------------------------------ live price feed ------------------------------
class Feed:
    """Live price. Pehle Upstox websocket (tick by tick). Tick na aaye ya
    websocket na chale to REST se (har REST_FALLBACK_SECONDS mein ek baar)."""

    def __init__(self):
        self.px = {}            # key -> (price, epoch)
        self.keys = set()
        self.streamer = None
        self.ws_ok = False      # kabhi websocket tick aaya?
        self.last_rest = 0.0

    # ---- websocket ----
    def _find(self, obj):
        """Message ke andar se ltpc.ltp dhundo (ltpc / full dono mode chalenge)."""
        if isinstance(obj, dict):
            lt = obj.get("ltpc")
            if isinstance(lt, dict) and lt.get("ltp") is not None:
                try:
                    v = float(lt["ltp"])
                    if v > 0:
                        return v
                except Exception:
                    pass
            for v in obj.values():
                r = self._find(v)
                if r is not None:
                    return r
        return None

    def _on_message(self, msg):
        try:
            if isinstance(msg, (str, bytes)):
                msg = json.loads(msg)
            feeds = (msg or {}).get("feeds") or {}
            t = time.time()
            for key, val in feeds.items():
                p = self._find(val)
                if p:
                    self.px[key] = (p, t)
                    self.ws_ok = True
        except Exception as e:
            log("ws message error:", type(e).__name__)

    def _on_open(self, *a):
        try:
            self.streamer.subscribe(sorted(self.keys), "ltpc")
        except Exception as e:
            log("ws subscribe error:", str(e)[:100])

    def start(self, keys):
        self.keys = set(keys)
        try:
            import upstox_client
        except Exception:
            log("upstox SDK install nahi -> REST fallback")
            return False
        try:
            cfg = upstox_client.Configuration()
            cfg.access_token = TOKEN
            s = upstox_client.MarketDataStreamerV3(upstox_client.ApiClient(cfg))
            try:
                s.auto_reconnect(True, 5, 200)
            except Exception:
                pass
            s.on("open", self._on_open)
            s.on("message", self._on_message)
            s.on("error", lambda *a: log("ws error:", str(a)[:120]))
            s.on("close", lambda *a: log("ws close"))
            self.streamer = s
            s.connect()
            return True
        except Exception as e:
            log("websocket start fail:", str(e)[:120])
            return False

    def subscribe(self, key):
        self.keys.add(key)
        if self.streamer:
            try:
                self.streamer.subscribe([key], "ltpc")
            except Exception as e:
                log("ws subscribe error:", str(e)[:100])

    def unsubscribe(self, key):
        self.keys.discard(key)
        if self.streamer:
            try:
                self.streamer.unsubscribe([key])
            except Exception:
                pass

    def stop(self):
        if self.streamer:
            try:
                self.streamer.disconnect()
            except Exception:
                pass

    # ---- price ----
    def price(self, key, urgent=False):
        """Taaza price. Websocket tick taaza ho to wahi. Warna REST (throttled);
        urgent=True (exit ke waqt) mein throttle nahi."""
        t = time.time()
        v = self.px.get(key)
        if v and t - v[1] <= STALE_SECONDS:
            return v[0]
        if urgent or t - self.last_rest >= REST_FALLBACK_SECONDS:
            self.last_rest = t
            try:
                got = ltp_multi(sorted(self.keys | {key}))
                for k, p in got.items():
                    self.px[k] = (p, t)
            except Exception as e:
                log("rest price error:", str(e)[:100])
        v = self.px.get(key)
        return v[0] if v else None


# ------------------------------ state / csv ------------------------------
def fresh_state(day):
    return {"date": str(day), "trades": 0, "last_candle": "", "pos": None,
            "done": [], "holiday_checked": False, "last_pnl_slot": "",
            "last_min_slot": ""}


def load_state(day):
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
        if st.get("date") == str(day):
            st.setdefault("last_pnl_slot", "")
            st.setdefault("last_min_slot", "")
            return st
    except Exception:
        pass
    return fresh_state(day)


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=1)


def append_csv(row):
    # purani format ki file ho to side mein rakh do, naya header banega
    if os.path.exists(CSV_FILE):
        try:
            with open(CSV_FILE) as f:
                head = f.readline().strip()
        except Exception:
            head = ""
        if head != ",".join(CSV_COLS):
            os.replace(CSV_FILE, CSV_FILE.replace(
                ".csv", f"_old_{int(time.time())}.csv"))
    new = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        if new:
            w.writeheader()
        w.writerow(row)


def append_pnl_log(row):
    new = not os.path.exists(PNL_LOG_FILE)
    with open(PNL_LOG_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PNL_COLS)
        if new:
            w.writeheader()
        w.writerow(row)


def levels(side, entry, candles, k):
    """candles[k] = entry candle. SL = pichhli SL_CANDLES candle ka low (CE) /
    high (PE). R1 = 1:1 level, tgt = 1:RR level. Sab spot par.
    Valid na ho to None."""
    end = k + 1 if SL_INCLUDE_ENTRY_CANDLE else k
    prior = candles[max(0, end - SL_CANDLES):end]
    if not prior:
        return None
    if side == "CE":
        sl = min(c["l"] for c in prior)
        risk = entry - sl
        if risk <= 0:
            return None
        return sl, entry + R1 * risk, entry + RR * risk
    sl = max(c["h"] for c in prior)
    risk = sl - entry
    if risk <= 0:
        return None
    return sl, entry - R1 * risk, entry - RR * risk


# ------------------------------ live trading ------------------------------
def enter(st, side, cs, ts):
    spot = cs[-1]["c"]
    lv = levels(side, spot, cs, len(cs) - 1)
    if not lv:
        tg(f"⚠️ nifty: {side} signal aaya par SL valid nahi (spot SL ke "
           f"galat side par). Trade skip.")
        return
    sl, r1, tgt = lv
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
        "lot_size": info["lot"], "lots_total": LOTS, "lots_open": LOTS,
        "spot_entry": spot, "sl0": sl, "sl": sl, "r1": r1, "tgt": tgt,
        "stage": 0, "opt_entry": pick["ltp"],
        "entry_time": now().strftime("%H:%M:%S"),
        "legs": [], "booked_pnl": 0.0,
    }
    st["last_pnl_slot"] = bnd(now()).isoformat()
    save_state(st)
    FEED.subscribe(pick["key"])
    p = st["pos"]
    tg(f"🟢 NIFTY ENTRY #{p['no']} — {side} BUY ({LOTS} lot)\n"
       f"Candle: {ts.strftime('%H:%M')} close, EMA {FAST_EMA}/{SLOW_EMA} "
       f"{'upar' if side == 'CE' else 'niche'} cross\n"
       f"Spot: {spot:.2f}\n"
       f"Strike: {p['strike']} {side} (delta {p['delta']:.2f}), "
       f"expiry {p['expiry']}\n"
       f"Premium entry: {p['opt_entry']:.2f} | Qty: {p['lot_size'] * LOTS}\n"
       f"SL spot: {sl:.2f} ({abs(spot - sl):.1f} pts)\n"
       f"1:{R1:g} spot: {r1:.2f} → {BOOK_LOTS} lot book, baaki cost-to-cost\n"
       f"Target 1:{RR:g} spot: {tgt:.2f}\n"
       f"(SL/target spot touch hote hi turant exit)")


def close_lots(st, lots, reason, spot=None):
    """Khuli position ke `lots` lot abhi ke live price par band (market order)."""
    p = st["pos"]
    if spot is None:
        spot = FEED.price(SPOT_KEY, urgent=True)
    opt = FEED.price(p["key"], urgent=True)
    if spot is None or opt is None:
        raise RuntimeError("live price nahi mila (exit retry hoga)")
    pnl = (opt - p["opt_entry"]) * p["lot_size"] * lots
    p["legs"].append({"lots": lots, "time": now().strftime("%H:%M:%S"),
                      "spot": round(spot, 2), "opt": round(opt, 2),
                      "reason": reason, "pnl": round(pnl, 2)})
    p["lots_open"] -= lots
    p["booked_pnl"] += pnl
    save_state(st)
    return spot, opt, pnl


def finish(st):
    """Position poori band ho gayi: CSV row + Telegram report."""
    p = st["pos"]
    legs = p["legs"]
    total = round(sum(l["pnl"] for l in legs), 2)
    row = {
        "date": st["date"], "trade_no": p["no"], "side": p["side"],
        "entry_time": p["entry_time"], "spot_entry": round(p["spot_entry"], 2),
        "sl_spot": round(p["sl0"], 2), "target_spot": round(p["tgt"], 2),
        "strike": p["strike"], "expiry": p["expiry"],
        "delta": round(p["delta"], 3), "option_entry": round(p["opt_entry"], 2),
        "lots": p["lots_total"], "qty_per_lot": p["lot_size"],
        "exit_time": legs[-1]["time"], "total_pnl": total,
    }
    for i in (1, 2):
        if len(legs) >= i:
            l = legs[i - 1]
            row.update({f"leg{i}_lots": l["lots"], f"leg{i}_time": l["time"],
                        f"leg{i}_spot": l["spot"], f"leg{i}_option": l["opt"],
                        f"leg{i}_reason": l["reason"], f"leg{i}_pnl": l["pnl"]})
        else:
            for c in ("lots", "time", "spot", "option", "reason", "pnl"):
                row[f"leg{i}_{c}"] = ""
    append_csv(row)
    st["done"].append(row)
    st["pos"] = None
    save_state(st)
    FEED.unsubscribe(p["key"])
    icon = "✅" if total >= 0 else "🔴"
    lines = [f"{icon} NIFTY EXIT #{p['no']} — {p['side']} {p['strike']} "
             f"(trade band)",
             f"Entry: spot {p['spot_entry']:.2f} | premium {p['opt_entry']:.2f} "
             f"| {p['lots_total']} lot x {p['lot_size']}"]
    for l in legs:
        lines.append(f"• {l['lots']} lot {l['reason']} {l['time']}: spot "
                     f"{l['spot']:.2f}, premium {l['opt']:.2f}, "
                     f"P&L Rs {l['pnl']:,.2f}")
    lines.append(f"TOTAL P&L: Rs {total:,.2f}")
    day_total = sum(r["total_pnl"] for r in st["done"])
    lines.append(f"Aaj ka total ({len(st['done'])} trade): Rs {day_total:,.2f}")
    tg("\n".join(lines))


def exit_all(st, reason, spot=None):
    close_lots(st, st["pos"]["lots_open"], reason, spot)
    finish(st)


def book_partial(st, spot):
    p = st["pos"]
    spot, opt, pnl = close_lots(st, BOOK_LOTS, f"1:{R1:g} BOOK", spot)
    p["stage"] = 1
    p["sl"] = p["spot_entry"]
    save_state(st)
    live = (opt - p["opt_entry"]) * p["lot_size"] * p["lots_open"]
    tg(f"🟡 NIFTY #{p['no']} {p['side']} {p['strike']} — "
       f"1:{R1:g} TRADE CLOSE ({BOOK_LOTS} lot)\n"
       f"Booked PNL: Rs {p['booked_pnl']:,.2f}\n"
       f"Live PNL: Rs {live:,.2f}")


def manage(st):
    """Khuli position: SL / 1:1 book / target. Spot price touch hote hi turant
    (candle close ka wait nahi)."""
    p = st["pos"]
    spot = FEED.price(SPOT_KEY)
    if spot is None:
        return
    long_ = p["side"] == "CE"
    sl_hit = spot <= p["sl"] if long_ else spot >= p["sl"]
    r1_hit = spot >= p["r1"] if long_ else spot <= p["r1"]
    t_hit = spot >= p["tgt"] if long_ else spot <= p["tgt"]
    if sl_hit:
        exit_all(st, "SL" if p["stage"] == 0 else "CTC", spot)
        return
    if p["stage"] == 0 and r1_hit:
        book_partial(st, spot)
    if p["stage"] == 1 and t_hit:
        exit_all(st, "TARGET", spot)


def live_numbers(p, opt):
    """Option points diff (CMP - entry), khule lot ka live PNL, total."""
    qty = p["lot_size"] * p["lots_open"]
    diff = opt - p["opt_entry"]
    live = diff * qty
    return diff, qty, live, p["booked_pnl"] + live


def pnl_update(st, n):
    """Position khuli ho to har 5 min mein Telegram par live PNL."""
    p = st["pos"]
    if not p:
        return
    b = bnd(n)
    slot = b.isoformat()
    if slot <= st.get("last_pnl_slot", "") or (n - b).total_seconds() < 5:
        return
    if b.minute % PNL_UPDATE_MINUTES:
        return
    spot = FEED.price(SPOT_KEY)
    opt = FEED.price(p["key"])
    if spot is None or opt is None:
        return
    diff, qty, live, total = live_numbers(p, opt)
    st["last_pnl_slot"] = slot
    save_state(st)
    icon = "📈" if total >= 0 else "📉"
    tg(f"{icon} NIFTY LIVE PNL {n.strftime('%H:%M')} — #{p['no']} {p['side']} "
       f"{p['strike']}\n"
       f"Entry {p['opt_entry']:.2f} | CMP {opt:.2f} | Diff {diff:+.2f} pts\n"
       f"Khule lot {p['lots_open']}/{p['lots_total']}: {diff:+.2f} x {qty} = "
       f"Live PNL Rs {live:,.2f}\n"
       f"Booked PNL: Rs {p['booked_pnl']:,.2f}\n"
       f"Total: Rs {total:,.2f}\n"
       f"Spot {spot:.2f} | SL {p['sl']:.2f} | Target {p['tgt']:.2f}")


def minute_log(st, n):
    """Position khuli ho to har 1 min mein option PNL: log + pnl_log.csv."""
    p = st["pos"]
    if not p:
        return
    slot = n.replace(second=0, microsecond=0).isoformat()
    if slot <= st.get("last_min_slot", ""):
        return
    spot = FEED.price(SPOT_KEY)
    opt = FEED.price(p["key"])
    if spot is None or opt is None:
        return
    diff, qty, live, total = live_numbers(p, opt)
    st["last_min_slot"] = slot
    row = {"date": st["date"], "time": n.strftime("%H:%M"),
           "trade_no": p["no"], "side": p["side"], "strike": p["strike"],
           "spot": round(spot, 2), "option_cmp": round(opt, 2),
           "option_entry": round(p["opt_entry"], 2),
           "pts_diff": round(diff, 2), "lots_open": p["lots_open"],
           "qty_open": qty, "live_pnl": round(live, 2),
           "booked_pnl": round(p["booked_pnl"], 2),
           "total_pnl": round(total, 2)}
    append_pnl_log(row)
    log(f"PNL1m #{p['no']} {p['side']} {p['strike']} spot {spot:.2f} "
        f"opt {opt:.2f} diff {diff:+.2f} x {qty} live {live:,.2f} "
        f"booked {p['booked_pnl']:,.2f} total {total:,.2f}")


def handle_signal(st, sig, cs, ts):
    p = st["pos"]
    if p:
        if p["side"] == sig:
            return
        tg(f"🔄 NIFTY: {sig} crossover aaya, chalti {p['side']} trade band "
           f"karke nayi trade lunga")
        exit_all(st, "REVERSAL")
    if st["trades"] >= MAX_TRADES:
        tg(f"nifty: {sig} signal aaya par aaj ke {MAX_TRADES} trade ho chuke, "
           f"nayi trade nahi.")
        return
    enter(st, sig, cs, ts)


def day_summary(st):
    done = st["done"]
    total = sum(r["total_pnl"] for r in done)
    wins = sum(1 for r in done if r["total_pnl"] > 0)
    tg(f"📊 NIFTY DAY SUMMARY {st['date']}\n"
       f"Trades: {len(done)} | Win: {wins} | Loss: {len(done) - wins}\n"
       f"Total P&L: Rs {total:,.2f}")


def run_live():
    global FEED
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

    FEED = Feed()
    keys = [SPOT_KEY] + ([st["pos"]["key"]] if st["pos"] else [])
    FEED.start(keys)
    for _ in range(40):                      # max ~8 sec websocket tick ka wait
        if FEED.ws_ok:
            break
        time.sleep(0.2)
    feed_txt = ("websocket (tick by tick) ✅" if FEED.ws_ok else
                f"REST fallback (har ~{REST_FALLBACK_SECONDS} sec) ⚠️")

    tg(f"🚀 NIFTY LIVE (paper) shuru\n"
       f"EMA {FAST_EMA}/{SLOW_EMA} | 5 min | delta {TARGET_DELTA} | "
       f"{LOTS} lot\n"
       f"SL pichhli {SL_CANDLES} candle H/L | 1:{R1:g} par {BOOK_LOTS} lot "
       f"book, baaki CTC | Target 1:{RR:g} (spot)\n"
       f"Entry candle close par, SL/target touch par turant exit\n"
       f"Price feed: {feed_txt}\n"
       f"Aaj ke trades ab tak: {st['trades']}/{MAX_TRADES}"
       + (" | position khuli hai" if st["pos"] else ""))

    errors = 0
    next_try = 0.0
    try:
        while not stop["flag"]:
            n = now()
            if n >= end:
                if st["pos"]:
                    exit_all(st, "TIME 15:15")
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

                # 1) khuli position: SL / 1:1 / target (tick par)
                if st["pos"]:
                    manage(st)
                # 2) har 5 min Telegram PnL + har 1 min log
                if st["pos"]:
                    pnl_update(st, n)
                    minute_log(st, n)

                # 3) naya candle close hua? crossover dekho
                b = bnd(n)
                last_start = b - dt.timedelta(minutes=5)
                key = last_start.isoformat()
                age = (n - b).total_seconds()

                if (key > st["last_candle"] and age >= 4
                        and time.time() >= next_try):
                    in_win = (at(today, WINDOW_START) <= last_start
                              <= at(today, LAST_CANDLE_START))
                    if age > 90 or not in_win:
                        st["last_candle"] = key
                        save_state(st)
                    else:
                        cs = get_candles()
                        if not cs or cs[-1]["ts"] < last_start:
                            if age < 60:
                                next_try = time.time() + 3   # thodi der baad retry
                            else:
                                st["last_candle"] = key
                                save_state(st)
                        else:
                            st["last_candle"] = key
                            save_state(st)
                            sig = latest_signal(cs)
                            log("candle", last_start.strftime("%H:%M"),
                                "crossover:", sig or "koi nahi")
                            if sig:
                                handle_signal(st, sig, cs, last_start)
                errors = 0
            except Exception as e:
                errors += 1
                log("loop error:", e)
                if errors == 10:
                    tg(f"⚠️ nifty: baar baar error aa raha hai: {str(e)[:150]}")
                time.sleep(1)
            if st["pos"]:
                time.sleep(TICK_SECONDS)
            else:
                nxt = (bnd(n).replace(second=4) + dt.timedelta(minutes=5))
                time.sleep(max(1, min(20, (nxt - now()).total_seconds())))
    finally:
        save_state(st)
        FEED.stop()
        if stop["flag"]:
            tg("⛔ nifty run beech mein ruk gaya"
               + (" — POSITION KHULI HAI, jaldi dobara run karo (mode 0)."
                  if st["pos"] else ". Dobara run karo (mode 0)."))
            return
    day_summary(st)


# ------------------------------ test mode ------------------------------
def simulate_day(candles, day):
    """Spot par replay (naye rules): cross -> entry at close, SL pichhli candle,
    1:1 par BOOK_LOTS book, baaki CTC, target 1:RR, ulta cross = reversal.
    pts = sab lot ke points ka jod (har lot ke liye alag gina)."""
    closes = [c["c"] for c in candles]
    ef = ema_series(closes, FAST_EMA)
    es = ema_series(closes, SLOW_EMA)
    idx = [i for i, c in enumerate(candles) if c["ts"].date() == day]
    out = []
    state = {"pos": None, "count": 0}

    def pts(side, entry, px):
        return (px - entry) if side == "CE" else (entry - px)

    def close(px, why, lots):
        p = state["pos"]
        p["pts"] += pts(p["side"], p["entry"], px) * lots
        p["legs"].append(f"{lots}L {why}@{px:.1f}")
        p["open"] -= lots
        if p["open"] <= 0:
            out.append({"side": p["side"], "t": p["t"], "entry": p["entry"],
                        "legs": " | ".join(p["legs"]), "pts": p["pts"]})
            state["pos"] = None

    for k in idx:
        c = candles[k]
        p = state["pos"]
        if p:
            long_ = p["side"] == "CE"
            sl_hit = c["l"] <= p["sl"] if long_ else c["h"] >= p["sl"]
            r1_hit = c["h"] >= p["r1"] if long_ else c["l"] <= p["r1"]
            t_hit = c["h"] >= p["tgt"] if long_ else c["l"] <= p["tgt"]
            if sl_hit:
                close(p["sl"], "SL" if p["stage"] == 0 else "CTC", p["open"])
            else:
                if p["stage"] == 0 and r1_hit:
                    close(p["r1"], "1:1", BOOK_LOTS)
                    p["stage"] = 1
                    p["sl"] = p["entry"]
                if state["pos"] and p["stage"] == 1 and t_hit:
                    close(p["tgt"], "TARGET", p["open"])

        in_win = (at(day, WINDOW_START) <= c["ts"] <= at(day, LAST_CANDLE_START))
        sig = cross_at(ef, es, k) if in_win else None
        if sig:
            p = state["pos"]
            if p and p["side"] != sig:
                close(c["c"], "REVERSAL", p["open"])
            if not state["pos"] and state["count"] < MAX_TRADES:
                lv = levels(sig, c["c"], candles, k)
                if lv:
                    state["count"] += 1
                    state["pos"] = {
                        "side": sig, "entry": c["c"], "sl": lv[0], "r1": lv[1],
                        "tgt": lv[2], "stage": 0, "open": LOTS, "pts": 0.0,
                        "legs": [], "t": c["ts"].strftime("%H:%M")}
        p = state["pos"]
        if p and c["ts"] >= at(day, LAST_CANDLE_START):
            close(c["c"], "TIME", p["open"])
    if state["pos"]:
        close(candles[idx[-1]]["c"], "TIME(open)", state["pos"]["open"])
    return out


def run_test():
    L = ["🧪 NIFTY TEST (mode 1) — koi trade/CSV save nahi hoga",
         f"Time: {now().strftime('%d-%b %H:%M')}"]
    L.append(f"Settings: EMA {FAST_EMA}/{SLOW_EMA}, delta {TARGET_DELTA}, "
             f"{LOTS} lot, SL pichhli {SL_CANDLES} candle H/L, "
             f"1:{R1:g} par {BOOK_LOTS} lot book + CTC, target 1:{RR:g}, "
             f"max {MAX_TRADES} trades")

    try:
        L.append(f"✅ Upstox token OK | Nifty spot: {ltp(SPOT_KEY):.2f}")
    except Exception as e:
        L.append(f"❌ Upstox token/spot error: {str(e)[:120]}")

    # live price feed check (websocket)
    f = Feed()
    try:
        try:
            import upstox_client  # noqa: F401
            have_sdk = True
        except Exception:
            have_sdk = False
        if not have_sdk:
            L.append("⚠️ Live feed: upstox SDK install nahi hui (workflow mein "
                     "'pip install requests upstox-python-sdk' chahiye). "
                     "Abhi REST fallback chalega.")
        else:
            f.start([SPOT_KEY])
            for _ in range(60):          # max ~12 sec
                if f.ws_ok:
                    break
                time.sleep(0.2)
            if f.ws_ok:
                L.append(f"✅ Live feed (websocket) chal raha hai | spot tick: "
                         f"{f.px[SPOT_KEY][0]:.2f}")
            else:
                L.append("⚠️ Websocket se tick nahi aaya (market band ho to "
                         "normal ho sakta hai). Tab REST fallback chalega.")
    except Exception as e:
        L.append(f"⚠️ Live feed check error: {str(e)[:100]}")
    finally:
        f.stop()

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
                L.append("Replay: us din koi trade nahi (window 9:20-15:15)")
            for r in res:
                L.append(f"• {r['t']} {r['side']} entry {r['entry']:.1f} → "
                         f"{r['legs']} = {r['pts']:+.1f} pts")
            if res:
                L.append(f"Replay total ({LOTS} lot ke pts ka jod, spot par): "
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
