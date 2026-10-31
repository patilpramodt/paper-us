"""Alpaca implementation of DataProvider (free tier: IEX stock data + indicative option quotes).

Written against alpaca-py 0.44 (signatures checked by introspection; the request objects are
also built offline in tests/test_alpaca_requests.py). It has NOT been exercised against the live
API from the build sandbox - run `python smoke_test.py` on your machine first.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from alpaca.data.enums import Adjustment, DataFeed, OptionsFeed
from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import OptionSnapshotRequest, StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import ContractType
from alpaca.trading.requests import GetCalendarRequest, GetOptionContractsRequest

from .config import Config
from .types import ET, Bar, Contract, Quote, Session

log = logging.getLogger("paper_us.alpaca")

_STOCK_FEEDS = {"iex": DataFeed.IEX, "delayed_sip": DataFeed.DELAYED_SIP, "sip": DataFeed.SIP}
_OPTION_FEEDS = {"indicative": OptionsFeed.INDICATIVE, "opra": OptionsFeed.OPRA}


def _as_et(x, day: date) -> datetime:
    """Calendar open/close may arrive as naive datetime (ET), aware datetime or time."""
    if isinstance(x, datetime):
        return x.replace(tzinfo=ET) if x.tzinfo is None else x.astimezone(ET)
    if isinstance(x, time):
        return datetime.combine(day, x, tzinfo=ET)
    raise TypeError(f"unexpected calendar value {x!r}")


class AlpacaProvider:
    def __init__(self, key: str, secret: str, cfg: Config, paper: bool = True):
        self.cfg = cfg
        self.stock_feed = _STOCK_FEEDS[cfg.stock_feed]
        self.option_feed = _OPTION_FEEDS[cfg.option_feed]
        self._stock = StockHistoricalDataClient(key, secret)
        self._opt = OptionHistoricalDataClient(key, secret)
        self._trade = TradingClient(key, secret, paper=paper)  # used only for calendar + contract lookup
        self._sessions: Dict[date, Optional[Session]] = {}
        self._contracts: Dict[tuple, list] = {}

    # ── calendar ────────────────────────────────────────────────────────────
    def session(self, day: date) -> Optional[Session]:
        if day in self._sessions:
            return self._sessions[day]
        cal = self._trade.get_calendar(GetCalendarRequest(start=day, end=day))
        sess = None
        if cal:
            c = cal[0]
            if c.date == day:
                sess = Session(day=day, open=_as_et(c.open, day), close=_as_et(c.close, day))
        self._sessions[day] = sess
        return sess

    # ── stock data ──────────────────────────────────────────────────────────
    def minute_bars(self, symbols: Sequence[str], session: Session, now: datetime) -> Dict[str, List[Bar]]:
        req = StockBarsRequest(
            symbol_or_symbols=list(symbols), timeframe=TimeFrame.Minute,
            start=session.open.astimezone(timezone.utc),
            end=min(now, session.close).astimezone(timezone.utc),
            feed=self.stock_feed,
        )
        data = self._stock.get_stock_bars(req).data
        return {s: [_bar(b) for b in data.get(s, [])] for s in symbols}

    def first_window_volumes(self, symbols: Sequence[str], before_day: date, n: int) -> Dict[str, List[float]]:
        """Volume of the 09:30 30-minute bar for the last `n` sessions before `before_day`.

        Relies on Alpaca's 30-minute bars starting at 09:30 ET (true when bars align to the
        hour/half-hour). smoke_test.py prints what it finds so you can confirm.
        """
        start = datetime.combine(before_day - timedelta(days=n * 2 + 12), time(0, 0), tzinfo=ET)
        end = datetime.combine(before_day, time(0, 0), tzinfo=ET)
        req = StockBarsRequest(
            symbol_or_symbols=list(symbols), timeframe=TimeFrame(30, TimeFrameUnit.Minute),
            start=start.astimezone(timezone.utc), end=end.astimezone(timezone.utc), feed=self.stock_feed,
        )
        data = self._stock.get_stock_bars(req).data
        out: Dict[str, List[float]] = {}
        for s in symbols:
            rows = []
            for b in data.get(s, []):
                t = b.timestamp.astimezone(ET)
                if t.hour == 9 and t.minute == 30:
                    rows.append((t.date(), float(b.volume)))
            rows.sort(reverse=True)
            out[s] = [v for _, v in rows[:n]]
        return out

    def history_sessions(self, symbol: str, before_day: date, n: int) -> List[Tuple[Session, List[Bar]]]:
        cal = self._trade.get_calendar(GetCalendarRequest(
            start=before_day - timedelta(days=n * 2 + 12), end=before_day - timedelta(days=1)))
        sessions = [Session(c.date, _as_et(c.open, c.date), _as_et(c.close, c.date)) for c in cal]
        sessions.sort(key=lambda s: s.day)
        sessions = sessions[-n:]
        if not sessions:
            return []
        req = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, adjustment=Adjustment.RAW,
            start=sessions[0].open.astimezone(timezone.utc), end=sessions[-1].close.astimezone(timezone.utc),
            feed=_STOCK_FEEDS[self.cfg.mom_history_feed],
        )
        bars = [_bar(b) for b in self._stock.get_stock_bars(req).data.get(symbol, [])]
        out = []
        for s in sessions:
            rows = sorted((b for b in bars if s.open <= b.ts < s.close), key=lambda b: b.ts)
            if rows and rows[0].ts == s.open:  # the backtest drops days with no opening bar
                out.append((s, rows))
        return out

    def latest_price(self, symbol: str) -> Optional[float]:
        res = self._stock.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=symbol, feed=self.stock_feed))
        t = res.get(symbol)
        return float(t.price) if t is not None else None

    # ── options ─────────────────────────────────────────────────────────────
    def pick_contract(self, underlying: str, is_call: bool, spot: float, today: date) -> Optional[Contract]:
        lo = today + timedelta(days=self.cfg.min_dte)
        hi = lo + timedelta(days=self.cfg.expiry_window_days)
        band = self.cfg.strike_band_pct
        key = (underlying, is_call, today, round(spot, -1))
        rows = self._contracts.get(key)
        if rows is None:
            rows, token = [], None
            while True:
                res = self._trade.get_option_contracts(GetOptionContractsRequest(
                    underlying_symbols=[underlying],
                    type=ContractType.CALL if is_call else ContractType.PUT,
                    expiration_date_gte=lo, expiration_date_lte=hi,
                    strike_price_gte=str(round(spot * (1 - band), 2)),
                    strike_price_lte=str(round(spot * (1 + band), 2)),
                    limit=1000, page_token=token,
                ))
                rows.extend(res.option_contracts or [])
                token = res.next_page_token
                if not token:
                    break
            self._contracts[key] = rows
        rows = [r for r in rows if getattr(r, "tradable", True) and r.expiration_date >= lo]
        if not rows:
            return None
        expiry = min(r.expiration_date for r in rows)
        best = min((r for r in rows if r.expiration_date == expiry), key=lambda r: abs(float(r.strike_price) - spot))
        return Contract(
            symbol=best.symbol, underlying=underlying, is_call=is_call, strike=float(best.strike_price),
            expiry=best.expiration_date, multiplier=int(float(best.size or 100)),
        )

    def option_quote(self, contract_symbol: str) -> Optional[Quote]:
        snaps = self._opt.get_option_snapshot(
            OptionSnapshotRequest(symbol_or_symbols=contract_symbol, feed=self.option_feed))
        s = snaps.get(contract_symbol)
        if s is None or s.latest_quote is None:
            return None
        q = s.latest_quote
        bid = float(q.bid_price) if q.bid_price else None
        ask = float(q.ask_price) if q.ask_price else None
        return Quote(bid=bid, ask=ask)


def _bar(b) -> Bar:
    return Bar(ts=b.timestamp.astimezone(ET), o=float(b.open), h=float(b.high),
               l=float(b.low), c=float(b.close), v=float(b.volume), vw=float(b.vwap or 0.0))
