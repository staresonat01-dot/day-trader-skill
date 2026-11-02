#!/usr/bin/env python3
"""The Gold & FX watchlist: daily numbers, setup status and price alerts. Standard library only.

    python3 watchlist.py build --raw DIR --spot "XAUUSD=3941.61,EURUSD=1.11978" --setups setups.json --out OUT
    python3 watchlist.py alerts --spot "XAUUSD=3990.10" --setups setups.json [--state alerts-state.json]

`build` reads Alpha Vantage exports saved as files in --raw:
    xau_daily.csv, xag_daily.csv          GOLD_SILVER_HISTORY daily (date,price)
    eurusd_daily.csv, gbpusd_daily.csv,   FX_DAILY (timestamp,open,high,low,close)
    usdjpy_daily.csv
    us10y_daily.csv                       TREASURY_YIELD daily, 10year (timestamp,value)
and writes three row files for the dashboard to OUT: markets.json (one row per market),
history.json (the last 60 closes per market) and setups.json (each setup with its live status).

Alpha Vantage labels each gold and silver value one day late: the value dated D is the close
of the trading day before D (its Saturday value is Friday's close). `build` shifts them back.

`alerts` compares spot prices with each setup's entry zone, stop and target and prints one
line per alert that has not rung before (the state file remembers what rang, per day).
Exit code 0 always; the lines are the output. Not financial advice.
"""

import argparse
import csv
import datetime as dt
import json
import os
import statistics as st
import sys

MARKETS = [
    # symbol, name, file, kind, decimals
    ("XAUUSD", "Gold", "xau_daily.csv", "metal", 2),
    ("XAGUSD", "Silver", "xag_daily.csv", "metal", 3),
    ("EURUSD", "EUR/USD", "eurusd_daily.csv", "fx", 5),
    ("GBPUSD", "GBP/USD", "gbpusd_daily.csv", "fx", 5),
    ("USDJPY", "USD/JPY", "usdjpy_daily.csv", "fx", 3),
    ("US10Y", "US 10-year yield", "us10y_daily.csv", "yield", 2),
]


def parse_spot(text):
    out = {}
    for part in (text or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().upper()] = float(v)
    return out


