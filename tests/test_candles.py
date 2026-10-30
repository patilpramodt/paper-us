from conftest import CLOSE, OPEN, mk_bars, t

from paper_us.candles import build_candles


def test_aggregates_ohlcv_and_anchors_to_open():
    closes = [100 + i for i in range(30)]
    bars = mk_bars(t(9, 30), closes, vol=10)
    cs = build_candles(bars, OPEN, CLOSE, 30, t(10, 1), 15)
    assert len(cs) == 1
    c = cs[0]
    assert c.idx == 0 and c.start == t(9, 30) and c.end == t(10, 0)
    assert c.o == 100 and c.c == 129 and c.v == 300
    assert c.h == max(b.h for b in bars) and c.l == min(b.l for b in bars)


def test_incomplete_candle_not_emitted_until_settled():
    bars = mk_bars(t(9, 30), [100.0] * 30, vol=1)
    assert build_candles(bars, OPEN, CLOSE, 30, t(10, 0, 5), 15) == []      # inside settle window
    assert len(build_candles(bars, OPEN, CLOSE, 30, t(10, 0, 15), 15)) == 1


def test_partial_second_candle_excluded():
    bars = mk_bars(t(9, 30), [100.0] * 45, vol=1)
    cs = build_candles(bars, OPEN, CLOSE, 30, t(10, 15), 15)
    assert [c.idx for c in cs] == [0]


def test_gap_keeps_idx_and_bars_outside_session_ignored():
    bars = mk_bars(t(9, 30), [100.0] * 30) + mk_bars(t(10, 30), [101.0] * 30)
    bars += mk_bars(t(16, 0), [1.0] * 5)  # after close
    cs = build_candles(bars, OPEN, CLOSE, 30, t(17, 0), 15)
    assert [c.idx for c in cs] == [0, 2]


def test_dst_regression_open_is_est_after_nov_1():
    # 09:30 ET on Nov 2 2026 is 14:30 UTC (EST); candle anchoring must follow the session open
    assert OPEN.utcoffset().total_seconds() == -5 * 3600
