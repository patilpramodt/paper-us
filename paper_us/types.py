"""Plain data types and the DataProvider interface the engine depends on."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional, Protocol, Sequence
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Bar:
    ts: datetime  # bar START, timezone-aware
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass(frozen=True)
class Candle:
    idx: int  # 0 = first candle of the session
    start: datetime
    end: datetime
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass(frozen=True)
class Quote:
    bid: Optional[float]
    ask: Optional[float]

    @property
    def valid(self) -> bool:
        return bool(self.bid and self.ask and self.bid > 0 and self.ask > 0 and self.ask >= self.bid)

    @property
    def spread_pct(self) -> Optional[float]:
        if not self.valid:
            return None
        mid = (self.bid + self.ask) / 2.0
        return (self.ask - self.bid) / mid if mid > 0 else None


@dataclass(frozen=True)
class Contract:
    symbol: str  # OCC symbol, e.g. NVDA261120C00200000
    underlying: str
    is_call: bool
    strike: float
    expiry: date
    multiplier: int = 100


@dataclass(frozen=True)
class Session:
    day: date
    open: datetime   # aware, ET
    close: datetime  # aware, ET (13:00 on early-close days)


class DataProvider(Protocol):
    def session(self, day: date) -> Optional[Session]:
        """Trading session for `day`, or None if the market is closed."""

    def minute_bars(self, symbols: Sequence[str], session: Session, now: datetime) -> Dict[str, List[Bar]]:
        """Today's 1-minute bars (session open .. now) per symbol."""

    def first_window_volumes(self, symbols: Sequence[str], before_day: date, n: int) -> Dict[str, List[float]]:
        """Volume of the first 30 minutes (09:30-10:00 ET) of up to `n` sessions before `before_day`, newest first."""

    def latest_price(self, symbol: str) -> Optional[float]:
        """Last trade price of the underlying."""

    def pick_contract(self, underlying: str, is_call: bool, spot: float, today: date) -> Optional[Contract]:
        """Nearest eligible expiry, strike closest to spot."""

    def option_quote(self, contract_symbol: str) -> Optional[Quote]:
        """Latest bid/ask for one option contract."""
