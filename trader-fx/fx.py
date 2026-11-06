#!/usr/bin/env python3
"""Forex and gold day trading for trader-fx. Standard library only.

The stock pack assumes one market: NYSE, 9:30 to 4:00 New York time, whole
shares, long only. Forex and gold trade almost around the clock, size in
units, lots, ounces or contracts, and go both ways. This tool redoes the
day-trading chain for them in one file:

    python3 fx.py info EURUSD                          # pip, lot, pip value, the default spread
    python3 fx.py check EURUSD --session london        # opening-range breakout: waiting / triggered / expired
    python3 fx.py check XAUUSD --session comex --side buy --write
    python3 fx.py plan --stop or-mid --target 1.5R --write          # trader/fx-entry.json -> trader/fx-plan.json
    python3 fx.py plan --symbol EURUSD --side sell --entry 1.11850 --or-high 1.12010 --or-low 1.11880
    python3 fx.py size --account 5000 --risk 50 --write             # -> trader/fx-order.json
    python3 fx.py replay --write                                    # the Time Machine for this plan

Sessions (local wall-clock times, worked out without zoneinfo):
    tokyo    09:00 to 15:00 Tokyo     entries until 12:00
    london   08:00 to 16:00 London    entries until 12:00   (the default)
    newyork  08:00 to 16:00 New York  entries until 12:00
    comex    08:20 to 13:30 New York  entries until 11:30   (gold's busiest hours)
--open, --flat and --last-entry override any of them (HH:MM, 24-hour, on the
5-minute grid). The opening range is the first --or-minutes (15 by default).

Instruments: any pair of two currencies (EURUSD, USDJPY, GBPJPY...), spot gold
and silver (XAUUSD, XAGUSD) and COMEX gold futures (GC, MGC). Distances print
in pips for currencies and in dollars for metals.

Data: Yahoo's free 5-minute chart (EURUSD=X, GC=F...), about 60 days of it,
unofficial and delayed. Yahoo has no spot gold, so XAUUSD reads COMEX gold
futures (GC=F) and says so: futures sit a few dollars to tens of dollars from
spot, so for spot gold give your broker's own bars with --csv FILE (columns
time, open, high, low, close; times in UTC unless --csv-tz says otherwise).

Nothing here places an order. `size` writes a ticket you can read and place
yourself. Not financial advice.
"""

import argparse
import csv
import datetime
import gzip
import json
import math
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
HOSTS = ("query2", "query1")
PAUSE = 0.5
BACKOFF = 5.0
INTERVAL = 300                 # 5-minute bars
EPOCH = datetime.datetime(1970, 1, 1)
MIN_SESSIONS = 20
MAX_FILL = 3                   # missing 5-minute bars a past session may have (filled flat) and still count
COIN_FLIP = 0.05
STRONG = 0.15
MANY_NEITHER = 0.40
MIN_RISK_SPREADS = 3           # a stop closer than 3 spreads is noise, not a stop

SESSIONS = {
    "tokyo": {"tz": "tokyo", "open": "09:00", "flat": "15:00", "last_entry": "12:00"},
    "london": {"tz": "london", "open": "08:00", "flat": "16:00", "last_entry": "12:00"},
    "newyork": {"tz": "newyork", "open": "08:00", "flat": "16:00", "last_entry": "12:00"},
    "comex": {"tz": "newyork", "open": "08:20", "flat": "13:30", "last_entry": "11:30"},
}
TZ_NAMES = {"tokyo": "Tokyo", "london": "London", "newyork": "New York", "utc": "UTC"}

CCY = set("USD EUR GBP JPY CHF CAD AUD NZD SEK NOK DKK SGD HKD MXN ZAR TRY PLN CNH HUF CZK".split())
MAJORS = {"EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD"}
METALS = {
    "XAU": {"name": "spot gold", "lot": 100, "tick": 0.01, "decimals": 2, "yahoo": "GC=F", "spread": 0.30},
    "XAG": {"name": "spot silver", "lot": 5000, "tick": 0.001, "decimals": 3, "yahoo": "SI=F", "spread": 0.03},
}
FUTURES = {
    "GC": {"name": "COMEX gold futures", "mult": 100, "tick": 0.10, "decimals": 1, "yahoo": "GC=F"},
    "MGC": {"name": "COMEX micro gold futures", "mult": 10, "tick": 0.10, "decimals": 1, "yahoo": "MGC=F"},
}
ALIASES = {"GOLD": "XAUUSD", "XAU": "XAUUSD", "SILVER": "XAGUSD", "XAG": "XAGUSD"}


class FXError(Exception):
    pass


# ---------------------------------------------------------------- time zones, without zoneinfo (Python 3.8)

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def _last_sunday(year, month):
    nxt = datetime.date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - datetime.timedelta(days=1)
    return d - datetime.timedelta(days=(d.weekday() + 1) % 7)


def newyork_offset(utc):
    """-4 from 2:00 a.m. on the second Sunday of March to 2:00 a.m. on the first Sunday of November, else -5."""
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    return -4 if start <= utc.replace(tzinfo=None) < end else -5


def london_offset(utc):
    """+1 from 01:00 UTC on the last Sunday of March to 01:00 UTC on the last Sunday of October, else 0."""
    y = utc.year
    start = datetime.datetime.combine(_last_sunday(y, 3), datetime.time(1, 0))
    end = datetime.datetime.combine(_last_sunday(y, 10), datetime.time(1, 0))
    return 1 if start <= utc.replace(tzinfo=None) < end else 0


TZ = {
    "newyork": (newyork_offset, (-4, -5)),
    "london": (london_offset, (1, 0)),
    "tokyo": (lambda u: 9, (9,)),
    "utc": (lambda u: 0, (0,)),
}


def to_local(utc, tz):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=TZ[tz][0](utc))


def local_to_utc(local, tz):
    fn, offs = TZ[tz]
    for off in offs:
        u = local - datetime.timedelta(hours=off)
        if fn(u) == off:
            return u
    return local - datetime.timedelta(hours=offs[-1])


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def epoch_of(utc):
    return int((utc.replace(tzinfo=None) - EPOCH).total_seconds())


