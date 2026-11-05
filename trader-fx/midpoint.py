#!/usr/bin/env python3
"""Premium / discount midpoint rules: test them, or read where price stands now. Standard library only.

    python3 midpoint.py test xau_daily.csv                    # 15 years of daily gold, against a random walk
    python3 midpoint.py test eurusd-5m.csv --range 48         # your broker's 5-minute bars (48 bars = 4 hours)
    python3 midpoint.py now xau_daily.csv --price 4126.26     # which half price is in, and any pattern forming

The rules, as popular "smart money" teaching states them:
  1. Higher lows above 50% (bullish): price compresses above the range midpoint without dipping into
     the discount half -> buyers defend fair value, expect a break UP.
  2. Lower highs below 50% (bearish): the mirror -> expect a break DOWN.
  3. Failed midpoint retest (fakeout): price crosses the midpoint but closes back in the other half
     on the retest -> the cross was a trap, expect the move to reverse.

How they are made exact here (closes only; change the numbers with the flags):
  range      the highest and lowest close of the --range bars before the setup window; midpoint = their average
  rule 1     the last --window closes all above the midpoint, inside the range, and the lowest close of the
             window's second half above the lowest of its first half (rising lows)
  rule 2     the mirror below the midpoint, with falling highs
  rule 3     a close crossing the midpoint, then a close back across it within --retest bars (failed) or
             not (held); judged --retest bars after the cross
  outcome    which edge breaks first: the first close beyond the range high or low within --horizon bars

THE BENCHMARK is what makes the test fair. Price high in a range reaches the top first more often even
if it moves at random: a driftless random walk starting a fraction f of the way up a range hits the top
first with probability f. Every group is compared with the average f of its own starting points, so
"edge" is how much better the rule did than chance from the same spot. A 95% interval (Wilson) that
clears the benchmark is flagged; with ~10 groups per run, one flag can be luck.

Input: a CSV with a time column (date, time, timestamp or datetime) and a price column (close, price
or value), any order, newest first or last. Date-only rows on Saturday or Sunday are dropped (Alpha
Vantage's metal series repeats Friday there); intraday rows are all kept. Not financial advice.
"""

import argparse
import csv
import datetime as dt
import json
import math
import statistics as st
import sys

TIME_COLS = ("date", "time", "timestamp", "datetime", "gmt time")
PRICE_COLS = ("close", "price", "value")


class MidError(Exception):
    pass


def load(path):
    """[(time text, close)] oldest first."""
    try:
        fh = open(path, encoding="utf-8-sig", newline="")
    except OSError as e:
        raise MidError("cannot open %s: %s" % (path, e))
    with fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise MidError("%s has no rows" % path)
    keys = {k.strip().lower(): k for k in rows[0] if k}
    tcol = next((keys[k] for k in TIME_COLS if k in keys), None)
    pcol = next((keys[k] for k in PRICE_COLS if k in keys), None)
    if not tcol or not pcol:
        raise MidError("%s needs a time column (%s) and a price column (%s)"
                       % (path, ", ".join(TIME_COLS), ", ".join(PRICE_COLS)))
    out = {}
    for r in rows:
        t = (r.get(tcol) or "").strip()
        try:
            p = float(r.get(pcol))
        except (TypeError, ValueError):
            continue                            # "." for a holiday, a blank line
        if len(t) == 10:
            try:
                if dt.date.fromisoformat(t).weekday() >= 5:
                    continue
            except ValueError:
                pass
        out[t] = p
    series = sorted(out.items())
    if len(series) < 30:
        raise MidError("%s has only %d usable rows" % (path, len(series)))
    return series


def first_break(P, t, hi, lo, horizon):
    for j in range(t + 1, min(len(P), t + 1 + horizon)):
        if P[j] > hi:
            return "up"
        if P[j] < lo:
            return "down"
    return "none"


def wilson(k, n, z=1.96):
    if n == 0:
        return 0.0, 1.0
    p = k / n
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return c - h, c + h


GROUPS = [
    ("all", "Any bar inside the range (baseline)", None),
    ("above", "Inside the range, above the midpoint (baseline)", "up"),
    ("below", "Inside the range, below the midpoint (baseline)", "down"),
    ("rule1", "Rule 1: closes above mid, rising lows", "up"),
    ("rule2", "Rule 2: closes below mid, falling highs", "down"),
    ("up_held", "Rule 3: crossed up, held above mid", "up"),
    ("up_failed", "Rule 3: crossed up, failed back below (trap)", "down"),
    ("down_held", "Rule 3: crossed down, held below mid", "down"),
    ("down_failed", "Rule 3: crossed down, failed back above (trap)", "up"),
]


