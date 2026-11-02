#!/usr/bin/env python3
"""OANDA order tickets for trader-fx. Standard library only.

    python3 oanda.py ticket                        # trader/fx-order.json as OANDA would get it, in plain words
    python3 oanda.py place --confirm yes           # send it to your OANDA PRACTICE account
    python3 oanda.py place --confirm yes --live    # a live account: needs OANDA_ENV=live as well

Reads trader/fx-order.json (from `fx.py size --write`) and builds one OANDA v20 order with the stop
and the target attached on fill:
    a MARKET order (fill now, at most --max-slippage away from the plan's entry), or
    a LIMIT order at the plan's entry with --limit (good until --gtd-hours, 8 by default).

Safety, in this order:
  1. Nothing is sent without --confirm yes. Claude passes it only after you typed yes in the chat for
     this exact ticket, shown first with `ticket`.
  2. The practice server is the default. A live order needs BOTH OANDA_ENV=live in the environment
     and --live on the command line.
  3. A ticket older than --max-age minutes (15) is refused: re-size it at the current price.
  4. A ticket that was already placed is refused.
  5. Gold futures (GC, MGC) are not OANDA products and are refused.
Credentials come from the environment only, never from the chat or a file in the repo:
    OANDA_TOKEN     your API token (OANDA: Manage API Access)
    OANDA_ACCOUNT   the account id, like 101-001-1234567-001
    OANDA_ENV       practice (default) or live
Every send is appended to trader/fx-fills.json with OANDA's answer. Not financial advice.
"""

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

HOSTS = {"practice": "https://api-fxpractice.oanda.com", "live": "https://api-fxtrade.oanda.com"}
DECIMALS = {"JPY": 3, "XAU": 3, "XAG": 5}


class OandaError(Exception):
    pass


def instrument_name(symbol):
    s = str(symbol).upper()
    if s in ("GC", "MGC"):
        raise OandaError("%s is a COMEX future, which OANDA does not offer. Place it with a futures broker." % s)
    if len(s) != 6:
        raise OandaError("cannot map %r to an OANDA instrument" % symbol)
    return s[:3] + "_" + s[3:]


def price_text(instrument, price):
    base, quote = instrument.split("_")
    dec = DECIMALS.get(quote) if quote == "JPY" else DECIMALS.get(base, 5)
    return "%.*f" % (dec, float(price))


