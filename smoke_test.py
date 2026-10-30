"""Run once on your machine/VPS BEFORE starting the service. Checks every Alpaca call the bot makes.

    python smoke_test.py            # uses ALPACA_API_KEY / ALPACA_SECRET_KEY from env or .env

Prints PASS/FAIL per step. Needs no market hours except for the live-quote steps (they WARN, not
fail, when the market is closed).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from paper_us.alpaca_data import AlpacaProvider
from paper_us.config import load_config
from paper_us.types import ET

ok = True


def step(name, fn):
    global ok
    try:
        res = fn()
        print(f"PASS  {name}: {res}")
        return res
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"FAIL  {name}: {type(e).__name__}: {e}")
        return None


def main() -> int:
    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        print("Set ALPACA_API_KEY and ALPACA_SECRET_KEY first.")
        return 2
    cfg = load_config()
    p = AlpacaProvider(key, secret, cfg, paper=True)
    now = datetime.now(ET)
    print(f"Now (ET): {now:%Y-%m-%d %H:%M %Z}   feeds: stock={cfg.stock_feed} option={cfg.option_feed}")

    # Most recent trading day for the data checks
    day = now.date()
    sess = None
    for _ in range(7):
        sess = step(f"calendar {day}", lambda d=day: p.session(d))
        if sess:
            break
        day -= timedelta(days=1)
    if sess:
        print(f"      session {sess.open:%H:%M}-{sess.close:%H:%M} ET")

    sym = cfg.universe[0]
    hist = step(f"first-30m volume history {sym}",
                lambda: p.first_window_volumes([sym], now.date(), cfg.rvol_lookback)[sym])
    if hist is not None:
        print(f"      got {len(hist)} sessions (need >= {cfg.rvol_min_days}); newest first: {[int(x) for x in hist[:5]]}")
        if len(hist) < cfg.rvol_min_days:
            print("WARN  too little history: if 0, Alpaca's 30-min bars may not start at 09:30 - tell me and I'll switch to 1-min aggregation")

    price = step(f"latest trade {sym}", lambda: p.latest_price(sym))
    if sess:
        end = min(now, sess.close)
        bars = step(f"1-min bars {sym} today/last session", lambda: p.minute_bars([sym], sess, end)[sym])
        if bars is not None:
            print(f"      {len(bars)} bars" + (f", first {bars[0].ts:%H:%M} last {bars[-1].ts:%H:%M}" if bars else ""))

    if price:
        con = step(f"pick contract {sym} call", lambda: p.pick_contract(sym, True, price, now.date()))
        if con:
            print(f"      {con.symbol} strike {con.strike} expiry {con.expiry} multiplier {con.multiplier}")
            q = step(f"option quote {con.symbol}", lambda: p.option_quote(con.symbol))
            if q is None or not q.valid:
                print("WARN  no valid bid/ask right now (normal outside market hours)")
            else:
                print(f"      bid {q.bid} ask {q.ask} spread {q.spread_pct:.1%}")
    print("\nALL STEPS PASSED" if ok else "\nSOME STEPS FAILED - paste the output to me")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
