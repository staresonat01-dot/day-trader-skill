import contextlib
import datetime
import io
import json
import os
import random
import shutil
import tempfile
import unittest

from _load import load

fx = load("trader-fx", "fx")
UTC = datetime.datetime


def synthetic_bars(days=44, seed=7, start=datetime.date(2026, 9, 1), price=1.1200, sigma=0.00012):
    """Seeded random-walk 5-minute bars around the clock on weekdays. Synthetic, not market data."""
    rng = random.Random(seed)
    bars, d, made, p = [], start, 0, price
    while made < days:
        if d.weekday() < 5:
            t0 = fx.epoch_of(UTC.combine(d, datetime.time(0, 0)))
            for i in range(288):
                o = p
                c = o * (1 + rng.gauss(0, sigma))
                h = max(o, c) * (1 + abs(rng.gauss(0, sigma / 2)))
                l = min(o, c) * (1 - abs(rng.gauss(0, sigma / 2)))
                bars.append({"t": t0 + 300 * i, "o": o, "h": h, "l": l, "c": c})
                p = c
            made += 1
        d += datetime.timedelta(days=1)
    return bars, p


def breakout_day(day, p):
    """One more weekday: quiet until 08:00 UTC, an opening range of 1.11900 to 1.12000, then a close above it."""
    t0 = fx.epoch_of(UTC.combine(day, datetime.time(0, 0)))
    bars = []
    for i in range(288):
        t = t0 + 300 * i
        if i < 96:
            bars.append({"t": t, "o": 1.1195, "h": 1.1197, "l": 1.1193, "c": 1.1195})
        elif i < 99:      # 08:00, 08:05, 08:10: the 15-minute range
            bars.append({"t": t, "o": 1.1195, "h": 1.1200, "l": 1.1190, "c": 1.1196})
        elif i == 99:     # 08:15 closes above 1.12000
            bars.append({"t": t, "o": 1.1196, "h": 1.1207, "l": 1.1195, "c": 1.1205})
        else:
            bars.append({"t": t, "o": 1.1205, "h": 1.1208, "l": 1.1202, "c": 1.1205})
    return bars


def write_csv(path, bars):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("time,open,high,low,close\n")
        for b in bars:
            fh.write("%s,%.6f,%.6f,%.6f,%.6f\n" % (fx.utc_of(b["t"]).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                 b["o"], b["h"], b["l"], b["c"]))


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fx.main(argv)
    return code, out.getvalue(), err.getvalue()


class TimeZones(unittest.TestCase):
    def test_london_summer_time_edges_2026(self):
        self.assertEqual(fx._last_sunday(2026, 3), datetime.date(2026, 3, 29))
        self.assertEqual(fx._last_sunday(2026, 10), datetime.date(2026, 10, 25))
        self.assertEqual(fx.london_offset(UTC(2026, 3, 29, 0, 59)), 0)
        self.assertEqual(fx.london_offset(UTC(2026, 3, 29, 1, 0)), 1)
        self.assertEqual(fx.london_offset(UTC(2026, 10, 25, 0, 59)), 1)
        self.assertEqual(fx.london_offset(UTC(2026, 10, 25, 1, 0)), 0)

    def test_new_york_daylight_time_ends_first_sunday_of_november(self):
        self.assertEqual(fx.newyork_offset(UTC(2026, 11, 1, 5, 59)), -4)
        self.assertEqual(fx.newyork_offset(UTC(2026, 11, 1, 6, 0)), -5)

    def test_local_round_trip(self):
        self.assertEqual(fx.local_to_utc(UTC(2026, 7, 1, 8, 0), "london"), UTC(2026, 7, 1, 7, 0))
        self.assertEqual(fx.local_to_utc(UTC(2026, 11, 2, 8, 0), "london"), UTC(2026, 11, 2, 8, 0))
        self.assertEqual(fx.local_to_utc(UTC(2026, 11, 2, 9, 0), "tokyo"), UTC(2026, 11, 2, 0, 0))
        self.assertEqual(fx.to_local(UTC(2026, 11, 2, 13, 20), "newyork"), UTC(2026, 11, 2, 8, 20))

    def test_sessions(self):
        s = fx.make_session("london")
        self.assertEqual((s["open"], s["flat"], s["last_entry"], s["slots"]), (480, 960, 720, 96))
        s = fx.make_session("comex")
        self.assertEqual((s["tz"], s["open"], s["slots"]), ("newyork", 500, 62))
        with self.assertRaises(fx.FXError):
            fx.make_session("london", open_="08:03")
        with self.assertRaises(fx.FXError):
            fx.make_session("mars")


