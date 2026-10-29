"""Offline guard: the request objects the Alpaca adapter builds must still validate against the
installed alpaca-py (catches signature drift without needing network or keys)."""

from datetime import date, datetime, timezone

from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.requests import OptionSnapshotRequest, StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.enums import ContractType
from alpaca.trading.requests import GetCalendarRequest, GetOptionContractsRequest


def test_requests_construct():
    now = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
    StockBarsRequest(symbol_or_symbols=["NVDA", "AAPL"], timeframe=TimeFrame.Minute, start=now, end=now,
                     feed=DataFeed.IEX)
    StockBarsRequest(symbol_or_symbols=["NVDA"], timeframe=TimeFrame(30, TimeFrameUnit.Minute), start=now, end=now,
                     feed=DataFeed.IEX)
    StockLatestTradeRequest(symbol_or_symbols="NVDA", feed=DataFeed.IEX)
    OptionSnapshotRequest(symbol_or_symbols="NVDA261106C00200000", feed=OptionsFeed.INDICATIVE)
    GetCalendarRequest(start=date(2026, 11, 2), end=date(2026, 11, 2))
    GetOptionContractsRequest(
        underlying_symbols=["NVDA"], type=ContractType.CALL,
        expiration_date_gte=date(2026, 11, 4), expiration_date_lte=date(2026, 11, 18),
        strike_price_gte="180.0", strike_price_lte="220.0", limit=1000, page_token=None,
    )
