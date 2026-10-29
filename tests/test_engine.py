import csv
from dataclasses import replace

import pytest
from conftest import CLOSE, DAY, OPEN, FakeProvider, day_bars, mk_bars, t

from paper_us.config import Variant
from paper_us.engine import Orb30Engine
from paper_us.store import Store
from paper_us.types import Quote, Session

RVOL = Variant("ORB30_RVOL", 2.0, None)
TRAIL = Variant("ORB30_RVOL_TRAIL", 2.5, 100.0)
CE = "AAA261106C00200000"


def engine(variant, cfg, prov, store):
    e = Orb30Engine(variant, cfg, prov, store)
    e.start_day(prov.session(DAY))
    return e


def rows(store, variant):
    p = store.trades_path(variant.name)
    try:
        return list(csv.DictReader(open(p)))
    except FileNotFoundError:
        return []


def setup_breakout(prov, rvol_avg_vol=1000.0, direction="up"):
    """Avg first-window volume history = rvol_avg_vol*30/day... first window total = 30*1000 = 30000."""
    prov.bars["AAA"] = day_bars(breakout=direction)
    prov.hist["AAA"] = [rvol_avg_vol] * 14
    prov.price["AAA"] = 102.0 if direction == "up" else 98.0


def test_rvol_gate_blocks_low_volume_and_marks_day_done(cfg, store, prov):
    setup_breakout(prov, rvol_avg_vol=20000.0)               # rvol = 30000/20000 = 1.5 < 2.0
    e = engine(RVOL, cfg, prov, store)
    e.poll(t(10, 31), prov.minute_bars(["AAA"], prov.session(DAY), t(10, 31)))
    st = e.state["AAA"]
    assert st.pos is None and st.signal is None and st.done
    assert "rvol_gate" in st.done_reason and rows(store, RVOL) == []


def test_variants_use_different_rvol_bars(cfg, store, prov):
    setup_breakout(prov, rvol_avg_vol=12000.0)               # rvol = 2.5 -> passes both (>=)
    a = engine(RVOL, cfg, prov, store)
    setup2 = FakeProvider(); setup2.bars = prov.bars; setup2.hist = {"AAA": [14000.0] * 14}  # rvol 2.14
    setup2.price = prov.price
    b = Orb30Engine(TRAIL, cfg, setup2, store); b.start_day(setup2.session(DAY))
    bars = prov.minute_bars(["AAA"], prov.session(DAY), t(10, 31))
    a.poll(t(10, 31), bars)
    b.poll(t(10, 31), bars)
    assert a.state["AAA"].pos is not None                    # 2.5 >= 2.0
    assert b.state["AAA"].pos is None and b.state["AAA"].done   # 2.14 < 2.5


def test_no_history_means_no_trade(cfg, store, prov):
    setup_breakout(prov)
    prov.hist["AAA"] = [1000.0] * 5                          # < rvol_min_days (10)
    e = engine(RVOL, cfg, prov, store)
    e.poll(t(10, 31), prov.minute_bars(["AAA"], prov.session(DAY), t(10, 31)))
    assert e.state["AAA"].pos is None and e.state["AAA"].done


def test_call_entry_fills_at_ask_with_expected_sl(cfg, store, prov):
    setup_breakout(prov, rvol_avg_vol=1000.0)                # rvol = 30
    e = engine(RVOL, cfg, prov, store)
    e.poll(t(10, 31), prov.minute_bars(["AAA"], prov.session(DAY), t(10, 31)))
    pos = e.state["AAA"].pos
    assert pos is not None and pos.is_call and pos.entry_px == 5.00 and pos.qty == 1
    assert pos.contract == CE
    # SL = breakout-candle low - 0.10%: candle low is min(l) of the 10:00 bars
    c_low = min(b.l for b in prov.bars["AAA"][30:60])
    assert pos.sl_level == pytest.approx(c_low - 0.001 * c_low)


def test_put_entry_on_breakdown(cfg, store, prov):
    setup_breakout(prov, direction="down")
    e = engine(RVOL, cfg, prov, store)
    e.poll(t(10, 31), prov.minute_bars(["AAA"], prov.session(DAY), t(10, 31)))
    pos = e.state["AAA"].pos
    assert pos is not None and not pos.is_call and pos.contract.endswith("P00200000")
    assert pos.sl_level > max(b.h for b in prov.bars["AAA"][30:60])


