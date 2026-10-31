# paper-us

Paper-only US stock-option versions of two strategies from `paper-main`:

| Variant | RVOL bar | Exit |
|---|---|---|
| `ORB30_RVOL` | first-30-min volume >= 2.0x 14-day average | candle SL or EOD square-off, no target |
| `ORB30_RVOL_TRAIL` | >= 2.5x | candle SL + $ step lock on option P&L, no target |

Rules (30-minute candles on the underlying, 09:30 ET open; mirrored for puts):
mark the first candle (high/low +/- 0.05%); a later candle that CLOSES beyond it buys the ATM
call/put (nearest expiry >= 2 days out); SL when the stock crosses the breakout candle's low/high
-/+ 0.10%; one trade per stock per day; the first breakout of the day decides the RVOL gate.
Last trigger candle starts 14:30 ET; square-off 15 minutes before the close (early-close days follow
the Alpaca calendar).

Nothing here sends orders. Fills are simulated from option bid/ask (buy at ask, sell at bid) minus a
per-contract fee. Alpaca is used only for data, the market calendar and contract lookup.

## Honest limits

- The numeric defaults (buffers, $100 trail step, spread/premium gates, 0.65 fee) are my translation of
  the NSE rules, not tested on US data. The first weeks of paper results are the test.
- Free data: stock bars are IEX-only (volume is a fraction of consolidated volume; RVOL compares like
  with like so it still works), option quotes are the *indicative* feed (derived from OPRA but not exact).
  Treat spreads/slippage in the P&L as approximate. Exits trigger on the stock price to limit that.
- The Alpaca adapter was written against alpaca-py 0.44 and checked by introspection, but it has not
  been run against the live API from the build environment. Run `python smoke_test.py` first.
- Polling every 10s: SL uses the latest trade plus the high/low of 1-minute bars since entry, so a wick
  between polls is still caught, but the fill is the bid at detection time, not at the SL price.

## QQQ momentum (shares)

A separate service, `paper-us-qqq`, paper trades 50 QQQ shares on the noise-area rule from
`backtest/momentum.py` with the same settings (look 14, vm 1, 30-minute checks). It is long or short,
flat by the close, and sends no orders. Fills are simulated at the last trade when a check runs and at the
last bar's close at EOD, with 0.2bp per side as `fees`. Output: `data/qqq_momentum_trades.csv`,
`logs/qqq_momentum.log` (every check is logged with its bands).

`backtest/mom_parity.py` replays this engine over the backtest's bars. On SIP bars its 437 trades over
2024-11..2026-10 are the backtest's 437. Limits of the live version:

- Bands and prev close use SIP history, but today's bars are free IEX. Over the same 500 days 68 of 437
  trades differ from a SIP-driven run (entry or exit time or side); total P&L was about the same.
- The backtest makes only 5.5bp/trade gross, so fills matter: above 1bp/side the Sharpe drops fast.
- Shorts assume borrow is free and available. Margin and dividends are ignored.

Install: `cp deploy/paper-us-qqq.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now paper-us-qqq`

## Layout

    paper_us/config.py       all tunables (+ env overrides)
    paper_us/engine.py       the strategy logic (pure; takes a DataProvider)
    paper_us/alpaca_data.py  Alpaca data/calendar/contract adapter
    paper_us/runner.py       service loop (waits for session, polls, squares off)
    paper_us/momentum.py     QQQ momentum engine
    paper_us/momentum_runner.py  its service entry point
    smoke_test.py            checks every Alpaca call before you trust the bot
    tests/                   tests, no network
    deploy/paper-us.service  systemd unit
    deploy/paper-us-qqq.service  systemd unit for QQQ momentum

Outputs: `data/orb30_rvol_trades.csv`, `data/orb30_rvol_trail_trades.csv` (one row per trade),
`data/state_*.json` (restart safety), `logs/paper_us.log`.

## Deploy (Hostinger VPS, one step at a time)

1. Alpaca: create a paper account, generate Paper API keys.
2. VPS: `git clone <your-repo> /root/paper-us && cd /root/paper-us`
3. `python3 -m venv venv && venv/bin/pip install -r requirements.txt`
4. `cp .env.example .env`, paste the keys, `chmod 600 .env`
5. `venv/bin/python smoke_test.py` — every line must be PASS (WARNs about quotes are fine off-hours)
6. `cp deploy/paper-us.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now paper-us`
7. `journalctl -u paper-us -f` — expect "paper-us starting" and, at market time, "range locked" lines.

Run the tests anywhere with `pip install -r requirements-dev.txt && pytest`.

## Session times

All times are `America/New_York` via `zoneinfo`, so the Nov 1 DST change needs no action:
09:30 ET is 19:00 IST until Oct 31 and 20:00 IST from Nov 2 (close 02:30 IST).