class Instruments(unittest.TestCase):
    def test_spellings(self):
        for s in ("EURUSD", "eur/usd", "EURUSD=X", "EUR_USD"):
            self.assertEqual(fx.instrument(s)["symbol"], "EURUSD")
        self.assertEqual(fx.instrument("gold")["symbol"], "XAUUSD")
        self.assertEqual(fx.instrument("GC=F")["symbol"], "GC")
        self.assertEqual(fx.instrument("mgc")["mult"], 10)

    def test_pips_and_decimals(self):
        self.assertEqual(fx.instrument("EURUSD")["pip"], 0.0001)
        j = fx.instrument("USDJPY")
        self.assertEqual((j["pip"], j["decimals"], j["yahoo"]), (0.01, 3, "USDJPY=X"))
        self.assertAlmostEqual(fx.instrument("EURGBP")["spread"], 0.0002)
        g = fx.instrument("XAUUSD")
        self.assertEqual((g["kind"], g["lot"], g["yahoo"], g["pip"]), ("metal", 100, "GC=F", None))

    def test_not_an_instrument(self):
        for s in ("TSLA", "EUREUR", "XAUEUR", ""):
            with self.assertRaises(fx.FXError, msg=s):
                fx.instrument(s)

    def test_round_away_and_nearest(self):
        e = fx.instrument("EURUSD")
        self.assertEqual(fx.round_tick(e, 1.119451, "down"), 1.11945)
        self.assertEqual(fx.round_tick(e, 1.119451, "up"), 1.11946)
        self.assertEqual(fx.round_tick(e, 1.122075), 1.12208)
        self.assertEqual(fx.round_tick(fx.instrument("GC"), 3940.04, "down"), 3940.0)

    def test_quote_to_usd(self):
        self.assertEqual(fx.quote_to_usd(fx.instrument("EURUSD"), 1.12), 1.0)
        self.assertAlmostEqual(fx.quote_to_usd(fx.instrument("USDJPY"), 150.0), 1 / 150.0)
        with self.assertRaises(fx.FXError):
            fx.quote_to_usd(fx.instrument("GBPJPY"), 195.0)
        self.assertEqual(fx.quote_to_usd(fx.instrument("GBPJPY"), 195.0, 0.0066), 0.0066)


def session_rows(rows, n_slots=96):
    """{slot: bar} from a list of (o, h, l, c)."""
    return {i: {"o": r[0], "h": r[1], "l": r[2], "c": r[3], "slot": i} for i, r in enumerate(rows[:n_slots])}