def parse_hhmm(text, what):
    m = re.match(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$", str(text or ""))
    if not m:
        raise FXError("%s wants a 24-hour time like 08:00 or 13:30, not %r" % (what, text))
    return int(m.group(1)) * 60 + int(m.group(2))


def hhmm(minutes):
    return "%02d:%02d" % divmod(int(minutes), 60)


def parse_now(text, tz):
    """--now: ISO time. Without an offset it is the session's own local time."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00").replace("T", " ")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise FXError("--now wants a time like \"2026-11-02 08:40\" (session time) or ...Z (UTC)")
    if dt.tzinfo is None:
        return local_to_utc(dt, tz)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


# ---------------------------------------------------------------- sessions and instruments

def make_session(name="london", open_=None, flat=None, last_entry=None, tz=None):
    if name not in SESSIONS:
        raise FXError("unknown session %r: pick one of %s" % (name, ", ".join(sorted(SESSIONS))))
    base = dict(SESSIONS[name])
    if tz:
        if tz not in TZ:
            raise FXError("unknown time zone %r: pick one of %s" % (tz, ", ".join(sorted(TZ))))
        base["tz"] = tz
    o = parse_hhmm(open_ or base["open"], "--open")
    f = parse_hhmm(flat or base["flat"], "--flat")
    le = parse_hhmm(last_entry or base["last_entry"], "--last-entry")
    if o % 5 or f % 5:
        raise FXError("--open and --flat must sit on the 5-minute grid (08:00, 08:05...)")
    if not o < f:
        raise FXError("the session must open before it goes flat (%s to %s)" % (hhmm(o), hhmm(f)))
    le = min(max(le, o + 5), f)
    return {"name": name, "tz": base["tz"], "open": o, "flat": f, "last_entry": le, "slots": (f - o) // 5}


def session_label(s):
    return "%s session, %s to %s %s time, entries until %s" % (
        s["name"], hhmm(s["open"]), hhmm(s["flat"]), TZ_NAMES[s["tz"]], hhmm(s["last_entry"]))


def instrument(sym):
    s = str(sym or "").upper().strip()
    for suf in ("=X", "=F"):
        if s.endswith(suf):
            s = s[:-len(suf)]
    s = re.sub(r"[/_\-\s]", "", s)
    s = ALIASES.get(s, s)
    if s in FUTURES:
        f = FUTURES[s]
        return {"symbol": s, "kind": "future", "name": f["name"], "base": "XAU", "quote": "USD",
                "tick": f["tick"], "decimals": f["decimals"], "pip": None, "lot": 1, "mult": f["mult"],
                "unit": "contracts", "step": 1, "yahoo": f["yahoo"], "spread": f["tick"]}
    if len(s) == 6 and s[:3] in METALS and s[3:] == "USD":
        m = METALS[s[:3]]
        return {"symbol": s, "kind": "metal", "name": m["name"], "base": s[:3], "quote": "USD",
                "tick": m["tick"], "decimals": m["decimals"], "pip": None, "lot": m["lot"], "mult": 1,
                "unit": "oz", "step": 1, "yahoo": m["yahoo"], "spread": m["spread"]}
    if len(s) == 6 and s[:3] in CCY and s[3:] in CCY and s[:3] != s[3:]:
        jpy = s[3:] == "JPY"
        pip = 0.01 if jpy else 0.0001
        return {"symbol": s, "kind": "fx", "name": "%s/%s" % (s[:3], s[3:]), "base": s[:3], "quote": s[3:],
                "tick": pip / 10, "decimals": 3 if jpy else 5, "pip": pip, "lot": 100000, "mult": 1,
                "unit": "units", "step": 1000, "yahoo": s + "=X",
                "spread": pip * (1.0 if s in MAJORS else 2.0)}
    raise FXError("%r is not a currency pair (EURUSD), spot metal (XAUUSD, XAGUSD) or gold future (GC, MGC)"
                  % sym)


def fmt_px(inst, p):
    return "%.*f" % (inst["decimals"], p)


def fmt_dist(inst, d):
    if inst["kind"] == "fx":
        return "%.1f pips" % (d / inst["pip"])
    return "$%.2f" % d


def round_tick(inst, x, mode="nearest"):
    q = round(x / inst["tick"], 6)
    n = math.floor(q) if mode == "down" else math.ceil(q) if mode == "up" else math.floor(q + 0.5)
    return round(n * inst["tick"], inst["decimals"])


def quote_to_usd(inst, price, quote_usd=None):
    """USD value of one unit of the quote currency."""
    if inst["quote"] == "USD":
        return 1.0
    if inst["base"] == "USD":
        return 1.0 / price
    if quote_usd:
        return float(quote_usd)
    raise FXError("%s is quoted in %s: pass --quote-usd, the USD value of 1 %s (about 0.0067 for JPY, 1.30 for "
                  "GBP), so the risk can be counted in dollars" % (inst["symbol"], inst["quote"], inst["quote"]))


# ---------------------------------------------------------------- data

def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def yahoo_url(ysym, rng, host="query2"):
    return "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=5m" % (
        host, urllib.parse.quote(ysym), rng)


def fetch_yahoo(ysym, rng):
    last = None
    for n, host in enumerate(HOSTS):
        if n:
            time.sleep(PAUSE)
        for attempt in (0, 1):
            try:
                return _get(yahoo_url(ysym, rng, host))
            except urllib.error.HTTPError as e:
                last = e.code
                if e.code == 429 and attempt == 0:
                    time.sleep(BACKOFF)
                    continue
                if e.code == 429 or e.code >= 500:
                    break
                raise FXError("Yahoo answered HTTP %d for %s" % (e.code, ysym))
            except (urllib.error.URLError, OSError) as e:
                raise FXError("could not reach Yahoo for %s: %s. Give your own bars with --csv FILE."
                              % (ysym, getattr(e, "reason", e)))
    raise FXError("Yahoo answered HTTP %s for %s on both hosts. Try again later, or use --csv FILE." % (last, ysym))


def parse_yahoo(text, ysym):
    """Yahoo chart JSON -> (bars, meta). Bars: {"t": epoch UTC, "o", "h", "l", "c"}, oldest first."""
    try:
        data = json.loads(text)
    except ValueError:
        raise FXError("Yahoo returned something that is not JSON for %s" % ysym)
    chart = data.get("chart") if isinstance(data, dict) else None
    res = (chart or {}).get("result")
    if not res:
        err = (chart or {}).get("error") or {}
        raise FXError("Yahoo has no data for %s: %s" % (ysym, err.get("description") or "unknown symbol"))
    r = res[0]
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close")]
    bars = {}
    for i, ts in enumerate(r.get("timestamp") or []):
        row = [c[i] if i < len(c) else None for c in cols]
        if ts is None or None in row:
            continue
        bars[int(ts)] = {"t": int(ts), "o": float(row[0]), "h": float(row[1]), "l": float(row[2]),
                         "c": float(row[3])}
    return [bars[t] for t in sorted(bars)], r.get("meta") or {}


def _csv_time(v, tz):
    v = str(v).strip()
    if re.match(r"^\d{9,13}(\.\d+)?$", v):
        x = float(v)
        return int(x / 1000 if x > 1e11 else x)
    t = v.replace("Z", "+00:00").replace("T", " ")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise FXError("cannot read the time %r: use ISO (2026-11-02 08:05:00) or epoch seconds" % v)
    if dt.tzinfo is None:
        return epoch_of(local_to_utc(dt, tz))
    return epoch_of((dt - dt.utcoffset()).replace(tzinfo=None))


def parse_csv(path, tz="utc"):
    """A CSV of bars: a time column (time, timestamp, datetime or date) plus open, high, low, close."""
    if not os.path.exists(path):
        raise FXError("no CSV at %s" % path)
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise FXError("%s has no rows" % path)
    keys = {k.strip().lower(): k for k in rows[0].keys() if k}
    tcol = next((keys[k] for k in ("time", "timestamp", "datetime", "date", "gmt time") if k in keys), None)
    need = [k for k in ("open", "high", "low", "close") if k not in keys]
    if tcol is None or need:
        raise FXError("%s needs a time column and open, high, low, close (missing: %s)"
                      % (path, ", ".join((["time"] if tcol is None else []) + need)))
    bars = {}
    for r in rows:
        try:
            t = _csv_time(r[tcol], tz)
            o, h, l, c = (float(r[keys[k]]) for k in ("open", "high", "low", "close"))
        except (TypeError, ValueError):
            continue
        bars[t] = {"t": t, "o": o, "h": h, "l": l, "c": c}
    return [bars[t] for t in sorted(bars)]


def load_bars(a, inst, rng):
    """(bars, as_of cap or None, source note)."""
    if getattr(a, "csv", None):
        return parse_csv(a.csv, a.csv_tz), None, "your CSV %s" % a.csv
    ysym = inst["yahoo"]
    text = fetch_yahoo(ysym, rng)
    bars, meta = parse_yahoo(text, ysym)
    cap = meta.get("regularMarketTime")
    note = "Yahoo %s 5-minute bars (free, delayed)" % ysym
    if inst["kind"] == "metal":
        note += ". Yahoo has no spot %s, so these are COMEX futures prices: they sit a few dollars off spot. " \
                "For your broker's exact levels use --csv" % inst["base"]
    return bars, (utc_of(cap) if isinstance(cap, (int, float)) else None), note


def sessionize(bars, sess, as_of):
    """{local date: {slot: bar}} for bars inside the session window that were complete by as_of."""
    days = {}
    for b in bars:
        if b["t"] % INTERVAL:
            continue
        start = utc_of(b["t"])
        if start + datetime.timedelta(seconds=INTERVAL) > as_of:
            continue
        loc = to_local(start, sess["tz"])
        m = loc.hour * 60 + loc.minute - sess["open"]
        if m < 0 or m >= sess["slots"] * 5:
            continue
        days.setdefault(loc.date().isoformat(), {})[m // 5] = dict(b, slot=m // 5, start=loc.strftime("%H:%M"))
    return days


def full_sessions(days, sess, before):
    """Past sessions with every bar (up to MAX_FILL gaps after the first bar, filled flat), oldest first."""
    full, short, filled = [], [], 0
    n = sess["slots"]
    for d in sorted(days):
        if d >= before:
            continue
        D = days[d]
        missing = [i for i in range(n) if i not in D]
        if 0 in missing or len(missing) > MAX_FILL:
            short.append({"date": d, "bars": len(D)})
            continue
        rows = []
        for i in range(n):
            if i in D:
                rows.append(D[i])
            else:
                pc = rows[-1]["c"]
                rows.append({"o": pc, "h": pc, "l": pc, "c": pc, "slot": i, "filled": True})
        filled += len(missing)
        full.append((d, rows))
    return full, short, filled


def atr(rows, period=14):
    rows = list(rows)
    if len(rows) < 2:
        return None
    trs = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) for p, b in zip(rows, rows[1:])]
    trs = trs[-period:]
    return sum(trs) / len(trs)


# ---------------------------------------------------------------- the entry: opening-range breakout, both ways

def evaluate(D, sess, or_minutes, side, now_min):
    """Pure function. D = {slot: completed bar} for one session, now_min = local minutes after midnight."""
    n = or_minutes // 5
    res = {"status": "waiting", "side": side, "or_minutes": or_minutes}
    if now_min < sess["open"]:
        res.update(status="not open", reason="the %s session opens at %s" % (sess["name"], hhmm(sess["open"])))
        return res
    have = [i for i in range(n) if i in D]
    if len(have) < n:
        if now_min < sess["open"] + or_minutes + 5:
            res.update(status="building", reason="the opening range runs to %s, %d of %d bars in"
                       % (hhmm(sess["open"] + or_minutes), len(have), n))
            return res
        res.update(status="no data", reason="the opening range is missing %d of its %d bars: no clean range today"
                   % (n - len(have), n))
        return res
    oh = max(D[i]["h"] for i in range(n))
    ol = min(D[i]["l"] for i in range(n))
    res.update(or_high=oh, or_low=ol)
    last = None
    for i in sorted(s for s in D if s >= n):
        start = sess["open"] + 5 * i
        if start >= sess["last_entry"]:
            break
        c = D[i]["c"]
        last = D[i]
        hit = "buy" if side in ("buy", "both") and c > oh else "sell" if side in ("sell", "both") and c < ol else None
        if hit:
            rows = [D[j] for j in sorted(D) if j <= i]
            res.update(status="triggered", side=hit, entry=c, entry_slot=i, entry_time=hhmm(start + 5),
                       atr5m=atr(rows))
            return res
    lastc = (last or D[n - 1])["c"]
    res.update(last=lastc, to_high=oh - lastc, to_low=lastc - ol)
    if now_min >= sess["last_entry"]:
        res.update(status="expired", reason="no 5-minute close beyond the range before %s" % hhmm(sess["last_entry"]))
    else:
        res["reason"] = "no 5-minute close beyond the range yet"
    return res


# ---------------------------------------------------------------- the plan: stop and target, both ways

def parse_stop_rule(text):
    t = str(text or "or-mid").strip().lower()
    if t in ("or-mid", "or-far"):
        return t, None
    m = re.match(r"^(pips|dist|atr):\s*([0-9]*\.?[0-9]+)$", t)
    if not m or float(m.group(2)) <= 0:
        raise FXError("--stop wants or-mid, or-far, pips:N, dist:N (in price) or atr:K, not %r" % text)
    return m.group(1), float(m.group(2))


def parse_target(text):
    t = str(text or "1.5R").strip()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*[Rr]$", t)
    if m and float(m.group(1)) > 0:
        return "r", float(m.group(1))
    m = re.match(r"^price:\s*([0-9]*\.?[0-9]+)$", t.lower())
    if m:
        return "price", float(m.group(1))
    raise FXError("--target wants 1R, 1.5R, 2R or price:X, not %r" % text)


def make_plan(inst, side, entry, stop_rule="or-mid", target="1.5R", or_high=None, or_low=None, atr5m=None,
              spread=None):
    if side not in ("buy", "sell"):
        raise FXError("side must be buy or sell, not %r" % side)
    long = side == "buy"
    rule, x = parse_stop_rule(stop_rule)
    if rule in ("or-mid", "or-far"):
        if or_high is None or or_low is None:
            raise FXError("a %s stop needs the opening range: give --or-high and --or-low" % rule)
        raw = (or_high + or_low) / 2 if rule == "or-mid" else (or_low if long else or_high)
    else:
        if rule == "pips":
            if inst["kind"] != "fx":
                raise FXError("pips:N is for currency pairs. For metals use dist:N (dollars)")
            d = x * inst["pip"]
        elif rule == "dist":
            d = x
        else:
            if not atr5m:
                raise FXError("an atr:K stop needs the 5-minute ATR from fx-entry.json (run check first)")
            d = x * atr5m
        raw = entry - d if long else entry + d
    stop = round_tick(inst, raw, "down" if long else "up")
    risk = (entry - stop) if long else (stop - entry)
    if risk <= 0:
        raise FXError("the stop %s is on the wrong side of the entry %s for a %s"
                      % (fmt_px(inst, stop), fmt_px(inst, entry), side))
    spread = inst["spread"] if spread is None else spread
    if risk < MIN_RISK_SPREADS * spread:
        raise FXError("the stop is %s away, under %d spreads (%s): normal noise would take it out. Use a wider stop"
                      % (fmt_dist(inst, risk), MIN_RISK_SPREADS, fmt_dist(inst, MIN_RISK_SPREADS * spread)))
    tk, tv = parse_target(target)
    if tk == "r":
        tgt = round_tick(inst, entry + tv * risk if long else entry - tv * risk)
    else:
        tgt = round_tick(inst, tv)
    reward = (tgt - entry) if long else (entry - tgt)
    if reward <= 0:
        raise FXError("the target %s is on the wrong side of the entry for a %s" % (fmt_px(inst, tgt), side))
    return {"symbol": inst["symbol"], "side": side, "entry": round_tick(inst, entry), "stop": stop, "target": tgt,
            "stop_rule": stop_rule, "risk": round(risk, inst["decimals"] + 1),
            "target_r": round(reward / risk, 4), "or_high": or_high, "or_low": or_low}


# ---------------------------------------------------------------- the size: units, lots, ounces or contracts

def size_position(inst, plan, account, risk_usd, leverage=20.0, step=None, quote_usd=None, max_risk_pct=2.0,
                  min_rr=1.0, spread=None):
    e, s, t = float(plan["entry"]), float(plan["stop"]), float(plan["target"])
    dist, reward = abs(e - s), abs(t - e)
    if account <= 0 or risk_usd <= 0:
        raise FXError("the account and the risk must be positive")
    if risk_usd > account * max_risk_pct / 100.0 + 1e-9:
        raise FXError("$%.2f is %.1f%% of a $%.2f account, over your %.1f%% limit (--max-risk-pct)"
                      % (risk_usd, 100.0 * risk_usd / account, account, max_risk_pct))
    rr = reward / dist
    if rr + 1e-9 < min_rr:
        raise FXError("reward:risk is %.2f, under your minimum of %.2f (--min-rr)" % (rr, min_rr))
    q2u = quote_to_usd(inst, e, quote_usd)
    fut = inst["kind"] == "future"
    spread = inst["spread"] if spread is None else spread
    step = 1 if fut else int(step or inst["step"])
    per_unit = (dist + spread) * q2u * inst["mult"]          # the loss at the stop plus the spread you pay
    units = int(math.floor(risk_usd / per_unit / step + 1e-9)) * step
    if units <= 0:
        raise FXError("the stop is too far for $%.2f: the smallest size (%d %s) would risk $%.2f"
                      % (risk_usd, step, inst["unit"].rstrip("s") if step == 1 else inst["unit"], step * per_unit))
    notional = units * inst["mult"] * e * q2u
    margin = None if fut else notional / leverage
    if margin is not None and margin > account:
        raise FXError("%d %s is $%.0f of %s; at %g:1 that needs $%.0f margin, more than the $%.0f account"
                      % (units, inst["unit"], notional, inst["symbol"], leverage, margin, account))
    out = {
        "symbol": inst["symbol"], "side": plan["side"], "units": units, "unit": inst["unit"],
        "lots": round(units / float(inst["lot"]), 4) if not fut else None,
        "entry": e, "stop": s, "target": t,
        "loss_at_stop_usd": round(units * inst["mult"] * dist * q2u, 2),
        "loss_with_spread_usd": round(units * per_unit, 2),
        "reward_at_target_usd": round(units * inst["mult"] * reward * q2u, 2),
        "reward_risk": round(rr, 2), "notional_usd": round(notional, 2),
        "margin_usd": round(margin, 2) if margin is not None else None, "leverage": None if fut else leverage,
        "spread": spread, "quote_to_usd": round(q2u, 8),
    }
    if inst["kind"] == "fx":
        out["pip_value_usd"] = round(units * inst["pip"] * q2u, 4)
    return out


# ---------------------------------------------------------------- the Time Machine, both ways

def build_pool(sessions, n):
    pool = [[] for _ in range(n)]
    for _, D in sessions:
        for i in range(1, n):
            pc = D[i - 1]["c"]
            pool[i].append((D[i]["o"] / pc, D[i]["h"] / pc, D[i]["l"] / pc, D[i]["c"] / pc))
    return pool


def pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def simulate(pool, entry, stop, target, k, last, side, replays=10000, seed=19, window=2):
    """Replays of bars k+1..last, each bar drawn from the same time of day (+/- window bars) in past sessions,
    scaled to the current price. Stop checked before target inside a bar (the conservative call)."""
    long = side == "buy"
    n = len(pool)
    risk = (entry - stop) if long else (stop - entry)
    r_t = ((target - entry) if long else (entry - target)) / risk
    rng = random.Random(seed)
    outcomes, closes = [], []
    for _ in range(replays):
        p, oc = entry, None
        for i in range(k + 1, last + 1):
            j = i if window == 0 else min(n - 1, max(1, i + rng.randint(-window, window)))
            o, h, l, c = rng.choice(pool[j])
            hi, lo = p * h, p * l
            if oc is None:
                if long:
                    oc = "stop" if lo <= stop else "target" if hi >= target else None
                else:
                    oc = "stop" if hi >= stop else "target" if lo <= target else None
            p = p * c
        outcomes.append(oc or "close")
        closes.append(p)
    Rs = [r_t if o == "target" else -1.0 if o == "stop" else ((p - entry) if long else (entry - p)) / risk
          for o, p in zip(outcomes, closes)]
    green = sum(1 for o, p in zip(outcomes, closes)
                if o == "target" or (o == "close" and (p > entry if long else p < entry)))
    srt = sorted(closes)
    return {"replays": replays, "seed": seed, "window": window, "bars_left": last - k,
            "p_target": round(outcomes.count("target") / replays, 4),
            "p_stop": round(outcomes.count("stop") / replays, 4),
            "p_neither": round(outcomes.count("close") / replays, 4), "p_green": round(green / replays, 4),
            "median_close": pct(srt, 0.5), "p5_close": pct(srt, 0.05), "p95_close": pct(srt, 0.95),
            "avg_R": round(statistics.mean(Rs), 3), "r_at_target": round(r_t, 4)}


def history_replay(sessions, inst, plan, sess, or_minutes, last):
    """The plan's own rule on every past session: same side, same range, same stop rule and target R."""
    long = plan["side"] == "buy"
    n = or_minutes // 5
    le_slot = (sess["last_entry"] - sess["open"]) // 5
    rule, x = parse_stop_rule(plan.get("stop_rule"))
    fixed = abs(plan["entry"] - plan["stop"])
    trades = []
    for d, D in sessions:
        oh = max(b["h"] for b in D[:n])
        ol = min(b["l"] for b in D[:n])
        kk = next((i for i in range(n, min(le_slot, last + 1)) if (D[i]["c"] > oh if long else D[i]["c"] < ol)),
                  None)
        if kk is None or kk >= last:
            continue
        e = D[kk]["c"]
        if rule == "or-mid":
            raw = (oh + ol) / 2
        elif rule == "or-far":
            raw = ol if long else oh
        else:
            raw = e - fixed if long else e + fixed
        s = round_tick(inst, raw, "down" if long else "up")
        risk = (e - s) if long else (s - e)
        if risk <= 0:
            continue
        t = e + plan["target_r"] * risk if long else e - plan["target_r"] * risk
        oc = None
        for b in D[kk + 1:last + 1]:
            if (b["l"] <= s) if long else (b["h"] >= s):
                oc = "stop"
                break
            if (b["h"] >= t) if long else (b["l"] <= t):
                oc = "target"
                break
        cl = D[last]["c"]
        R = plan["target_r"] if oc == "target" else -1.0 if oc == "stop" else ((cl - e) if long else (e - cl)) / risk
        trades.append({"date": d, "time": hhmm(sess["open"] + 5 * (kk + 1)), "entry": round_tick(inst, e),
                       "stop": s, "target": round_tick(inst, t), "outcome": oc or "close", "R": round(R, 2)})
    return {"sessions": len(sessions), "triggered": len(trades),
            "target": sum(1 for x in trades if x["outcome"] == "target"),
            "stop": sum(1 for x in trades if x["outcome"] == "stop"),
            "close": sum(1 for x in trades if x["outcome"] == "close"),
            "avg_R": round(statistics.mean(x["R"] for x in trades), 3) if trades else None, "trades": trades}


def verdict(sim):
    hit = sim["p_target"] + sim["p_stop"]
    R = sim["r_at_target"]
    if hit == 0:
        return "Neither side was hit in any replay: the close decides this one."
    gap = sim["p_target"] / hit - 1.0 / (1.0 + R)
    if abs(gap) < COIN_FLIP:
        line = "Close to a coin flip." if abs(R - 1) < 1e-6 else "Close to break-even at %gR." % round(R, 2)
    else:
        line = ("Strongly toward the %s." if abs(gap) >= STRONG else "Leans toward the %s.") % (
            "target" if gap > 0 else "stop")
    if sim["p_neither"] > MANY_NEITHER:
        line += " %d%% of replays hit neither by the flat time, so where it closes matters most." % round(
            100 * sim["p_neither"])
    if (gap > 0) != (sim["avg_R"] > 0) and abs(gap) >= COIN_FLIP:
        line += " The average R (%+.2f) points the other way." % sim["avg_R"]
    return line


# ---------------------------------------------------------------- files and the commands

def _read_json(path, what):
    if not os.path.exists(path):
        raise FXError("no %s at %s" % (what, path))
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.write("\n")


def _config(a):
    p = os.path.join(a.dir, "config.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("fx") or {}
    except (OSError, ValueError):
        return {}


def _session_from(a, saved=None):
    saved = saved or {}
    return make_session(a.session or saved.get("name") or "london", a.open or (saved.get("open") and hhmm(saved["open"])),
                        a.flat or (saved.get("flat") and hhmm(saved["flat"])),
                        a.last_entry or (saved.get("last_entry") and hhmm(saved["last_entry"])),
                        a.tz or saved.get("tz"))


def cmd_info(a):
    inst = instrument(a.symbol)
    if a.json:
        print(json.dumps(inst, indent=2))
        return
    print("%s: %s (%s)" % (inst["symbol"], inst["name"], inst["kind"]))
    if inst["kind"] == "fx":
        print("  pip %s, price to %d decimals, 1 standard lot = 100,000 %s, sized in steps of %d units"
              % (inst["pip"], inst["decimals"], inst["base"], inst["step"]))
        print("  default spread %s" % fmt_dist(inst, inst["spread"]))
        if inst["quote"] == "USD":
            print("  pip value: $10.00 a standard lot, $1.00 a mini (10k), $0.10 a micro (1k)")
        elif inst["base"] == "USD":
            print("  pip value: 1,000 %s a standard lot, so $ = 1,000 / the price" % inst["quote"])
        else:
            print("  pip value is in %s: size needs --quote-usd" % inst["quote"])
    elif inst["kind"] == "metal":
        print("  tick $%s an ounce, 1 lot = %d oz, sized in whole ounces, default spread %s"
              % (inst["tick"], inst["lot"], fmt_dist(inst, inst["spread"])))
        print("  a $1.00 move is $1 an ounce, $%d a lot" % inst["lot"])
    else:
        print("  %d oz a contract, tick $%.2f = $%.2f a contract, whole contracts only"
              % (inst["mult"], inst["tick"], inst["tick"] * inst["mult"]))
        print("  a $1.00 move is $%d a contract" % inst["mult"])
    print("  data: Yahoo %s, or your own bars with --csv" % inst["yahoo"])


def cmd_check(a):
    inst = instrument(a.symbol)
    sess = _session_from(a)
    now = parse_now(a.now, sess["tz"])
    bars, cap, note = load_bars(a, inst, "5d")
    as_of = min(now, cap) if cap and not a.csv else now
    days = sessionize(bars, sess, as_of)
    loc = to_local(now, sess["tz"])
    day = loc.date().isoformat()
    res = evaluate(days.get(day, {}), sess, a.or_minutes, a.side, loc.hour * 60 + loc.minute)
    res.update(symbol=inst["symbol"], date=day, session=sess, now=loc.strftime("%Y-%m-%d %H:%M"), source=note)
    if a.write:
        _write_json(os.path.join(a.dir, "fx-entry.json"), res)
    if a.json:
        print(json.dumps(res, indent=2))
        return
    print("%s, %s (%s time %s)" % (inst["symbol"], session_label(sess), TZ_NAMES[sess["tz"]], res["now"]))
    print("  data: %s" % note)
    if "or_high" in res:
        print("  opening range (%d min): %s to %s, %s tall" % (a.or_minutes, fmt_px(inst, res["or_low"]),
              fmt_px(inst, res["or_high"]), fmt_dist(inst, res["or_high"] - res["or_low"])))
    st = res["status"]
    if st == "triggered":
        print("  TRIGGERED %s at %s on the %s close" % (res["side"].upper(), fmt_px(inst, res["entry"]),
                                                       res["entry_time"]))
        print("  next: fx.py plan, then fx.py replay before you size anything")
    elif st in ("waiting", "expired") and "last" in res:
        print("  %s: last close %s, %s under the high, %s over the low. %s" % (
            st.upper(), fmt_px(inst, res["last"]), fmt_dist(inst, res["to_high"]), fmt_dist(inst, res["to_low"]),
            res.get("reason", "")))
    else:
        print("  %s: %s" % (st.upper(), res.get("reason", "")))
    if a.write:
        print("  wrote %s" % os.path.join(a.dir, "fx-entry.json"))


MAJORS_SCAN = ("EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD", "XAUUSD")


def cmd_scan(a):
    """The opening-range check on a list of markets in one go, triggered first, then nearest to a breakout."""
    sess = _session_from(a)
    now = parse_now(a.now, sess["tz"])
    loc = to_local(now, sess["tz"])
    day = loc.date().isoformat()
    syms = [s.strip() for s in (a.symbols or ",".join(MAJORS_SCAN)).split(",") if s.strip()]
    rows = []
    for n, sym in enumerate(syms):
        inst = instrument(sym)
        try:
            if a.csv_dir:
                path = next((os.path.join(a.csv_dir, inst["symbol"] + ext) for ext in (".csv", ".csv.gz")
                             if os.path.exists(os.path.join(a.csv_dir, inst["symbol"] + ext))), None)
                if not path:
                    raise FXError("no %s.csv in %s" % (inst["symbol"], a.csv_dir))
                bars, cap = parse_csv(path, a.csv_tz), None
            else:
                if n:
                    time.sleep(PAUSE)
                text = fetch_yahoo(inst["yahoo"], "5d")
                bars, meta = parse_yahoo(text, inst["yahoo"])
                cap = meta.get("regularMarketTime")
                cap = utc_of(cap) if isinstance(cap, (int, float)) else None
        except FXError as e:
            rows.append({"symbol": inst["symbol"], "status": "error", "reason": str(e)})
            continue
        as_of = min(now, cap) if cap else now
        res = evaluate(sessionize(bars, sess, as_of).get(day, {}), sess, a.or_minutes, a.side, loc.hour * 60 + loc.minute)
        res["symbol"] = inst["symbol"]
        if "or_high" in res:
            res["or_size"] = fmt_dist(inst, res["or_high"] - res["or_low"])
            if "last" in res:
                near = min(res["to_high"], res["to_low"])
                res["nearest"] = near / (res["or_high"] - res["or_low"]) if res["or_high"] > res["or_low"] else None
                res["near_text"] = "%s to the %s" % (fmt_dist(inst, near), "high" if res["to_high"] <= res["to_low"] else "low")
        rows.append(res)
    order = {"triggered": 0, "waiting": 1, "building": 2, "expired": 3, "not open": 4, "no data": 5, "error": 6}
    rows.sort(key=lambda r: (order.get(r["status"], 9), r.get("nearest") if r.get("nearest") is not None else 9e9))
    out = {"session": sess, "date": day, "now": loc.strftime("%Y-%m-%d %H:%M"), "rows": rows}
    if a.write:
        _write_json(os.path.join(a.dir, "fx-scan.json"), out)
    if a.json:
        print(json.dumps(out, indent=2))
        return
    print("Scan: %s (%s time %s), %d-minute opening range" % (session_label(sess), TZ_NAMES[sess["tz"]], out["now"],
                                                             a.or_minutes))
    for r in rows:
        inst = instrument(r["symbol"])
        if r["status"] == "triggered":
            line = "TRIGGERED %s at %s on the %s close" % (r["side"].upper(), fmt_px(inst, r["entry"]), r["entry_time"])
        elif r.get("near_text"):
            line = "%s, %s" % (r["status"], r["near_text"])
        else:
            line = "%s: %s" % (r["status"], r.get("reason", ""))
        print("  %-7s %-14s %s" % (r["symbol"], ("range " + r["or_size"]) if r.get("or_size") else "", line))
    print("  Next for a triggered market: fx.py check SYMBOL --write, then plan, replay and size.")


def cmd_plan(a):
    saved = {}
    if a.symbol:
        missing = [f for f in ("side", "entry") if getattr(a, f) is None]
        if missing:
            raise FXError("with --symbol also give %s" % ", ".join("--" + m for m in missing))
        if a.side == "both":
            raise FXError("a plan needs one side: --side buy or --side sell")
        inst = instrument(a.symbol)
        side, entry, oh, ol, atr5m = a.side, a.entry, a.or_high, a.or_low, None
        date, entry_time, slot = a.date, a.entry_time, None
        sess = _session_from(a)
    else:
        saved = _read_json(os.path.join(a.dir, "fx-entry.json"), "fx-entry.json (run fx.py check --write)")
        if saved.get("status") != "triggered" and a.entry is None:
            raise FXError("fx-entry.json has not triggered (%s). Plan ahead with --entry PRICE --side buy|sell"
                          % saved.get("status"))
        inst = instrument(saved["symbol"])
        side = a.side if a.side in ("buy", "sell") else saved.get("side")
        entry = a.entry if a.entry is not None else saved["entry"]
        oh, ol, atr5m = saved.get("or_high"), saved.get("or_low"), saved.get("atr5m")
        date, entry_time, slot = saved.get("date"), saved.get("entry_time"), saved.get("entry_slot")
        sess = _session_from(a, saved.get("session"))
    plan = make_plan(inst, side, entry, a.stop, a.target, oh, ol, atr5m, a.spread)
    plan.update(date=date, entry_time=entry_time, entry_slot=slot, session=sess, or_minutes=a.or_minutes,
                flat_by=hhmm(sess["flat"]))
    if a.write:
        _write_json(os.path.join(a.dir, "fx-plan.json"), plan)
    if a.json:
        print(json.dumps(plan, indent=2))
        return
    print("%s %s at %s" % (plan["side"].upper(), inst["symbol"], fmt_px(inst, plan["entry"])))
    print("  stop   %s  (%s, %s risk)" % (fmt_px(inst, plan["stop"]), plan["stop_rule"],
                                         fmt_dist(inst, abs(plan["entry"] - plan["stop"]))))
    print("  target %s  (%gR, %s)" % (fmt_px(inst, plan["target"]), round(plan["target_r"], 2),
                                      fmt_dist(inst, abs(plan["target"] - plan["entry"]))))
    print("  flat by %s %s time, win or lose" % (plan["flat_by"], TZ_NAMES[sess["tz"]]))
    if a.write:
        print("  wrote %s" % os.path.join(a.dir, "fx-plan.json"))


def cmd_size(a):
    cfg = _config(a)
    plan = _read_json(a.plan or os.path.join(a.dir, "fx-plan.json"), "fx-plan.json (run fx.py plan --write)")
    inst = instrument(plan["symbol"])

    def pick(flag, key, default):
        v = getattr(a, flag)
        return v if v is not None else cfg.get(key, default)

    account, risk = pick("account", "account_usd", None), pick("risk", "max_loss_per_trade_usd", None)
    if account is None or risk is None:
        raise FXError("give --account and --risk in dollars (or fx.account_usd and fx.max_loss_per_trade_usd in "
                      "trader/config.json)")
    order = size_position(inst, plan, float(account), float(risk), float(pick("leverage", "leverage", 20.0)),
                          pick("step", "lot_step", None), a.quote_usd, float(pick("max_risk_pct", "max_risk_pct", 2.0)),
                          float(pick("min_rr", "min_reward_risk", 1.0)), a.spread)
    order.update(type="bracket", placed=False, flat_by=plan.get("flat_by"),
                 created_at=datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat())
    if a.write:
        _write_json(os.path.join(a.dir, "fx-order.json"), order)
    if a.json:
        print(json.dumps(order, indent=2))
        return
    size = "%d %s" % (order["units"], order["unit"])
    if order["lots"] is not None:
        size += " (%g lots)" % order["lots"]
    print("%s %s %s, bracket: stop %s, target %s" % (order["side"].upper(), size, inst["symbol"],
          fmt_px(inst, order["stop"]), fmt_px(inst, order["target"])))
    print("  loss at the stop  $%.2f  ($%.2f counting the spread)" % (order["loss_at_stop_usd"],
                                                                     order["loss_with_spread_usd"]))
    print("  gain at the target $%.2f  (reward:risk %.2f)" % (order["reward_at_target_usd"], order["reward_risk"]))
    if "pip_value_usd" in order:
        print("  $%.2f a pip" % order["pip_value_usd"])
    if order["margin_usd"] is not None:
        print("  $%.0f of %s, $%.0f margin at %g:1" % (order["notional_usd"], inst["symbol"], order["margin_usd"],
                                                      order["leverage"]))
    else:
        print("  $%.0f of gold; futures margin is set by your broker, check it" % order["notional_usd"])
    print("  NOT PLACED. This is a ticket for you to read; you place it yourself.")
    if a.write:
        print("  wrote %s" % os.path.join(a.dir, "fx-order.json"))


def cmd_replay(a):
    plan = _read_json(a.plan or os.path.join(a.dir, "fx-plan.json"), "fx-plan.json (run fx.py plan --write)")
    inst = instrument(plan["symbol"])
    sess = _session_from(a, plan.get("session"))
    or_minutes = int(plan.get("or_minutes") or a.or_minutes)
    if plan.get("entry_slot") is not None:
        k = int(plan["entry_slot"])
    elif plan.get("entry_time"):
        k = (parse_hhmm(plan["entry_time"], "entry_time") - sess["open"]) // 5 - 1
    else:
        raise FXError("the plan has no entry time: plan with --entry-time HH:MM (when the entry bar closed)")
    last = sess["slots"] - 1
    if not 0 <= k < last:
        raise FXError("the entry bar is outside the %s session" % sess["name"])
    date = plan.get("date") or "9999-12-31"
    bars, _, note = load_bars(a, inst, "60d")
    days = sessionize(bars, sess, datetime.datetime(9999, 1, 1))
    sessions, short, filled = full_sessions(days, sess, date)
    if len(sessions) < MIN_SESSIONS:
        raise FXError("only %d full %s sessions before %s (need %d): not enough history for fair odds"
                      % (len(sessions), sess["name"], date, MIN_SESSIONS))
    pool = build_pool(sessions, sess["slots"])
    sim = simulate(pool, plan["entry"], plan["stop"], plan["target"], k, last, plan["side"], a.replays, a.seed)
    hist = history_replay(sessions, inst, plan, sess, or_minutes, last)
    out = {"plan": plan, "source": note, "sessions": len(sessions), "skipped": short, "filled_bars": filled,
           "simulation": sim, "history": hist, "verdict": verdict(sim)}
    if a.write:
        _write_json(os.path.join(a.dir, "fx-timemachine.json"), out)
    if a.json:
        print(json.dumps(out, indent=2))
        return
    print("Time Machine: %s %s at %s, stop %s, target %s, flat by %s %s" % (
        plan["side"].upper(), inst["symbol"], fmt_px(inst, plan["entry"]), fmt_px(inst, plan["stop"]),
        fmt_px(inst, plan["target"]), hhmm(sess["flat"]), TZ_NAMES[sess["tz"]]))
    print("  data: %s; %d full %s sessions (%d left out, %d missing bars filled flat)"
          % (note, len(sessions), sess["name"], len(short), filled))
    print("  %d replays (seed %d) of the last %d bars:" % (sim["replays"], sim["seed"], sim["bars_left"]))
    print("    target first %5.1f%%   stop first %5.1f%%   neither %5.1f%%" % (
        100 * sim["p_target"], 100 * sim["p_stop"], 100 * sim["p_neither"]))
    print("    green at the end %.1f%%, average %+.2fR" % (100 * sim["p_green"], sim["avg_R"]))
    print("    price at the flat time: median %s, 5th to 95th percentile %s to %s" % (
        fmt_px(inst, sim["median_close"]), fmt_px(inst, sim["p5_close"]), fmt_px(inst, sim["p95_close"])))
    print("  the same rule on those %d sessions: fired %d times, target %d, stop %d, neither %d%s" % (
        hist["sessions"], hist["triggered"], hist["target"], hist["stop"], hist["close"],
        (", average %+.2fR" % hist["avg_R"]) if hist["avg_R"] is not None else ""))
    print("  verdict: %s" % out["verdict"])
    print("  A simulation from recent bars, not a prediction: rate decisions, data releases and weekend gaps can "
          "do what the sample never did.")


def build_parser():
    p = argparse.ArgumentParser(description="Forex and gold day trading: sessions, breakouts, plans, sizing, replays.")
    p.add_argument("--dir", default="trader", help="where the JSON files live (default trader/)")
    sub = p.add_subparsers(dest="cmd")

    def sess_args(q):
        q.add_argument("--session", choices=sorted(SESSIONS))
        q.add_argument("--open")
        q.add_argument("--flat")
        q.add_argument("--last-entry", dest="last_entry")
        q.add_argument("--tz", choices=sorted(TZ))
        q.add_argument("--or-minutes", dest="or_minutes", type=int, default=15)

    def data_args(q):
        q.add_argument("--csv", help="your own 5-minute bars instead of Yahoo")
        q.add_argument("--csv-tz", dest="csv_tz", default="utc", choices=sorted(TZ))

    q = sub.add_parser("info")
    q.add_argument("symbol")
    q.add_argument("--json", action="store_true")
    q = sub.add_parser("check")
    q.add_argument("symbol")
    q.add_argument("--side", choices=("buy", "sell", "both"), default="both")
    q.add_argument("--now")
    q.add_argument("--write", action="store_true")
    q.add_argument("--json", action="store_true")
    sess_args(q)
    data_args(q)
    q = sub.add_parser("scan")
    q.add_argument("--symbols", help="comma-separated; default the majors plus XAUUSD")
    q.add_argument("--side", choices=("buy", "sell", "both"), default="both")
    q.add_argument("--now")
    q.add_argument("--csv-dir", dest="csv_dir", help="a folder of <SYMBOL>.csv files instead of Yahoo")
    q.add_argument("--csv-tz", dest="csv_tz", default="utc", choices=sorted(TZ))
    q.add_argument("--write", action="store_true")
    q.add_argument("--json", action="store_true")
    sess_args(q)
    q = sub.add_parser("plan")
    q.add_argument("--symbol")
    q.add_argument("--side", choices=("buy", "sell", "both"))
    q.add_argument("--entry", type=float)
    q.add_argument("--or-high", dest="or_high", type=float)
    q.add_argument("--or-low", dest="or_low", type=float)
    q.add_argument("--date")
    q.add_argument("--entry-time", dest="entry_time")
    q.add_argument("--stop", default="or-mid")
    q.add_argument("--target", default="1.5R")
    q.add_argument("--spread", type=float, help="in price units (0.0001 = 1 pip on EURUSD)")
    q.add_argument("--write", action="store_true")
    q.add_argument("--json", action="store_true")
    sess_args(q)
    q = sub.add_parser("size")
    q.add_argument("--plan")
    q.add_argument("--account", type=float)
    q.add_argument("--risk", type=float)
    q.add_argument("--leverage", type=float)
    q.add_argument("--step", type=int, help="size step in units (1000 = micro lots; 1 for brokers like OANDA)")
    q.add_argument("--quote-usd", dest="quote_usd", type=float)
    q.add_argument("--max-risk-pct", dest="max_risk_pct", type=float)
    q.add_argument("--min-rr", dest="min_rr", type=float)
    q.add_argument("--spread", type=float)
    q.add_argument("--write", action="store_true")
    q.add_argument("--json", action="store_true")
    q = sub.add_parser("replay")
    q.add_argument("--plan")
    q.add_argument("--replays", type=int, default=10000)
    q.add_argument("--seed", type=int, default=19)
    q.add_argument("--write", action="store_true")
    q.add_argument("--json", action="store_true")
    sess_args(q)
    data_args(q)
    for q in sub.choices.values():   # --dir works before or after the command
        q.add_argument("--dir", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return 2
    try:
        {"info": cmd_info, "check": cmd_check, "scan": cmd_scan, "plan": cmd_plan, "size": cmd_size,
         "replay": cmd_replay}[a.cmd](a)
    except FXError as e:
        print("fx: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
