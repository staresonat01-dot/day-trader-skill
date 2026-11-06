import contextlib
import io
import os
import random
import shutil
import tempfile
import unittest

from _load import load

mp = load("trader-fx", "midpoint")


def put(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def series(prices):
    """Intraday-looking times, so nothing is dropped as a weekend."""
    return [("2026-01-05 %02d:%02d" % divmod(i, 60), p) for i, p in enumerate(prices)]


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mp.main(argv)
    return code, out.getvalue(), err.getvalue()


class Loading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_alpha_vantage_metal_rows_newest_first_weekends_dropped(self):
        rows = ["date,price"] + ["2026-%02d-%02d,%d" % (1 + i // 28, 1 + i % 28, 100 + i) for i in range(200)][::-1]
        put(os.path.join(self.tmp, "m.csv"), "\n".join(rows) + "\n")
        s = mp.load(os.path.join(self.tmp, "m.csv"))
        self.assertEqual(s[0][0], "2026-01-01")
        self.assertTrue(all(__import__("datetime").date.fromisoformat(r[0]).weekday() < 5 for r in s))
        self.assertEqual([r[1] for r in s], sorted(r[1] for r in s))
        self.assertTrue(all(r[1] == r[2] == r[3] for r in s))          # no wicks: high and low are the close
        with self.assertRaises(mp.MidError):                            # closes only: --wicks cannot work
            mp.load(os.path.join(self.tmp, "m.csv"), wicks=True)

    def test_fx_daily_and_broker_bars(self):
        put(os.path.join(self.tmp, "fx.csv"), "timestamp,open,high,low,close\n" +
            "\n".join("2026-03-%02dT%02d:00:00Z,1,1,1,%.4f" % (1 + i // 24, i % 24, 1.1 + i / 1000) for i in range(40)) + "\n")
        s = mp.load(os.path.join(self.tmp, "fx.csv"))
        self.assertEqual(len(s), 40)                 # intraday rows are all kept, Sunday 1 March included
        self.assertEqual(s[0], ("2026-03-01T00:00:00Z", 1.1, 1.1, 1.1))
        w = mp.load(os.path.join(self.tmp, "fx.csv"), wicks=True)
        self.assertEqual(w[0], ("2026-03-01T00:00:00Z", 1.1, 1.1, 1.0))  # high 1 < close: widened to the close
        put(os.path.join(self.tmp, "bad.csv"), "when,open\n1,2\n")
        with self.assertRaises(mp.MidError):
            mp.load(os.path.join(self.tmp, "bad.csv"))
        put(os.path.join(self.tmp, "short.csv"), "date,price\n2026-01-05,1\n")
        with self.assertRaises(mp.MidError):
            mp.load(os.path.join(self.tmp, "short.csv"))


class Rules(unittest.TestCase):
    def test_rule1_rising_lows_above_mid_then_break_up(self):
        P = [5, 10, 0, 10, 0, 6, 7, 8, 11]          # range 0-10 from bars 1-4, window 6-7-8, then 11
        g = mp.collect(P, rng=4, window=3, horizon=5, retest=2, gap=1)
        self.assertEqual(g["rule1"], [("up", 0.8)])
        self.assertEqual(g["rule2"], [])

    def test_rule2_mirror(self):
        P = [5, 10, 0, 10, 0, 4, 3, 2, -1]
        g = mp.collect(P, rng=4, window=3, horizon=5, retest=2, gap=1)
        self.assertEqual(g["rule2"], [("down", 0.2)])

    def test_rule3_failed_up_cross(self):
        P = [10, 0, 10, 0, 4, 6, 4.5, 4, -1]        # crosses up through 5 at bar 5, back below by bar 7
        g = mp.collect(P, rng=4, window=3, horizon=5, retest=2, gap=1)
        self.assertEqual(g["up_failed"], [("down", 0.4)])
        self.assertEqual(g["up_held"], [])

    def test_score_against_the_random_walk(self):
        ev = [("up", 0.8), ("up", 0.6), ("down", 0.7), ("none", 0.5)]
        r = mp.score(ev, "up")
        self.assertEqual((r["n"], r["breaks"], r["none"]), (4, 3, 1))
        self.assertAlmostEqual(r["right"], 2 / 3)
        self.assertAlmostEqual(r["chance"], 0.7)
        self.assertAlmostEqual(r["edge"], 2 / 3 - 0.7)
        self.assertEqual(r["verdict"], "no edge")
        d = mp.score(ev, "down")
        self.assertAlmostEqual(d["right"], 1 / 3)
        self.assertAlmostEqual(d["chance"], 0.3)
        self.assertEqual(mp.score([("none", 0.5)], "up")["breaks"], 0)
        strong = mp.score([("up", 0.5)] * 60, "up")
        self.assertEqual(strong["verdict"], "beats chance")

    def test_a_random_walk_shows_no_edge_on_the_baseline(self):
        rng = random.Random(3)
        P, p = [], 100.0
        for _ in range(20000):
            p *= 1 + rng.gauss(0, 0.01)
            P.append(p)
        g = mp.collect(P)
        r = mp.score(g["all"], None)
        self.assertLess(abs(r["edge"]), 0.06)
        self.assertGreater(r["breaks"], 1000)


class Wicks(unittest.TestCase):
    P = [5, 10, 0, 10, 0, 6, 7, 8, 11]

    def test_closes_only_matches_the_default(self):
        self.assertEqual(mp.collect(self.P, rng=4, window=3, horizon=5, gap=1),
                         mp.collect(self.P, rng=4, window=3, horizon=5, gap=1, H=list(self.P), L=list(self.P)))

    def test_a_wick_into_the_discount_half_cancels_rule1(self):
        L = list(self.P)
        L[5] = 4.5                                   # the first window bar's low dips under the midpoint 5
        g = mp.collect(self.P, rng=4, window=3, horizon=5, gap=1, H=list(self.P), L=L)
        self.assertEqual(g["rule1"], [])

    def test_wicks_widen_the_range_and_move_the_midpoint(self):
        H = list(self.P)
        H[1] = 12                                    # range 0-12, midpoint 6: the window low of 6 is not above it
        g = mp.collect(self.P, rng=4, window=3, horizon=5, gap=1, H=H, L=list(self.P))
        self.assertEqual(g["rule1"], [])
        H[1] = 11                                    # midpoint 5.5: rule 1 again, the break at 11 is not a close above 11
        g = mp.collect(self.P, rng=4, window=3, horizon=5, gap=1, H=H, L=list(self.P))
        self.assertEqual(g["rule1"], [("none", 8 / 11)])

    def test_state_now_with_wicks(self):
        rows = [(t, p, p + 0.5, p - 0.5) for t, p in series([5, 10, 0, 10, 0, 6, 7, 8])]
        s = mp.state_now(rows, rng=4, window=3, wicks=True)
        self.assertEqual((s["range_low"], s["range_high"], s["midpoint"]), (-0.5, 10.5, 5))
        self.assertTrue(any("lows above the midpoint" in p for p in s["patterns"]))


class Now(unittest.TestCase):
    def test_zone_and_failed_retest(self):
        s = mp.state_now(series([10, 0, 10, 0, 4, 6, 4.5, 4]), rng=4, window=3, retest=2)
        self.assertEqual((s["range_high"], s["range_low"], s["midpoint"]), (10, 0, 5))
        self.assertIn("discount", s["zone"])
        self.assertAlmostEqual(s["position"], 0.4)
        self.assertTrue(any("crossed up" in p and "failed retest" in p for p in s["patterns"]))

    def test_rule1_now_and_above_the_range(self):
        s = mp.state_now(series([5, 10, 0, 10, 0, 6, 7, 8]), rng=4, window=3)
        self.assertTrue(any(p.startswith("rule 1") for p in s["patterns"]))
        s = mp.state_now(series([5, 10, 0, 10, 0, 6, 7, 12]), rng=4, window=3)
        self.assertEqual(s["zone"], "above the range")


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        rng, p, rows = random.Random(5), 100.0, ["time,close"]
        for i in range(800):
            p *= 1 + rng.gauss(0, 0.01)
            rows.append("2026-01-%02d %02d:%02d,%.4f" % (1 + i // 1440, (i // 60) % 24, i % 60, p))
        self.csv = os.path.join(self.tmp, "bars.csv")
        put(self.csv, "\n".join(rows) + "\n")

    def test_test_and_now(self):
        code, out, err = run(["test", self.csv])
        self.assertEqual(code, 0, err)
        self.assertIn("Rule 1: closes above mid, rising lows", out)
        self.assertIn("chance", out)
        code, out, err = run(["now", self.csv, "--price", "1000"])
        self.assertEqual(code, 0, err)
        self.assertIn("above the range", out)
        code, out, err = run(["test", self.csv, "--json"])
        self.assertIn('"groups"', out)
        code, _, err = run(["test", self.csv, "--wicks"])
        self.assertEqual(code, 1)
        self.assertIn("needs high and low columns", err)

    def test_bad_settings_and_files(self):
        self.assertEqual(run(["test", self.csv, "--range", "2"])[0], 2)
        code, _, err = run(["test", os.path.join(self.tmp, "nope.csv")])
        self.assertEqual(code, 1)
        self.assertIn("cannot open", err)


if __name__ == "__main__":
    unittest.main()