class Entry(unittest.TestCase):
    S = fx.make_session("london")
    OR = [(1.1195, 1.1200, 1.1190, 1.1196)] * 3

    def test_triggered_long_on_the_first_close_above(self):
        D = session_rows(self.OR + [(1.1196, 1.1203, 1.1195, 1.1199), (1.1199, 1.1206, 1.1198, 1.1204)])
        r = fx.evaluate(D, self.S, 15, "both", 8 * 60 + 25)
        self.assertEqual((r["status"], r["side"], r["entry"], r["entry_slot"], r["entry_time"]),
                         ("triggered", "buy", 1.1204, 4, "08:25"))
        self.assertEqual((r["or_high"], r["or_low"]), (1.12, 1.119))

    def test_triggered_short_and_side_filter(self):
        D = session_rows(self.OR + [(1.1191, 1.1192, 1.1185, 1.1187)])
        self.assertEqual(fx.evaluate(D, self.S, 15, "both", 500)["side"], "sell")
        self.assertEqual(fx.evaluate(D, self.S, 15, "sell", 500)["status"], "triggered")
        r = fx.evaluate(D, self.S, 15, "buy", 500)
        self.assertEqual(r["status"], "waiting")
        self.assertAlmostEqual(r["to_high"], 0.0013)

    def test_a_wick_through_the_range_is_not_a_close(self):
        D = session_rows(self.OR + [(1.1196, 1.1210, 1.1180, 1.1197)])
        self.assertEqual(fx.evaluate(D, self.S, 15, "both", 500)["status"], "waiting")

    def test_phases(self):
        self.assertEqual(fx.evaluate({}, self.S, 15, "both", 7 * 60)["status"], "not open")
        self.assertEqual(fx.evaluate(session_rows(self.OR[:2]), self.S, 15, "both", 490)["status"], "building")
        self.assertEqual(fx.evaluate(session_rows(self.OR[:2]), self.S, 15, "both", 600)["status"], "no data")
        D = session_rows(self.OR + [(1.1195, 1.1199, 1.1191, 1.1195)] * 60)
        self.assertEqual(fx.evaluate(D, self.S, 15, "both", 12 * 60 + 5)["status"], "expired")

    def test_no_entry_after_the_last_entry_time(self):
        rows = self.OR + [(1.1195, 1.1199, 1.1191, 1.1195)] * 45 + [(1.1196, 1.1210, 1.1195, 1.1209)]
        D = session_rows(rows)   # slot 48 starts at 12:00, the last-entry time
        self.assertEqual(fx.evaluate(D, self.S, 15, "both", 12 * 60 + 10)["status"], "expired")


class Plans(unittest.TestCase):
    E = fx.instrument("EURUSD")

    def test_or_mid_long(self):
        p = fx.make_plan(self.E, "buy", 1.12050, "or-mid", "1.5R", 1.12010, 1.11880)
        self.assertEqual((p["stop"], p["target"]), (1.11945, 1.12208))
        self.assertAlmostEqual(p["risk"], 0.00105)

    def test_or_far_short_rounds_up(self):
        p = fx.make_plan(self.E, "sell", 1.11850, "or-far", "2R", 1.120004, 1.11880)
        self.assertEqual(p["stop"], 1.12001)
        self.assertEqual(p["target"], round(1.11850 - 2 * (1.12001 - 1.11850), 5))

    def test_pips_dist_and_atr(self):
        self.assertEqual(fx.make_plan(self.E, "buy", 1.12, "pips:20", "1R")["stop"], 1.118)
        g = fx.instrument("XAUUSD")
        p = fx.make_plan(g, "sell", 3950.0, "dist:12.5", "2R")
        self.assertEqual((p["stop"], p["target"]), (3962.5, 3925.0))
        self.assertEqual(fx.make_plan(self.E, "buy", 1.12, "atr:2", "1R", atr5m=0.0004)["stop"], 1.1192)
        with self.assertRaises(fx.FXError):
            fx.make_plan(g, "buy", 3950.0, "pips:20")
        with self.assertRaises(fx.FXError):
            fx.make_plan(self.E, "buy", 1.12, "atr:2")

    def test_refusals(self):
        with self.assertRaises(fx.FXError):     # stop 2 pips away: under 3 spreads
            fx.make_plan(self.E, "buy", 1.12, "pips:2")
        with self.assertRaises(fx.FXError):     # range above a long entry
            fx.make_plan(self.E, "buy", 1.1180, "or-mid", "1R", 1.1200, 1.1190)
        with self.assertRaises(fx.FXError):     # target below a long entry
            fx.make_plan(self.E, "buy", 1.12, "pips:20", "price:1.1100")
        with self.assertRaises(fx.FXError):
            fx.make_plan(self.E, "both", 1.12, "pips:20")


