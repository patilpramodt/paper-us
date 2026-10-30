"""Aggregate 1-minute bars into session-anchored N-minute candles."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Sequence

from .types import Bar, Candle


def build_candles(
    bars: Sequence[Bar],
    session_open: datetime,
    session_close: datetime,
    minutes: int,
    now: datetime,
    settle_sec: int,
) -> List[Candle]:
    """Return only COMPLETE candles, in order.

    A candle is complete once `now` is at least `settle_sec` past its end, which gives
    the last 1-minute bar of the window time to arrive. Buckets with no bars are skipped
    (idx is not renumbered, so gaps are visible to the caller).
    """
    width = minutes * 60
    buckets: Dict[int, List[Bar]] = {}
    for b in bars:
        if b.ts < session_open or b.ts >= session_close:
            continue
        idx = int((b.ts - session_open).total_seconds() // width)
        buckets.setdefault(idx, []).append(b)

    out: List[Candle] = []
    for idx in sorted(buckets):
        start = session_open + timedelta(seconds=idx * width)
        end = start + timedelta(seconds=width)
        if now < end + timedelta(seconds=settle_sec):
            continue
        bs = sorted(buckets[idx], key=lambda x: x.ts)
        out.append(
            Candle(
                idx=idx,
                start=start,
                end=end,
                o=bs[0].o,
                h=max(x.h for x in bs),
                l=min(x.l for x in bs),
                c=bs[-1].c,
                v=sum(x.v for x in bs),
            )
        )
    return out