def test_sl_exit_books_bid_net_of_fees_and_one_trade_per_day(cfg, store, prov):
    setup_breakout(prov)
    e = engine(RVOL, cfg, prov, store)
    e.poll(t(10, 31), prov.minute_bars(["AAA"], prov.session(DAY), t(10, 31)))
    assert e.state["AAA"].pos is not None
    # spot collapses through SL; option bid drops to 3.00
    prov.price["AAA"] = 95.0
    prov.quotes[CE] = Quote(bid=3.00, ask=3.10)
    e.poll(t(10, 40), prov.minute_bars(["AAA"], prov.session(DAY), t(10, 40)))
    r = rows(store, RVOL)
    assert len(r) == 1 and r[0]["reason"] == "SL"
    assert float(r[0]["exit_px"]) == 3.00
    assert float(r[0]["gross_pnl"]) == pytest.approx(-200.0)
    assert float(r[0]["fees"]) == pytest.approx(1.30)
    assert float(r[0]["net_pnl"]) == pytest.approx(-201.30)
    # a later breakout candle must not re-enter (one trade per stock per day)
    prov.bars["AAA"] += mk_bars(t(10, 30), [103.0 + 0.1 * i for i in range(30)], vol=1000)
    prov.price["AAA"] = 106.0
    prov.quotes[CE] = Quote(bid=4.9, ask=5.0)
    e.poll(t(11, 1), prov.minute_bars(["AAA"], prov.session(DAY), t(11, 1)))
    assert e.state["AAA"].pos is None and len(rows(store, RVOL)) == 1


def test_sl_detected_from_bar_extreme_between_polls(cfg, store, prov):
    setup_breakout(prov)
    e = engine(RVOL, cfg, prov, store)
    e.poll(t(10, 31), prov.minute_bars(["AAA"], prov.session(DAY), t(10, 31)))
    sl = e.state["AAA"].pos.sl_level
    # a minute bar after entry wicks below the SL, but the latest trade has recovered
    prov.bars["AAA"].append(mk_bars(t(10, 33), [101.0])[0].__class__(
        ts=t(10, 33), o=101.0, h=101.5, l=sl - 0.5, c=101.0, v=1000))
    prov.price["AAA"] = 101.0
    e.poll(t(10, 34), prov.minute_bars(["AAA"], prov.session(DAY), t(10, 34)))
    assert rows(store, RVOL)[0]["reason"] == "SL"


def test_no_trail_variant_holds_through_profit_and_exits_eod(cfg, store, prov):
    setup_breakout(prov)
    e = engine(RVOL, cfg, prov, store)
    e.poll(t(10, 31), prov.minute_bars(["AAA"], prov.session(DAY), t(10, 31)))
    prov.quotes[CE] = Quote(bid=9.0, ask=9.1)                # +$400 gross
    e.poll(t(11, 0), prov.minute_bars(["AAA"], prov.session(DAY), t(11, 0)))
    prov.quotes[CE] = Quote(bid=5.0, ask=5.1)                # back to flat: no trail -> still open
    e.poll(t(11, 5), prov.minute_bars(["AAA"], prov.session(DAY), t(11, 5)))
    assert e.state["AAA"].pos is not None
    prov.quotes[CE] = Quote(bid=6.0, ask=6.1)
    e.poll(t(15, 45), prov.minute_bars(["AAA"], prov.session(DAY), t(15, 45)))
    r = rows(store, RVOL)
    assert len(r) == 1 and r[0]["reason"] == "EOD" and float(r[0]["exit_px"]) == 6.0


def test_trail_step_lock_exits_when_pnl_falls_to_locked_level(cfg, store, prov):
    setup_breakout(prov)
    e = engine(TRAIL, cfg, prov, store)
    e.poll(t(10, 31), prov.minute_bars(["AAA"], prov.session(DAY), t(10, 31)))
    # entry 5.00, step $100 = +$1.00 premium. Peak +$250 -> 2 steps -> lock at +$100
    prov.quotes[CE] = Quote(bid=7.5, ask=7.6)
    e.poll(t(11, 0), prov.minute_bars(["AAA"], prov.session(DAY), t(11, 0)))
    assert e.state["AAA"].pos.peak_gross == pytest.approx(250.0)
    prov.quotes[CE] = Quote(bid=6.2, ask=6.3)                # +$120 > lock(+$100): still open
    e.poll(t(11, 5), prov.minute_bars(["AAA"], prov.session(DAY), t(11, 5)))
    assert e.state["AAA"].pos is not None
    prov.quotes[CE] = Quote(bid=5.9, ask=6.0)                # +$90 <= +$100: lock hit
    e.poll(t(11, 10), prov.minute_bars(["AAA"], prov.session(DAY), t(11, 10)))
    r = rows(store, TRAIL)
    assert len(r) == 1 and r[0]["reason"] == "TRAIL_LOCK" and float(r[0]["gross_pnl"]) == pytest.approx(90.0)