class Sizing(unittest.TestCase):
    def plan(self, side, e, s, t):
        return {"side": side, "entry": e, "stop": s, "target": t}

    def test_eurusd_micro_lots(self):
        o = fx.size_position(fx.instrument("EURUSD"), self.plan("buy", 1.12050, 1.11945, 1.12208), 5000, 50)
        self.assertEqual((o["units"], o["lots"]), (43000, 0.43))
        self.assertAlmostEqual(o["loss_at_stop_usd"], 45.15)
        self.assertLessEqual(o["loss_with_spread_usd"], 50)
        self.assertAlmostEqual(o["pip_value_usd"], 4.3)
        self.assertAlmostEqual(o["margin_usd"], 43000 * 1.1205 / 20, places=2)

    def test_usdjpy_converts_yen_to_dollars(self):
        o = fx.size_position(fx.instrument("USDJPY"), self.plan("buy", 150.0, 149.8, 150.3), 5000, 50)
        self.assertEqual(o["units"], 35000)
        self.assertAlmostEqual(o["loss_at_stop_usd"], 35000 * 0.2 / 150, places=2)
        self.assertAlmostEqual(o["notional_usd"], 35000)

    def test_cross_needs_quote_usd(self):
        p = self.plan("sell", 195.0, 195.3, 194.5)
        with self.assertRaises(fx.FXError):
            fx.size_position(fx.instrument("GBPJPY"), p, 5000, 50)
        o = fx.size_position(fx.instrument("GBPJPY"), p, 5000, 50, quote_usd=0.0066)
        self.assertLessEqual(o["loss_with_spread_usd"], 50)
        self.assertGreater(o["units"], 0)

    def test_gold_in_ounces_and_futures_in_contracts(self):
        o = fx.size_position(fx.instrument("XAUUSD"), self.plan("buy", 3950.0, 3940.0, 3965.0), 5000, 50)
        self.assertEqual((o["units"], o["unit"], o["lots"]), (4, "oz", 0.04))
        self.assertAlmostEqual(o["loss_at_stop_usd"], 40.0)
        with self.assertRaises(fx.FXError):    # one micro contract risks $101 at a $10 stop
            fx.size_position(fx.instrument("MGC"), self.plan("buy", 3950.0, 3940.0, 3965.0), 5000, 50)
        o = fx.size_position(fx.instrument("MGC"), self.plan("buy", 3950.0, 3940.0, 3965.0), 10000, 150)
        self.assertEqual((o["units"], o["unit"], o["margin_usd"]), (1, "contracts", None))
        self.assertAlmostEqual(o["loss_at_stop_usd"], 100.0)

    def test_limits(self):
        p = self.plan("buy", 1.12, 1.118, 1.123)
        with self.assertRaises(fx.FXError):     # 5% of the account
            fx.size_position(fx.instrument("EURUSD"), p, 1000, 50)
        with self.assertRaises(fx.FXError):     # reward:risk 0.5
            fx.size_position(fx.instrument("EURUSD"), self.plan("buy", 1.12, 1.118, 1.121), 5000, 50)
        with self.assertRaises(fx.FXError):     # no leverage: $33k of euros on a $5k account
            fx.size_position(fx.instrument("EURUSD"), self.plan("buy", 1.12, 1.1185, 1.122), 5000, 50, leverage=1)


