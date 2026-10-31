import csv
import threading
from datetime import date, datetime, timedelta

import pytest
from conftest import CLOSE, DAY, FakeProvider, OPEN, mk_bars, t

from paper_us.config import Config
from paper_us.momentum import NAME, MomentumEngine, close_series, decide, sigma_profile
from paper_us.runner import run_forever
from paper_us.store import Store
from paper_us.types import ET, Bar, Session

CFG = Config(universe=("QQQ",), settle_sec=15)


def past_session(day_no):
    day = date(2026, 10, day_no)
    return Session(day, datetime(2026, 10, day_no, 9, 30, tzinfo=ET), datetime(2026, 10, day_no, 16, 0, tzinfo=ET))


def flat_history(n=14, close=100.0, wiggle=0.1):
    """Every past session opens at `close` and sits `wiggle` away, so sigma is wiggle/close at every minute."""
    out = []
    for i in range(n):
        s = past_session(i + 1)
        bars = [Bar(s.open + timedelta(minutes=m), close, close, close, close + wiggle, 1000.0, close) for m in range(390)]
        out.append((s, bars))
    return out


def rows(closes):
    return [Bar(OPEN + timedelta(minutes=i), c, c, c, c, 1000.0, c) for i, c in enumerate(closes)]


@pytest.fixture
def eng(tmp_path):
    prov = FakeProvider()
    prov.history["QQQ"] = flat_history()
    prov.price["QQQ"] = 101.0
    e = MomentumEngine(CFG, prov, Store(str(tmp_path)))
    e.start_day(prov.session(DAY))
    return e


def trades(e):
    with open(e.store.trades_path(NAME)) as f:
        return list(csv.DictReader(f))


@pytest.mark.parametrize("side,close,want", [
    (0, 101.0, 1), (0, 99.0, -1), (0, 100.0, 0),   # upper 100.5 lower 99.5 vwap 100
    (1, 101.0, 1), (1, 100.2, 0), (1, 99.0, -1),
    (-1, 99.0, -1), (-1, 99.8, 0), (-1, 101.0, 1),
])
def test_decide(side, close, want):
    assert decide(side, close, 100.5, 99.5, 100.0) == want


def test_long_stops_on_vwap_above_the_band():
    assert decide(1, 100.6, 100.5, 99.5, 100.7) == 0


def test_sigma_early_close_days_only_count_their_minutes():
    full = (past_session(1), [Bar(past_session(1).open + timedelta(minutes=m), 100, 100, 100, 101, 1) for m in range(390)])
    half_s = Session(date(2026, 10, 2), datetime(2026, 10, 2, 9, 30, tzinfo=ET), datetime(2026, 10, 2, 13, 0, tzinfo=ET))
    half = (half_s, [Bar(half_s.open + timedelta(minutes=m), 100, 100, 100, 103, 1) for m in range(210)])
    sig = sigma_profile([full, half])
    assert sig[0] == pytest.approx(0.02)
    assert sig[300] == pytest.approx(0.01)


def test_close_series_carries_over_a_missing_minute():
    bars = rows([100.0, 101.0, 102.0])
    assert close_series(Session(DAY, OPEN, CLOSE), [bars[0], bars[2]])[:3] == [100.0, 100.0, 102.0]


def test_first_check_waits_for_settle_then_enters_long(eng):
    bars = {"QQQ": rows([100.0] * 29 + [101.0])}
    eng.poll(t(10, 0, 5), bars)
    assert eng.pos is None
    eng.poll(t(10, 0, 20), bars)
    assert eng.pos["side"] == 1 and eng.pos["entry_px"] == 101.0
    assert eng.pos["upper"] == pytest.approx(100.1 * 1.001)  # prev close 100.1 is above the open


def test_check_waits_for_late_bar_then_uses_carried_close(eng):
    bars = {"QQQ": rows([100.0] * 29)}           # the 09:59 bar has not arrived
    eng.poll(t(10, 0, 20), bars)
    assert eng.last_m == -1
    eng.poll(t(10, 1, 5), bars)
    assert eng.last_m == 29 and eng.pos is None


def test_check_runs_once(eng):
    bars = {"QQQ": rows([100.0] * 29 + [101.0])}
    eng.poll(t(10, 0, 20), bars)
    eng.p.price["QQQ"] = 150.0
    eng.poll(t(10, 0, 30), bars)
    assert eng.pos["entry_px"] == 101.0