def prev_weekday(d):
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def read_series(path, kind):
    """[(date, close)] oldest first, weekdays only."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    out = {}
    for r in rows:
        if kind == "metal":
            d, v = dt.date.fromisoformat(r["date"]), r["price"]
            d = prev_weekday(d)              # Alpha Vantage's one-day-late label
        elif kind == "yield":
            d, v = dt.date.fromisoformat(r["timestamp"]), r["value"]
        else:
            d, v = dt.date.fromisoformat(r["timestamp"]), r["close"]
        try:
            out[d] = float(v)
        except (TypeError, ValueError):
            continue                          # "." for a market holiday
    return [(d, out[d]) for d in sorted(out) if d.weekday() < 5]


def rsi(closes, k=14):
    if len(closes) <= k:
        return None
    gains = sum(max(closes[j] - closes[j - 1], 0) for j in range(len(closes) - k, len(closes)))
    losses = sum(max(closes[j - 1] - closes[j], 0) for j in range(len(closes) - k, len(closes)))
    return 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)


def summarize(symbol, name, series, spot, decimals, today):
    if spot is not None and series and series[-1][0] < today:
        series = series + [(today, spot)]
    elif spot is not None and series:
        series = series[:-1] + [(series[-1][0], spot)]
    closes = [c for _, c in series]
    x = closes[-1]

    def sma(k):
        return round(st.mean(closes[-k:]), decimals) if len(closes) >= k else None

    s20, s50, s200 = sma(20), sma(50), sma(200)
    above = [x > s for s in (s20, s50, s200) if s is not None]
    score = sum(above) / len(above) if above else 0.5
    trend = "up" if score == 1 else "down" if score == 0 else "mixed"
    prior = closes[-21:-1] if len(closes) > 20 else closes[:-1]
    row = {
        "symbol": symbol, "name": name, "as_of": series[-1][0].isoformat(),
        "price": round(x, decimals), "prev_close": round(closes[-2], decimals),
        "chg": round(x - closes[-2], decimals), "chg_pct": round((x / closes[-2] - 1) * 100, 2),
        "sma20": s20, "sma50": s50, "sma200": s200,
        "rsi14": round(rsi(closes), 1) if rsi(closes) is not None else None,
        "hi20": round(max(prior), decimals), "lo20": round(min(prior), decimals),
        "chg20_pct": round((x / closes[-21] - 1) * 100, 2) if len(closes) > 20 else None,
        "chg60_pct": round((x / closes[-61] - 1) * 100, 2) if len(closes) > 60 else None,
        "trend": trend, "decimals": decimals,
    }
    hist = [{"symbol": symbol, "date": d.isoformat(), "close": round(c, decimals)} for d, c in series[-60:]]
    return row, hist


def setup_status(s, price):
    """Where the price sits against one setup: past stop, at target, in zone, or how far away."""
    lo, hi = sorted(s["zone"])
    sell = s["side"] == "sell"
    mid = (lo + hi) / 2
    risk = abs(s["stop"] - mid)
    out = dict(s, zone_lo=lo, zone_hi=hi, price=price, rr=round(abs(s["target"] - mid) / risk, 2) if risk else None)
    if (sell and price >= s["stop"]) or (not sell and price <= s["stop"]):
        out.update(status="invalidated", distance=0.0)
    elif (sell and price <= s["target"]) or (not sell and price >= s["target"]):
        out.update(status="at target", distance=0.0)
    elif lo <= price <= hi:
        out.update(status="in zone", distance=0.0)
    else:
        out.update(status="waiting", distance=round(lo - price if price < lo else price - hi, 6))
    return out


def load_setups(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    rows = data["setups"] if isinstance(data, dict) else data
    for s in rows:
        for k in ("symbol", "side", "zone", "stop", "target"):
            if k not in s:
                raise SystemExit("setup %r is missing %s" % (s.get("symbol"), k))
        if s["side"] not in ("buy", "sell"):
            raise SystemExit("setup %s: side must be buy or sell" % s["symbol"])
        lo, hi = sorted(s["zone"])
        if s["side"] == "sell" and not (s["target"] < lo and s["stop"] > hi):
            raise SystemExit("setup %s: a sell needs target < zone < stop" % s["symbol"])
        if s["side"] == "buy" and not (s["target"] > hi and s["stop"] < lo):
            raise SystemExit("setup %s: a buy needs stop < zone < target" % s["symbol"])
    return rows


def cmd_build(a):
    spot = parse_spot(a.spot)
    today = dt.date.fromisoformat(a.today) if a.today else dt.datetime.now(dt.timezone.utc).date()
    markets, history = [], []
    for symbol, name, fname, kind, dec in MARKETS:
        path = os.path.join(a.raw, fname)
        if not os.path.exists(path):
            print("skip %s: no %s" % (symbol, path), file=sys.stderr)
            continue
        row, hist = summarize(symbol, name, read_series(path, kind), spot.get(symbol), dec, today)
        markets.append(row)
        history.extend(hist)
    price = {m["symbol"]: m["price"] for m in markets}
    setups = [setup_status(s, price[s["symbol"]]) for s in load_setups(a.setups) if s["symbol"] in price]
    os.makedirs(a.out, exist_ok=True)
    for name, rows in (("markets", markets), ("history", history), ("setups", setups)):
        with open(os.path.join(a.out, name + ".json"), "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=1)
            fh.write("\n")
    for m in markets:
        print("%-7s %12s %+6.2f%%  trend %-5s RSI %s" % (m["symbol"], m["price"], m["chg_pct"], m["trend"], m["rsi14"]))
    for s in setups:
        print("setup %-7s %-4s %-11s zone %s-%s stop %s target %s" % (
            s["symbol"], s["side"], s["status"], s["zone_lo"], s["zone_hi"], s["stop"], s["target"]))


def cmd_alerts(a):
    spot = parse_spot(a.spot)
    state = {}
    if a.state and os.path.exists(a.state):
        with open(a.state, encoding="utf-8") as fh:
            state = json.load(fh)
    day = a.today or dt.datetime.now(dt.timezone.utc).date().isoformat()
    rang = set(state.get(day, []))
    lines = []
    for s in load_setups(a.setups):
        if s["symbol"] not in spot:
            continue
        r = setup_status(s, spot[s["symbol"]])
        if r["status"] == "waiting":
            continue
        key = "%s:%s" % (s["symbol"], r["status"])
        if key in rang:
            continue
        rang.add(key)
        what = {"in zone": "is IN the %s zone %s-%s (stop %s, target %s)" % (s["side"], r["zone_lo"], r["zone_hi"], s["stop"], s["target"]),
                "invalidated": "went through the stop %s: the %s setup is off" % (s["stop"], s["side"]),
                "at target": "reached the target %s of the %s setup" % (s["target"], s["side"])}[r["status"]]
        lines.append("%s %s %s" % (s["symbol"], spot[s["symbol"]], what))
    if a.state:
        with open(a.state, "w", encoding="utf-8") as fh:
            json.dump({day: sorted(rang)}, fh)
    print("\n".join(lines) if lines else "no alerts")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd")
    b = sub.add_parser("build")
    b.add_argument("--raw", required=True)
    b.add_argument("--spot", default="")
    b.add_argument("--setups", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--today")
    al = sub.add_parser("alerts")
    al.add_argument("--spot", required=True)
    al.add_argument("--setups", required=True)
    al.add_argument("--state")
    al.add_argument("--today")
    a = p.parse_args(argv)
    if a.cmd == "build":
        cmd_build(a)
    elif a.cmd == "alerts":
        cmd_alerts(a)
    else:
        p.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