class Data(unittest.TestCase):
    def test_yahoo_shape(self):
        text = json.dumps({"chart": {"result": [{"meta": {"regularMarketTime": 1793606400},
                                                 "timestamp": [1793606100, 1793606400, 1793606700],
                                                 "indicators": {"quote": [{"open": [1.1, None, 1.2],
                                                                           "high": [1.2, 1.3, 1.3],
                                                                           "low": [1.0, 1.1, 1.1],
                                                                           "close": [1.15, 1.2, 1.25]}]}}]}})
        bars, meta = fx.parse_yahoo(text, "EURUSD=X")
        self.assertEqual([b["t"] for b in bars], [1793606100, 1793606700])
        with self.assertRaises(fx.FXError):
            fx.parse_yahoo(json.dumps({"chart": {"result": None, "error": {"description": "No data"}}}), "X")

    def test_csv_times_and_sessions_follow_daylight_time(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "bars.csv")
        with open(path, "w") as fh:
            fh.write("Time,Open,High,Low,Close\n"
                     "2026-07-01T07:00:00Z,1,1,1,1\n"         # 08:00 London in summer
                     "2026-11-02 08:00:00,2,2,2,2\n"           # 08:00 London in winter (naive = UTC)
                     "1793606700,3,3,3,3\n"                    # epoch seconds
                     "2026-11-02T10:00:00+01:00,4,4,4,4\n")    # with an offset
        bars = fx.parse_csv(path)
        self.assertEqual(len(bars), 4)
        days = fx.sessionize(bars, fx.make_session("london"), UTC(2027, 1, 1))
        self.assertEqual(days["2026-07-01"][0]["c"], 1)
        self.assertEqual(days["2026-11-02"][0]["c"], 2)
        self.assertEqual(days["2026-11-02"][0]["start"], "08:00")
        self.assertEqual(fx.parse_csv(path, "london")[0]["t"], fx.epoch_of(UTC(2026, 7, 1, 7, 0)))

    def test_incomplete_and_off_grid_bars_are_dropped(self):
        t = fx.epoch_of(UTC(2026, 11, 2, 8, 0))
        bars = [{"t": t, "o": 1, "h": 1, "l": 1, "c": 1}, {"t": t + 301, "o": 1, "h": 1, "l": 1, "c": 1},
                {"t": t + 300, "o": 1, "h": 1, "l": 1, "c": 1}]
        days = fx.sessionize(bars, fx.make_session("london"), UTC(2026, 11, 2, 8, 7))
        self.assertEqual(list(days["2026-11-02"]), [0])

    def test_full_sessions_fill_small_gaps_only(self):
        S = fx.make_session("london")
        full = {i: {"o": 1, "h": 1, "l": 1, "c": 1} for i in range(96)}
        gap2 = {i: b for i, b in full.items() if i not in (10, 11)}
        gap9 = {i: b for i, b in full.items() if not 20 <= i < 29}
        nofirst = {i: b for i, b in full.items() if i}
        days = {"2026-10-01": full, "2026-10-02": gap2, "2026-10-05": gap9, "2026-10-06": nofirst,
                "2026-10-07": full}
        ok, short, filled = fx.full_sessions(days, S, "2026-10-07")
        self.assertEqual([d for d, _ in ok], ["2026-10-01", "2026-10-02"])
        self.assertEqual((len(short), filled), (2, 2))
        self.assertTrue(ok[1][1][10]["filled"])