def test_stale_check_is_skipped(eng):
    eng.poll(t(10, 5), {"QQQ": rows([100.0] * 29 + [101.0])})
    assert eng.pos is None and eng.last_m == 29


def test_flip_books_exit_and_opens_other_side(eng):
    eng.poll(t(10, 0, 20), {"QQQ": rows([100.0] * 29 + [101.0])})
    eng.p.price["QQQ"] = 99.0
    eng.poll(t(10, 30, 20), {"QQQ": rows([100.0] * 29 + [101.0] * 30 + [99.0])})
    assert eng.pos["side"] == -1
    row = trades(eng)[0]
    assert row["reason"] == "FLIP" and float(row["gross_pnl"]) == -100.0
    assert float(row["fees"]) == pytest.approx(50 * 200 * 0.2 / 1e4, abs=0.01)


def test_eod_exit_uses_last_bar_close(eng):
    closes = [100.0] * 29 + [101.0]
    eng.poll(t(10, 0, 20), {"QQQ": rows(closes)})
    closes = closes + [102.0] * 360
    eng.poll(t(16, 0, 20), {"QQQ": rows(closes)})
    assert eng.pos is None
    row = trades(eng)[0]
    assert row["reason"] == "EOD" and float(row["exit_px"]) == 102.0


def test_no_check_after_last_one(eng):
    assert eng.checks[0] == 29 and eng.checks[-1] == 359


def test_early_close_checks_stop_at_1230(tmp_path):
    half = Session(DAY, OPEN, t(13, 0))
    prov = FakeProvider(half)
    prov.history["QQQ"] = flat_history()
    e = MomentumEngine(CFG, prov, Store(str(tmp_path)))
    e.start_day(half)
    assert e.checks[-1] == 179


def test_restart_restores_position_and_skips_done_check(eng):
    bars = {"QQQ": rows([100.0] * 29 + [101.0])}
    eng.poll(t(10, 0, 20), bars)
    e2 = MomentumEngine(CFG, eng.p, eng.store)
    e2.start_day(eng.session)
    assert e2.pos["side"] == 1 and e2.last_m == 29
    assert e2.pos["entry_ts"] == t(10, 0, 20)


def test_position_left_open_yesterday_is_booked_at_yesterdays_close(tmp_path):
    prov = FakeProvider()
    prov.history["QQQ"] = flat_history()
    store = Store(str(tmp_path))
    last = prov.history["QQQ"][-1][0]
    pos = dict(side=-1, entry_ts=last.open, entry_px=101.0, entry_close=101.0, upper=102.0, lower=100.0, vwap=100.5)
    store.save_state(NAME, last.day, dict(pos=pos, last_m=29, day_pnl=0.0, day_trades=0))
    e = MomentumEngine(CFG, prov, store)
    e.start_day(prov.session(DAY))
    row = trades(e)[0]
    assert row["date"] == last.day.isoformat() and row["stale_exit"] == "True"
    assert float(row["exit_px"]) == 100.1
    e.start_day(prov.session(DAY))
    assert len(trades(e)) == 1


def test_short_history_refuses_to_start(tmp_path):
    prov = FakeProvider()
    prov.history["QQQ"] = flat_history(5)
    e = MomentumEngine(CFG, prov, Store(str(tmp_path)))
    with pytest.raises(RuntimeError):
        e.start_day(prov.session(DAY))


def test_runner_full_day(tmp_path):
    prov = FakeProvider()
    prov.history["QQQ"] = flat_history()
    prov.bars["QQQ"] = rows([100.0] * 29 + [101.0] * 361)
    prov.price["QQQ"] = 101.0
    cfg = Config(universe=("QQQ",), poll_sec=10, settle_sec=15)
    store = Store(str(tmp_path))
    e = MomentumEngine(cfg, prov, store)
    stop = threading.Event()
    clock = {"now": t(9, 0)}

    def sleep(sec):
        clock["now"] += timedelta(seconds=sec)
        if clock["now"] > t(16, 10):
            stop.set()

    run_forever(cfg, prov, [e], stop, clock=lambda: clock["now"], sleep=sleep)
    got = trades(e)
    assert len(got) == 1 and got[0]["reason"] == "EOD" and got[0]["stale_exit"] == "False"