def test_trail_one_step_locks_breakeven(cfg, store, prov):
    setup_breakout(prov)
    e = engine(TRAIL, cfg, prov, store)
    e.poll(t(10, 31), prov.minute_bars(["AAA"], prov.session(DAY), t(10, 31)))
    prov.quotes[CE] = Quote(bid=6.2, ask=6.3)                # peak +$120 -> 1 step -> lock at 0
    e.poll(t(11, 0), prov.minute_bars(["AAA"], prov.session(DAY), t(11, 0)))
    prov.quotes[CE] = Quote(bid=5.0, ask=5.1)                # back to entry price: lock hit
    e.poll(t(11, 5), prov.minute_bars(["AAA"], prov.session(DAY), t(11, 5)))
    assert rows(store, TRAIL)[0]["reason"] == "TRAIL_LOCK"


def test_wide_spread_rejected_then_signal_expires(cfg, store, prov):
    setup_breakout(prov)
    prov.quote_default = Quote(bid=4.0, ask=5.0)             # 22% spread
    e = engine(RVOL, cfg, prov, store)
    e.poll(t(10, 31), prov.minute_bars(["AAA"], prov.session(DAY), t(10, 31)))
    assert e.state["AAA"].pos is None and e.state["AAA"].signal is not None
    e.poll(t(10, 33), prov.minute_bars(["AAA"], prov.session(DAY), t(10, 33)))   # > 90s later
    assert e.state["AAA"].signal is None and not e.state["AAA"].done
    # spread heals; a later breakout candle can still trade
    prov.quote_default = Quote(bid=4.9, ask=5.0)
    prov.bars["AAA"] += mk_bars(t(10, 30), [103.0 + 0.1 * i for i in range(30)], vol=1000)
    prov.price["AAA"] = 106.0
    e.poll(t(11, 1), prov.minute_bars(["AAA"], prov.session(DAY), t(11, 1)))
    assert e.state["AAA"].pos is not None


def test_signal_dropped_if_spot_already_through_sl(cfg, store, prov):
    setup_breakout(prov)
    prov.price["AAA"] = 90.0
    e = engine(RVOL, cfg, prov, store)
    e.poll(t(10, 31), prov.minute_bars(["AAA"], prov.session(DAY), t(10, 31)))
    assert e.state["AAA"].pos is None and e.state["AAA"].signal is None


def test_stale_breakout_after_restart_is_skipped(cfg, store, prov):
    setup_breakout(prov)
    e = engine(RVOL, cfg, prov, store)
    e.poll(t(13, 0), prov.minute_bars(["AAA"], prov.session(DAY), t(13, 0)))   # candle ended 10:30, 2.5h ago
    assert e.state["AAA"].pos is None and e.state["AAA"].signal is None


def test_restart_restores_open_position_and_does_not_double_enter(cfg, store, prov):
    setup_breakout(prov)
    e1 = engine(RVOL, cfg, prov, store)
    e1.poll(t(10, 31), prov.minute_bars(["AAA"], prov.session(DAY), t(10, 31)))
    assert e1.state["AAA"].pos is not None
    e2 = engine(RVOL, cfg, prov, store)                       # "restart"
    pos = e2.state["AAA"].pos
    assert pos is not None and pos.contract == CE and pos.entry_px == 5.0
    assert pos.entry_ts == e1.state["AAA"].pos.entry_ts
    e2.poll(t(10, 32), prov.minute_bars(["AAA"], prov.session(DAY), t(10, 32)))
    assert len(prov.contract_calls) == 1                      # no second entry attempt


def test_half_day_squareoff_and_last_entry_follow_session_close(cfg, store):
    half = Session(DAY, OPEN, DAY and OPEN.replace(hour=13, minute=0))
    prov = FakeProvider(half)
    e = Orb30Engine(RVOL, cfg, prov, store)
    e.start_day(half)
    assert e.eod_dt == OPEN.replace(hour=12, minute=45)
    assert e.last_entry_dt == OPEN.replace(hour=12, minute=0)  # close-60min beats 14:30


def test_stale_exit_when_no_bid_at_eod(cfg, store, prov):
    setup_breakout(prov)
    e = engine(RVOL, cfg, prov, store)
    e.poll(t(10, 31), prov.minute_bars(["AAA"], prov.session(DAY), t(10, 31)))
    prov.quotes[CE] = Quote(bid=None, ask=None)
    e.poll(t(15, 46), prov.minute_bars(["AAA"], prov.session(DAY), t(15, 46)))
    r = rows(store, RVOL)
    assert r[0]["stale_exit"] == "True" and float(r[0]["exit_px"]) == 4.90   # last bid seen at entry


def test_no_breakout_no_trade(cfg, store, prov):
    prov.bars["AAA"] = day_bars(breakout="none")
    prov.hist["AAA"] = [1000.0] * 14
    prov.price["AAA"] = 100.0
    e = engine(RVOL, cfg, prov, store)
    e.poll(t(10, 31), prov.minute_bars(["AAA"], prov.session(DAY), t(10, 31)))
    st = e.state["AAA"]
    assert st.pos is None and st.signal is None and not st.done