class TimeMachine(unittest.TestCase):
    def test_a_steady_rise_hits_a_long_target_and_a_short_stop(self):
        D = [{"o": 1.001 ** i, "h": 1.001 ** (i + 1), "l": 1.001 ** i, "c": 1.001 ** (i + 1)} for i in range(10)]
        pool = fx.build_pool([("d", D)], 10)
        up = fx.simulate(pool, 1.0, 0.998, 1.002, 0, 9, "buy", replays=50)
        self.assertEqual((up["p_target"], up["p_stop"]), (1.0, 0.0))
        dn = fx.simulate(pool, 1.0, 1.002, 0.998, 0, 9, "sell", replays=50)
        self.assertEqual((dn["p_target"], dn["p_stop"], dn["avg_R"]), (0.0, 1.0, -1.0))

    def test_same_seed_same_numbers(self):
        bars, _ = synthetic_bars(days=25)
        S = fx.make_session("london")
        sessions, _, _ = fx.full_sessions(fx.sessionize(bars, S, UTC(2027, 1, 1)), S, "2027-01-01")
        self.assertEqual(len(sessions), 25)
        pool = fx.build_pool(sessions, S["slots"])
        a = fx.simulate(pool, 1.12, 1.119, 1.1215, 4, 95, "buy", replays=2000, seed=3)
        b = fx.simulate(pool, 1.12, 1.119, 1.1215, 4, 95, "buy", replays=2000, seed=3)
        self.assertEqual(a, b)
        self.assertAlmostEqual(a["p_target"] + a["p_stop"] + a["p_neither"], 1.0, places=6)
        self.assertNotEqual(a, fx.simulate(pool, 1.12, 1.119, 1.1215, 4, 95, "buy", replays=2000, seed=4))

    def test_history_replay_on_a_known_session(self):
        S = fx.make_session("london")
        E = fx.instrument("EURUSD")
        rows = [{"o": 1.1195, "h": 1.1200, "l": 1.1190, "c": 1.1196}] * 3 \
            + [{"o": 1.1196, "h": 1.1206, "l": 1.1195, "c": 1.1205}] \
            + [{"o": 1.1205, "h": 1.1230, "l": 1.1204, "c": 1.1225}] \
            + [{"o": 1.1225, "h": 1.1226, "l": 1.1224, "c": 1.1225}] * 91
        plan = {"side": "buy", "entry": 1.1205, "stop": 1.11945, "target": 1.12208, "stop_rule": "or-mid",
                "target_r": 1.5}
        h = fx.history_replay([("2026-10-30", rows)], E, plan, S, 15, 95)
        self.assertEqual((h["triggered"], h["target"]), (1, 1))
        self.assertEqual(h["trades"][0]["time"], "08:20")
        plan["side"] = "sell"
        self.assertEqual(fx.history_replay([("2026-10-30", rows)], E, plan, S, 15, 95)["triggered"], 0)

    def test_verdicts(self):
        base = {"p_neither": 0.1, "avg_R": 0.0, "r_at_target": 1.0}
        self.assertEqual(fx.verdict(dict(base, p_target=0.45, p_stop=0.45)), "Close to a coin flip.")
        self.assertTrue(fx.verdict(dict(base, p_target=0.5, p_stop=0.4, avg_R=0.1)).startswith("Leans toward the target"))
        self.assertTrue(fx.verdict(dict(base, p_target=0.6, p_stop=0.3, avg_R=0.2)).startswith("Strongly toward the target"))
        self.assertIn("hit neither", fx.verdict(dict(base, p_target=0.2, p_stop=0.2, p_neither=0.6)))
        self.assertIn("points the other way", fx.verdict(dict(base, p_target=0.5, p_stop=0.4, avg_R=-0.1)))
        self.assertTrue(fx.verdict(dict(base, p_target=0.2, p_stop=0.7, avg_R=-0.5)).startswith("Strongly toward the stop"))


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        bars, p = synthetic_bars(days=44)          # Sep 1 to Oct 30, every weekday
        bars += breakout_day(datetime.date(2026, 11, 2), p)
        self.csv = os.path.join(self.tmp, "eurusd-5m.csv")
        write_csv(self.csv, bars)
        self.dir = os.path.join(self.tmp, "trader")

    def read(self, name):
        with open(os.path.join(self.dir, name), encoding="utf-8") as fh:
            return json.load(fh)

    def test_check_plan_size_replay(self):
        code, out, err = run(["--dir", self.dir, "check", "EURUSD", "--session", "london", "--csv", self.csv,
                              "--now", "2026-11-02 08:40", "--write"])
        self.assertEqual(code, 0, err)
        self.assertIn("TRIGGERED BUY at 1.12050 on the 08:20 close", out)
        code, out, err = run(["--dir", self.dir, "plan", "--target", "1.5R", "--write"])
        self.assertEqual(code, 0, err)
        plan = self.read("fx-plan.json")    # range 1.11900 to 1.12000: midpoint stop, 10 pips of risk
        self.assertEqual((plan["stop"], plan["target"], plan["entry_slot"]), (1.1195, 1.122, 3))
        code, out, err = run(["--dir", self.dir, "size", "--account", "5000", "--risk", "50", "--write"])
        self.assertEqual(code, 0, err)
        self.assertIn("NOT PLACED", out)
        order = self.read("fx-order.json")  # $50 / (10 pips + 1 pip of spread) = 45,454 -> 45,000 units
        self.assertEqual((order["units"], order["placed"]), (45000, False))
        code, out, err = run(["--dir", self.dir, "replay", "--csv", self.csv, "--replays", "500", "--write"])
        self.assertEqual(code, 0, err)
        tm = self.read("fx-timemachine.json")
        self.assertEqual(tm["sessions"], 44)
        self.assertIn("verdict:", out)

    def test_untriggered_entry_is_refused_without_an_entry_price(self):
        run(["--dir", self.dir, "check", "EURUSD", "--csv", self.csv, "--now", "2026-11-02 08:12", "--write"])
        code, _, err = run(["--dir", self.dir, "plan"])
        self.assertEqual(code, 1)
        self.assertIn("has not triggered", err)

    def test_info(self):
        code, out, _ = run(["info", "XAUUSD"])
        self.assertEqual(code, 0)
        self.assertIn("1 lot = 100 oz", out)


if __name__ == "__main__":
    unittest.main()