def collect(P, rng=20, window=5, horizon=30, retest=2, gap=5):
    """Every event as (break side, starting position 0..1 in the range), per group; one event per `gap` bars."""
    groups = {k: [] for k, _, _ in GROUPS}
    last = {k: -10 ** 9 for k in groups}

    def add(k, t, side, pos):
        if t - last[k] >= gap:
            groups[k].append((side, pos))
            last[k] = t

    half = window // 2
    for t in range(rng + window, len(P) - 1):
        base = P[t - window - rng + 1:t - window + 1]
        hi, lo = max(base), min(base)
        if hi <= lo:
            continue
        m = (hi + lo) / 2
        w = P[t - window + 1:t + 1]
        if not all(lo <= x <= hi for x in w):
            continue
        side, pos = first_break(P, t, hi, lo, horizon), (P[t] - lo) / (hi - lo)
        add("all", t, side, pos)
        add("above" if P[t] > m else "below", t, side, pos)
        if all(x > m for x in w) and min(w[half:]) > min(w[:half + 1]):
            add("rule1", t, side, pos)
        if all(x < m for x in w) and max(w[half:]) < max(w[:half + 1]):
            add("rule2", t, side, pos)
    for t in range(rng + 1, len(P) - retest - 1):
        base = P[t - rng:t]
        hi, lo = max(base), min(base)
        if hi <= lo or not lo <= P[t] <= hi:
            continue
        m = (hi + lo) / 2
        j = t + retest
        if not lo <= P[j] <= hi:
            continue
        side, pos = first_break(P, j, hi, lo, horizon), (P[j] - lo) / (hi - lo)
        if P[t - 1] < m < P[t]:
            add("up_failed" if any(P[i] < m for i in range(t + 1, j + 1)) else "up_held", j, side, pos)
        elif P[t - 1] > m > P[t]:
            add("down_failed" if any(P[i] > m for i in range(t + 1, j + 1)) else "down_held", j, side, pos)
    return groups


def score(events, predicts):
    """Share that broke the predicted way vs a random walk from the same starting points."""
    hits = [(s, p) for s, p in events if s != "none"]
    n = len(hits)
    if not n:
        return {"n": len(events), "breaks": 0}
    up = sum(s == "up" for s, _ in hits)
    chance_up = st.mean(p for _, p in hits)
    if predicts == "down":
        k, chance = n - up, 1 - chance_up
    else:
        k, chance = up, chance_up
    lo, hi = wilson(k, n)
    return {"n": len(events), "breaks": n, "right": k / n, "ci": [lo, hi], "chance": chance,
            "edge": k / n - chance, "verdict": "beats chance" if lo > chance else "worse than chance" if hi < chance else "no edge",
            "predicts": predicts or "up", "none": len(events) - n}


def cmd_test(a):
    series = load(a.csv)
    P = [p for _, p in series]
    groups = collect(P, a.range, a.window, a.horizon, a.retest, a.gap)
    res = {k: score(groups[k], pred) for k, _, pred in GROUPS}
    if a.json:
        print(json.dumps({"file": a.csv, "bars": len(P), "from": series[0][0], "to": series[-1][0],
                          "settings": vars(a), "groups": res}, indent=2, default=str))
        return
    print("%s: %d bars, %s to %s" % (a.csv, len(P), series[0][0], series[-1][0]))
    print("range %d bars, setup window %d, retest %d, outcome within %d bars, one event per %d bars"
          % (a.range, a.window, a.retest, a.horizon, a.gap))
    print("%-49s %6s %10s %13s %8s %7s" % ("", "events", "right way", "95% interval", "chance", "edge"))
    for k, label, pred in GROUPS:
        r = res[k]
        if not r.get("breaks"):
            print("%-49s %6d  (no breaks)" % (label, r["n"]))
            continue
        way = "up" if (pred or "up") == "up" else "down"
        flag = "" if r["verdict"] == "no edge" else "  <- " + r["verdict"]
        print("%-49s %6d %6.0f%% %-3s %5.0f-%3.0f%% %7.0f%% %+6.0f%s" % (
            label, r["breaks"], 100 * r["right"], way, 100 * r["ci"][0], 100 * r["ci"][1], 100 * r["chance"],
            100 * r["edge"], flag))
    print("\n'right way' = broke first the way the rule says (baselines: up); 'chance' = a random walk from the same")
    print("starting points. High hit rates near an edge are paid for by small wins and long losses: check reward to")
    print("risk, not the hit rate. Not financial advice.")


