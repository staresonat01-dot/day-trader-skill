import contextlib
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest

from _load import load
import test_fx as base

fx = load("trader-fx", "fx")
wl = load("trader-fx", "watchlist")
oanda = load("trader-fx", "oanda")
UTC = datetime.datetime


def put(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def put_json(path, obj):
    put(path, json.dumps(obj))


def get_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def run(fn, argv, **kw):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn(argv, **kw)
    return code, out.getvalue(), err.getvalue()


class Scan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        bars, p = base.synthetic_bars(days=3, start=datetime.date(2026, 10, 28))
        base.write_csv(os.path.join(self.tmp, "EURUSD.csv"), bars + base.breakout_day(datetime.date(2026, 11, 2), p))
        quiet = [dict(b, o=1.1195, h=1.11955, l=1.11945, c=1.1195) for b in base.breakout_day(datetime.date(2026, 11, 2), p)]
        base.write_csv(os.path.join(self.tmp, "GBPUSD.csv"), quiet)

    def test_triggered_first_then_waiting_then_missing(self):
        code, out, err = base.run(["--dir", self.tmp, "scan", "--symbols", "GBPUSD,EURUSD,USDJPY", "--csv-dir", self.tmp,
                                   "--now", "2026-11-02 08:40", "--json"])
        self.assertEqual(code, 0, err)
        rows = json.loads(out)["rows"]
        self.assertEqual([r["symbol"] for r in rows], ["EURUSD", "GBPUSD", "USDJPY"])
        self.assertEqual([r["status"] for r in rows], ["triggered", "waiting", "error"])
        self.assertIn("no USDJPY.csv", rows[2]["reason"])

    def test_text_output(self):
        code, out, _ = base.run(["scan", "--symbols", "EURUSD", "--csv-dir", self.tmp, "--now", "2026-11-02 08:40"])
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED BUY at 1.12050", out)


class Watchlist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        raw = os.path.join(self.tmp, "raw")
        os.makedirs(raw)
        d0 = datetime.date(2026, 8, 3)
        gold, fxr = ["date,price"], ["timestamp,open,high,low,close"]
        d, i = d0, 0
        while d <= datetime.date(2026, 11, 1):         # every calendar day, like Alpha Vantage's metals feed
            gold.append("%s,%.2f" % (d, 4000 + i))
            if d.weekday() < 5:
                fxr.append("%s,1.1,1.2,1.0,%.5f" % (d, 1.1 + i / 10000.0))
            d += datetime.timedelta(days=1)
            i += 1
        put(os.path.join(raw, "xau_daily.csv"), "\n".join(gold) + "\n")
        put(os.path.join(raw, "eurusd_daily.csv"), "\n".join(fxr) + "\n")
        self.raw = raw
        self.setups = os.path.join(self.tmp, "setups.json")
        put_json(self.setups, {"setups": [
            {"symbol": "XAUUSD", "side": "sell", "zone": [4200, 4210], "stop": 4260, "target": 4100},
            {"symbol": "EURUSD", "side": "buy", "zone": [1.1050, 1.1060], "stop": 1.1000, "target": 1.1200},
        ]})

    def test_metal_dates_shift_back_one_trading_day(self):
        s = wl.read_series(os.path.join(self.raw, "xau_daily.csv"), "metal")
        last = dict(s)
        # the value dated Mon Nov 2 is absent; Sat Oct 31 and Sun Nov 1 both fold into Fri Oct 30 (the later wins)
        self.assertEqual(s[-1][0], datetime.date(2026, 10, 30))
        self.assertEqual(last[datetime.date(2026, 10, 30)], 4000 + (datetime.date(2026, 11, 1) - datetime.date(2026, 8, 3)).days)
        self.assertTrue(all(d.weekday() < 5 for d, _ in s))

    def test_build_writes_three_files_with_status(self):
        out = os.path.join(self.tmp, "out")
        with contextlib.redirect_stdout(io.StringIO()):
            wl.main(["build", "--raw", self.raw, "--spot", "XAUUSD=4205,EURUSD=1.1300", "--setups", self.setups,
                     "--out", out, "--today", "2026-11-02"])
        m = {r["symbol"]: r for r in get_json(os.path.join(out, "markets.json"))}
        self.assertEqual(m["XAUUSD"]["price"], 4205)
        self.assertEqual(m["XAUUSD"]["as_of"], "2026-11-02")
        self.assertEqual(m["XAUUSD"]["trend"], "up")
        s = {r["symbol"]: r for r in get_json(os.path.join(out, "setups.json"))}
        self.assertEqual(s["XAUUSD"]["status"], "in zone")
        self.assertEqual(s["EURUSD"]["status"], "at target")
        self.assertAlmostEqual(s["XAUUSD"]["rr"], round(105 / 55, 2))
        h = get_json(os.path.join(out, "history.json"))
        self.assertEqual(len([r for r in h if r["symbol"] == "XAUUSD"]), 60)

    def test_status_rules(self):
        s = {"symbol": "X", "side": "sell", "zone": [10, 11], "stop": 12, "target": 8}
        self.assertEqual(wl.setup_status(s, 9.5)["status"], "waiting")
        self.assertAlmostEqual(wl.setup_status(s, 9.5)["distance"], 0.5)
        self.assertEqual(wl.setup_status(s, 10.5)["status"], "in zone")
        self.assertEqual(wl.setup_status(s, 12)["status"], "invalidated")
        self.assertEqual(wl.setup_status(s, 7.9)["status"], "at target")

    def test_alerts_ring_once_per_day(self):
        state = os.path.join(self.tmp, "state.json")
        args = ["alerts", "--spot", "XAUUSD=4205", "--setups", self.setups, "--state", state, "--today", "2026-11-02"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            wl.main(args)
        self.assertIn("XAUUSD 4205.0 is IN the sell zone", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            wl.main(args)
        self.assertEqual(out.getvalue().strip(), "no alerts")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            wl.main(args[:-1] + ["2026-11-03"])
        self.assertIn("IN the sell zone", out.getvalue())

    def test_bad_setup_is_refused(self):
        bad = os.path.join(self.tmp, "bad.json")
        put_json(bad, [{"symbol": "XAUUSD", "side": "sell", "zone": [4200, 4210], "stop": 4100, "target": 4300}])
        with self.assertRaises(SystemExit):
            wl.load_setups(bad)

    def test_shipped_setups_are_valid(self):
        rows = wl.load_setups(os.path.join(base.fx.__file__.rsplit(os.sep, 1)[0], "setups.json"))
        self.assertEqual({r["symbol"] for r in rows}, {"XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY"})


class FakeResponse(object):
    def __init__(self, status, body):
        self.status, self.body = status, body

    def read(self):
        return json.dumps(self.body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Oanda(unittest.TestCase):
    NOW = datetime.datetime(2026, 11, 2, 9, 0, tzinfo=datetime.timezone.utc)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.order = {"symbol": "EURUSD", "side": "sell", "units": 45000, "unit": "units", "entry": 1.1255,
                      "stop": 1.1315, "target": 1.114, "loss_with_spread_usd": 49.5, "reward_at_target_usd": 51.75,
                      "placed": False, "created_at": "2026-11-02T08:55:00+00:00", "flat_by": "16:00"}
        self.write()
        self.calls = []

    def write(self):
        put_json(os.path.join(self.tmp, "fx-order.json"), self.order)

    def opener(self, answer):
        def fake(req, timeout=None):
            self.calls.append(req)
            return FakeResponse(201, answer)
        return fake

    def place(self, extra=(), env=None, answer=None):
        env = {"OANDA_TOKEN": "tok", "OANDA_ACCOUNT": "101-001-1-001"} if env is None else env
        return run(oanda.main, ["--dir", self.tmp, "place"] + list(extra), environ=env,
                   opener=self.opener(answer or {"orderCreateTransaction": {"id": "77"},
                                                 "orderFillTransaction": {"units": "-45000", "price": "1.12548"}}),
                   now=self.NOW)

    def test_body_market_and_limit(self):
        b = oanda.build_order(self.order)["order"]
        self.assertEqual((b["type"], b["instrument"], b["units"], b["timeInForce"]), ("MARKET", "EUR_USD", "-45000", "FOK"))
        self.assertEqual((b["stopLossOnFill"]["price"], b["takeProfitOnFill"]["price"]), ("1.13150", "1.11400"))
        b = oanda.build_order(self.order, limit=True, now=self.NOW)["order"]
        self.assertEqual((b["type"], b["price"], b["gtdTime"]), ("LIMIT", "1.12550", "2026-11-02T17:00:00.000000000Z"))
        b = oanda.build_order(dict(self.order, symbol="USDJPY", side="buy", entry=156.3, stop=155.5, target=158.4),
                              max_slippage=0.05)["order"]
        self.assertEqual((b["units"], b["priceBound"], b["stopLossOnFill"]["price"]), ("45000", "156.350", "155.500"))
        self.assertEqual(oanda.price_text("XAU_USD", 3990), "3990.000")

    def test_futures_refused(self):
        with self.assertRaises(oanda.OandaError):
            oanda.build_order(dict(self.order, symbol="MGC", unit="contracts"))

    def test_ticket_sends_nothing(self):
        code, out, _ = run(oanda.main, ["--dir", self.tmp, "ticket"], environ={}, opener=self.opener({}), now=self.NOW)
        self.assertEqual(code, 0)
        self.assertIn("SELL 45000 EUR_USD on your OANDA PRACTICE account", out)
        self.assertEqual(self.calls, [])

    def test_refusals_send_nothing(self):
        self.assertIn("--confirm yes is required", self.place()[2])
        self.assertIn("add --live", self.place(["--confirm", "yes"], env={"OANDA_ENV": "live", "OANDA_TOKEN": "t", "OANDA_ACCOUNT": "a"})[2])
        self.assertIn("OANDA_ENV is not live", self.place(["--confirm", "yes", "--live"])[2])
        self.assertIn("OANDA_TOKEN", self.place(["--confirm", "yes"], env={})[2])
        self.order["created_at"] = "2026-11-02T08:00:00+00:00"
        self.write()
        self.assertIn("60 minutes old", self.place(["--confirm", "yes"])[2])
        self.assertEqual(self.calls, [])

    def test_place_practice_and_log(self):
        code, out, err = self.place(["--confirm", "yes"])
        self.assertEqual(code, 0, err)
        self.assertIn("Filled -45000 at 1.12548", out)
        req = self.calls[0]
        self.assertEqual(req.full_url, "https://api-fxpractice.oanda.com/v3/accounts/101-001-1-001/orders")
        self.assertEqual(req.get_header("Authorization"), "Bearer tok")
        self.assertEqual(json.loads(req.data)["order"]["units"], "-45000")
        saved = get_json(os.path.join(self.tmp, "fx-order.json"))
        self.assertEqual((saved["placed"], saved["oanda_order_id"]), (True, "77"))
        self.assertEqual(get_json(os.path.join(self.tmp, "fx-fills.json"))[0]["state"], "filled")
        self.assertIn("already placed", self.place(["--confirm", "yes"])[2])
        self.assertEqual(len(self.calls), 1)

    def test_live_goes_to_the_live_host(self):
        code, _, err = self.place(["--confirm", "yes", "--live"], env={"OANDA_ENV": "live", "OANDA_TOKEN": "t", "OANDA_ACCOUNT": "a"})
        self.assertEqual(code, 0, err)
        self.assertTrue(self.calls[0].full_url.startswith("https://api-fxtrade.oanda.com/"))

    def test_cancelled_order_is_not_marked_placed(self):
        code, out, _ = self.place(["--confirm", "yes"], answer={"orderCreateTransaction": {"id": "9"},
                                                                 "orderCancelTransaction": {"reason": "BOUNDS_VIOLATION"}})
        self.assertEqual(code, 1)
        self.assertIn("BOUNDS_VIOLATION", out)
        self.assertFalse(get_json(os.path.join(self.tmp, "fx-order.json"))["placed"])


if __name__ == "__main__":
    unittest.main()
