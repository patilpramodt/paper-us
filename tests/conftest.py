import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_us.config import Config, Variant  # noqa: E402
from paper_us.store import Store  # noqa: E402
from paper_us.types import ET, Bar, Contract, Quote, Session  # noqa: E402

DAY = date(2026, 11, 2)  # Monday, first day after US DST ends (EST, UTC-5)
OPEN = datetime(2026, 11, 2, 9, 30, tzinfo=ET)
CLOSE = datetime(2026, 11, 2, 16, 0, tzinfo=ET)


def t(h, m, s=0):
    return datetime(2026, 11, 2, h, m, s, tzinfo=ET)


def mk_bars(start, closes, vol=1000.0, spread=0.10):
    """One 1-minute bar per close, starting at `start`. o=previous close, h/l = +/- spread."""
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append(Bar(ts=start + timedelta(minutes=i), o=prev, h=max(prev, c) + spread,
                       l=min(prev, c) - spread, c=c, v=vol))
        prev = c
    return out


class FakeProvider:
    def __init__(self, session=None):
        self._session = session or Session(DAY, OPEN, CLOSE)
        self.bars = {}                 # sym -> list[Bar]
        self.hist = {}                 # sym -> list[float]
        self.price = {}                # sym -> last trade
        self.quotes = {}               # option symbol -> Quote
        self.quote_default = Quote(bid=4.90, ask=5.00)
        self.contract_calls = []

    def session(self, day):
        return self._session if day == self._session.day else None

    def minute_bars(self, symbols, session, now):
        return {s: [b for b in self.bars.get(s, []) if b.ts <= now] for s in symbols}

    def first_window_volumes(self, symbols, before_day, n):
        return {s: list(self.hist.get(s, []))[:n] for s in symbols}

    def latest_price(self, symbol):
        return self.price.get(symbol)

    def pick_contract(self, underlying, is_call, spot, today):
        self.contract_calls.append((underlying, is_call, spot))
        return Contract(symbol=f"{underlying}261106{'C' if is_call else 'P'}00200000", underlying=underlying,
                        is_call=is_call, strike=round(spot), expiry=date(2026, 11, 6))

    def option_quote(self, contract_symbol):
        return self.quotes.get(contract_symbol, self.quote_default)


@pytest.fixture
def cfg():
    return Config(universe=("AAA",), fee_per_contract=0.65, settle_sec=15)


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path))


@pytest.fixture
def prov():
    return FakeProvider()


def day_bars(first_vol_per_min=1000.0, breakout="up"):
    """09:30-10:00 range ~100 (99.9..100.1 + bar spread), then a clean 10:00-10:30 breakout candle."""
    bars = mk_bars(t(9, 30), [100.0] * 30, vol=first_vol_per_min)
    if breakout == "up":
        closes = [100.5 + 0.05 * i for i in range(30)]          # closes ~101.95 > upper
    elif breakout == "down":
        closes = [99.5 - 0.05 * i for i in range(30)]
    else:
        closes = [100.0] * 30
    bars += mk_bars(t(10, 0), closes, vol=1000.0)
    return bars