def state_now(series, rng=20, window=5, retest=2):
    P = [p for _, p in series]
    t = len(P) - 1
    base = P[t - window - rng + 1:t - window + 1]
    hi, lo = max(base), min(base)
    m = (hi + lo) / 2
    w = P[t - window + 1:t + 1]
    half = window // 2
    x = P[t]
    out = {"time": series[t][0], "price": x, "range_high": hi, "range_low": lo, "midpoint": m,
           "position": (x - lo) / (hi - lo) if hi > lo else None,
           "zone": "above the range" if x > hi else "below the range" if x < lo else
           "in the premium half (above the midpoint)" if x > m else "in the discount half (below the midpoint)",
           "patterns": []}
    if all(lo <= y <= hi for y in w):
        if all(y > m for y in w) and min(w[half:]) > min(w[:half + 1]):
            out["patterns"].append("rule 1: %d closes above the midpoint with rising lows" % window)
        if all(y < m for y in w) and max(w[half:]) < max(w[:half + 1]):
            out["patterns"].append("rule 2: %d closes below the midpoint with falling highs" % window)
    # a cross in the last `retest` bars, judged against the range before it
    for c in range(max(rng + 1, t - retest), t + 1):
        b = P[c - rng:c]
        h2, l2 = max(b), min(b)
        m2 = (h2 + l2) / 2
        if P[c - 1] < m2 < P[c]:
            back = any(P[i] < m2 for i in range(c + 1, t + 1))
            out["patterns"].append("rule 3: crossed up through %.6g (the midpoint of the %d bars before it, %.6g to %.6g) at %s, %s" % (
                m2, rng, l2, h2, series[c][0], "then closed back below (a failed retest)" if back else "holding above so far"))
        elif P[c - 1] > m2 > P[c]:
            back = any(P[i] > m2 for i in range(c + 1, t + 1))
            out["patterns"].append("rule 3: crossed down through %.6g (the midpoint of the %d bars before it, %.6g to %.6g) at %s, %s" % (
                m2, rng, l2, h2, series[c][0], "then closed back above (a failed retest)" if back else "holding below so far"))
    return out


def cmd_now(a):
    series = load(a.csv)
    if a.price is not None:
        series = series + [("now", a.price)]
    s = state_now(series, a.range, a.window, a.retest)
    if a.json:
        print(json.dumps(s, indent=2))
        return
    print("%s, last close %.6g (%s)" % (a.csv, s["price"], s["time"]))
    print("  range of the %d bars before the last %d: %.6g to %.6g, midpoint %.6g" % (
        a.range, a.window, s["range_low"], s["range_high"], s["midpoint"]))
    pos = s["position"]
    print("  price is %s%s" % (s["zone"], (", %.0f%% of the way up" % (100 * pos)) if pos is not None and 0 <= pos <= 1 else ""))
    for p in s["patterns"] or ["no midpoint pattern forming"]:
        print("  " + p)
    if pos is not None and 0 <= pos <= 1:
        print("  by chance alone, price this far up breaks the top first about %.0f%% of the time:" % (100 * pos))
        print("  run `test` on this file to see whether any pattern has done better than that.")


def main(argv=None):
    p = argparse.ArgumentParser(description="Premium/discount midpoint rules: a fair test, and the state now.")
    sub = p.add_subparsers(dest="cmd")
    for name in ("test", "now"):
        q = sub.add_parser(name)
        q.add_argument("csv")
        q.add_argument("--range", type=int, default=20, help="bars that set the range (20)")
        q.add_argument("--window", type=int, default=5, help="bars in the compression window (5)")
        q.add_argument("--retest", type=int, default=2, help="bars to judge a midpoint cross (2)")
        q.add_argument("--json", action="store_true")
        if name == "test":
            q.add_argument("--horizon", type=int, default=30, help="bars to wait for a break (30)")
            q.add_argument("--gap", type=int, default=5, help="bars between counted events (5)")
        else:
            q.add_argument("--price", type=float, help="the current price, added as the latest close")
    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return 2
    if a.range < 3 or a.window < 2 or a.retest < 1:
        print("midpoint: --range must be 3 or more, --window 2 or more, --retest 1 or more", file=sys.stderr)
        return 2
    try:
        (cmd_test if a.cmd == "test" else cmd_now)(a)
    except MidError as e:
        print("midpoint: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