def build_order(o, limit=False, gtd_hours=8.0, max_slippage=None, now=None):
    """The v20 request body for one bracket order from fx-order.json."""
    inst = instrument_name(o["symbol"])
    if o.get("unit") == "contracts":
        raise OandaError("this ticket is sized in futures contracts; OANDA trades units")
    units = int(o["units"]) if o["side"] == "buy" else -int(o["units"])
    order = {
        "instrument": inst, "units": str(units), "positionFill": "DEFAULT",
        "stopLossOnFill": {"price": price_text(inst, o["stop"]), "timeInForce": "GTC"},
        "takeProfitOnFill": {"price": price_text(inst, o["target"]), "timeInForce": "GTC"},
    }
    if limit:
        now = now or dt.datetime.now(dt.timezone.utc)
        until = (now + dt.timedelta(hours=gtd_hours)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
        order.update(type="LIMIT", price=price_text(inst, o["entry"]), timeInForce="GTD", gtdTime=until)
    else:
        order.update(type="MARKET", timeInForce="FOK")
        if max_slippage:
            e = float(o["entry"])
            bound = e + max_slippage if o["side"] == "buy" else e - max_slippage
            order["priceBound"] = price_text(inst, bound)
    return {"order": order}


def describe(o, body, env):
    od = body["order"]
    how = ("a LIMIT order at %s, good until %s" % (od["price"], od["gtdTime"][:16].replace("T", " ") + " UTC")
           if od["type"] == "LIMIT" else "a MARKET order, filled now" +
           (" (no worse than %s)" % od["priceBound"] if "priceBound" in od else ""))
    lines = [
        "%s %s %s on your OANDA %s account, as %s." % (o["side"].upper(), abs(int(od["units"])), od["instrument"],
                                                         env.upper(), how),
        "  stop-loss %s, take-profit %s" % (od["stopLossOnFill"]["price"], od["takeProfitOnFill"]["price"]),
        "  loss at the stop about $%.2f, gain at the target about $%.2f" % (o.get("loss_with_spread_usd", 0),
                                                                          o.get("reward_at_target_usd", 0)),
    ]
    if o.get("flat_by"):
        lines.append("  plan: flat by %s (close it yourself, or ask for the closing ticket)" % o["flat_by"])
    return "\n".join(lines)


def check_ticket(o, max_age_min, now=None):
    if o.get("placed"):
        raise OandaError("this ticket was already placed (%s). Size a new one." % o.get("oanda_order_id", "see fx-fills.json"))
    created = o.get("created_at")
    if not created:
        raise OandaError("the ticket has no created_at time: re-run fx.py size --write")
    age = ((now or dt.datetime.now(dt.timezone.utc)) - dt.datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds() / 60
    if age > max_age_min:
        raise OandaError("the ticket is %d minutes old (limit %d): re-size it at the current price" % (age, max_age_min))


def send(env, account, token, body, opener=urllib.request.urlopen):
    url = "%s/v3/accounts/%s/orders" % (HOSTS[env], account)
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                                          "Accept-Datetime-Format": "RFC3339"})
    try:
        with opener(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            detail = {}
        raise OandaError("OANDA answered HTTP %d: %s" % (e.code, detail.get("errorMessage") or detail.get("rejectReason")
                                                          or "no detail"))
    except (urllib.error.URLError, OSError) as e:
        raise OandaError("could not reach OANDA: %s" % getattr(e, "reason", e))


def summarize_answer(ans):
    fill = ans.get("orderFillTransaction") or {}
    cancel = ans.get("orderCancelTransaction") or {}
    create = ans.get("orderCreateTransaction") or {}
    if cancel:
        return "cancelled", "OANDA cancelled the order: %s" % cancel.get("reason", "no reason given")
    if fill:
        return "filled", "Filled %s at %s (order %s)" % (fill.get("units"), fill.get("price"), create.get("id") or fill.get("orderID"))
    if create:
        return "pending", "Order %s is working at OANDA" % create.get("id")
    return "unknown", "OANDA answered without an order: %s" % json.dumps(ans)[:300]


def main(argv=None, environ=None, opener=urllib.request.urlopen, now=None):
    environ = os.environ if environ is None else environ
    p = argparse.ArgumentParser(description="OANDA order tickets for trader-fx (practice by default).")
    p.add_argument("--dir", default="trader")
    sub = p.add_subparsers(dest="cmd")
    for name in ("ticket", "place"):
        q = sub.add_parser(name)
        q.add_argument("--order", help="default trader/fx-order.json")
        q.add_argument("--limit", action="store_true", help="a limit order at the plan's entry instead of market")
        q.add_argument("--gtd-hours", dest="gtd_hours", type=float, default=8.0)
        q.add_argument("--max-slippage", dest="max_slippage", type=float, help="market orders: in price units")
        q.add_argument("--max-age", dest="max_age", type=int, default=15, help="minutes")
        q.add_argument("--dir", default=argparse.SUPPRESS)
        if name == "place":
            q.add_argument("--confirm", default="")
            q.add_argument("--live", action="store_true")
    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return 2
    path = a.order or os.path.join(a.dir, "fx-order.json")
    env = (environ.get("OANDA_ENV") or "practice").lower()
    try:
        if not os.path.exists(path):
            raise OandaError("no ticket at %s: run fx.py size --write first" % path)
        with open(path, encoding="utf-8") as fh:
            o = json.load(fh)
        if env not in HOSTS:
            raise OandaError("OANDA_ENV must be practice or live, not %r" % env)
        body = build_order(o, a.limit, a.gtd_hours, a.max_slippage, now)
        if a.cmd == "ticket":
            print(describe(o, body, env))
            print(json.dumps(body, indent=2))
            print("Not sent. To send it: type yes in the chat for this exact ticket.")
            return 0
        if a.confirm.strip().lower() != "yes":
            raise OandaError("not sent: --confirm yes is required, and only after you typed yes for this exact ticket")
        if env == "live" and not a.live:
            raise OandaError("OANDA_ENV is live: add --live as well to send a live order")
        if a.live and env != "live":
            raise OandaError("--live given but OANDA_ENV is not live: set OANDA_ENV=live to trade a live account")
        check_ticket(o, a.max_age, now)
        token, account = environ.get("OANDA_TOKEN"), environ.get("OANDA_ACCOUNT")
        if not token or not account:
            raise OandaError("set OANDA_TOKEN and OANDA_ACCOUNT in the environment (never paste them in the chat)")
        print(describe(o, body, env))
        status, ans = send(env, account, token, body, opener)
        state, text = summarize_answer(ans)
        log_path = os.path.join(a.dir, "fx-fills.json")
        log = []
        if os.path.exists(log_path):
            with open(log_path, encoding="utf-8") as fh:
                log = json.load(fh)
        log.append({"at": (now or dt.datetime.now(dt.timezone.utc)).isoformat(), "env": env, "request": body,
                    "http": status, "state": state, "answer": ans})
        os.makedirs(a.dir, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as fh:
            json.dump(log, fh, indent=2)
        if state in ("filled", "pending"):
            o.update(placed=True, oanda_env=env,
                     oanda_order_id=(ans.get("orderCreateTransaction") or {}).get("id"))
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(o, fh, indent=2)
        print(text)
        return 0 if state in ("filled", "pending") else 1
    except OandaError as e:
        print("oanda: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
