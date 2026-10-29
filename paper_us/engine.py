"""ORB30 RVOL engine (paper only). One instance per variant.

Rules (30-minute candles on the UNDERLYING stock; mirror for puts):

  MARK    first candle 09:30-10:00 ET: upper = high + buffer, lower = low - buffer
  FILTER  stocks only. First-candle volume >= variant.rvol_min x the average first-candle
          volume of the previous N sessions. The FIRST breakout of the day decides: if the
          filter fails there, the stock is done for the day.
  ENTRY   a LATER candle CLOSES above upper -> buy ATM call; below lower -> buy ATM put.
          One trade per stock per day.
  EXIT    SL    spot crosses the breakout candle's low (calls) / high (puts) -/+ buffer
          TRAIL (ORB30_RVOL_TRAIL only) step lock on gross option P&L: peak >= 1 step ->
                stop at entry (0); >= 2 steps -> +1 step; ...
          EOD   square-off before the close
          No profit target in either variant.

Fills are simulated: buys at the ask, sells at the bid (+ optional extra slippage), minus
a per-contract fee on each side.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from .candles import build_candles
from .config import Config, Variant
from .store import Store
from .types import Bar, Candle, DataProvider, Quote, Session

log = logging.getLogger("paper_us.engine")


@dataclass
class Signal:
    is_call: bool
    idx: int
    since: datetime
    sl_level: float
    candle_close: float


@dataclass
class Position:
    contract: str
    underlying: str
    is_call: bool
    strike: float
    expiry: str
    multiplier: int
    qty: int
    entry_ts: datetime
    entry_px: float
    entry_spot: float
    sl_level: float
    rvol: float
    or_high: float
    or_low: float
    entry_spread_pct: float
    peak_gross: float = 0.0
    last_bid: Optional[float] = None


@dataclass
class SymState:
    or_high: Optional[float] = None
    or_low: Optional[float] = None
    upper: Optional[float] = None
    lower: Optional[float] = None
    first_vol: Optional[float] = None
    rvol: Optional[float] = None
    rvol_note: str = ""
    last_eval_idx: int = 0
    done: bool = False
    done_reason: str = ""
    signal: Optional[Signal] = None
    pos: Optional[Position] = None


class Orb30Engine:
    def __init__(self, variant: Variant, cfg: Config, provider: DataProvider, store: Store):
        self.v = variant
        self.cfg = cfg
        self.p = provider
        self.store = store
        self.session: Optional[Session] = None
        self.state: Dict[str, SymState] = {}
        self.hist: Dict[str, List[float]] = {}
        self.day_pnl = 0.0
        self.day_trades = 0
        self._dirty = False

    # ── day lifecycle ───────────────────────────────────────────────────────
    def start_day(self, session: Session) -> None:
        self.session = session
        self.state = {s: SymState() for s in self.cfg.universe}
        self.day_pnl, self.day_trades = 0.0, 0
        self.last_entry_dt = min(
            session.open.replace(hour=self.cfg.last_entry_bar_start.hour, minute=self.cfg.last_entry_bar_start.minute),
            session.close - timedelta(minutes=60),
        )
        self.eod_dt = session.close - timedelta(minutes=self.cfg.squareoff_before_close_min)

        self.hist = self.p.first_window_volumes(self.cfg.universe, session.day, self.cfg.rvol_lookback)
        for s in self.cfg.universe:
            log.info("[%s] %s first-30m history: %d sessions", self.v.name, s, len(self.hist.get(s, [])))

        saved = self.store.load_state(self.v.name, session.day)
        if saved:
            self._restore(saved)
            log.info("[%s] restored state for %s (%d open)", self.v.name, session.day,
                     sum(1 for st in self.state.values() if st.pos))

    def finish_day(self) -> None:
        # Safety: anything still open after the close is booked stale.
        for sym, st in self.state.items():
            if st.pos:
                self._exit(sym, st, self.session.close, "EOD", None, force=True)
        self._save()
        log.info("[%s] DAY DONE %s: %d trades, net %.2f USD", self.v.name, self.session.day,
                 self.day_trades, self.day_pnl)

    # ── main poll ───────────────────────────────────────────────────────────
    def poll(self, now: datetime, bars: Dict[str, List[Bar]]) -> None:
        for sym in self.cfg.universe:
            st = self.state[sym]
            try:
                if st.pos:
                    self._manage(sym, st, now, bars.get(sym, []))
                if st.pos is None and not st.done:
                    self._scan(sym, st, now, bars.get(sym, []))
                if st.pos is None and st.signal is not None and not st.done:
                    self._try_enter(sym, st, now)
            except Exception:
                log.exception("[%s] %s poll error", self.v.name, sym)
        if self._dirty:
            self._save()

    # ── scanning ────────────────────────────────────────────────────────────
    def _scan(self, sym: str, st: SymState, now: datetime, bars: List[Bar]) -> None:
        if now >= self.eod_dt:
            st.done, st.done_reason = True, "after_squareoff_time"
            self._dirty = True
            return
        candles = build_candles(bars, self.session.open, self.session.close,
                                self.cfg.bar_minutes, now, self.cfg.settle_sec)
        if not candles:
            return
        if st.or_high is None:
            first = candles[0]
            if first.idx != 0:
                return  # no trades in the first window: no opening range today
            self._lock_range(sym, st, first)

        for c in candles:
            if c.idx == 0 or c.idx <= st.last_eval_idx:
                continue
            st.last_eval_idx = c.idx
            self._dirty = True
            if c.start > self.last_entry_dt:
                st.done, st.done_reason = True, "past_last_entry_bar"
                return
            if c.c > st.upper:
                is_call = True
            elif c.c < st.lower:
                is_call = False
            else:
                continue
            if (now - c.end).total_seconds() > self.cfg.max_signal_age_sec:
                log.info("[%s] %s stale breakout (candle ended %s) - skipped", self.v.name, sym, c.end.time())
                continue
            # first (fresh) breakout decides the RVOL gate
            if st.rvol is None or st.rvol < self.v.rvol_min:
                st.done, st.done_reason = True, f"rvol_gate({st.rvol_note})"
                log.info("[%s] %s breakout but %s < %.1f -> done for the day",
                         self.v.name, sym, st.rvol_note, self.v.rvol_min)
                return
            buf = self.cfg.sl_buffer_pct * (c.l if is_call else c.h)
            sl = c.l - buf if is_call else c.h + buf
            st.signal = Signal(is_call=is_call, idx=c.idx, since=now, sl_level=sl, candle_close=c.c)
            log.info("[%s] %s SIGNAL %s candle %s-%s close %.2f (range %.2f..%.2f) rvol %.2f SL %.2f",
                     self.v.name, sym, "CALL" if is_call else "PUT", c.start.time(), c.end.time(),
                     c.c, st.lower, st.upper, st.rvol, sl)
            return

    def _lock_range(self, sym: str, st: SymState, c: Candle) -> None:
        st.or_high, st.or_low, st.first_vol = c.h, c.l, c.v
        st.upper = c.h + self.cfg.mark_buffer_pct * c.h
        st.lower = c.l - self.cfg.mark_buffer_pct * c.l
        hist = self.hist.get(sym, [])
        if len(hist) < self.cfg.rvol_min_days:
            st.rvol, st.rvol_note = None, f"only {len(hist)} days history"
        else:
            avg = sum(hist) / len(hist)
            if avg <= 0:
                st.rvol, st.rvol_note = None, "zero avg volume"
            else:
                st.rvol = c.v / avg
                st.rvol_note = f"rvol {st.rvol:.2f}"
        log.info("[%s] %s range locked %.2f..%.2f (vol %.0f, %s)", self.v.name, sym,
                 st.lower, st.upper, c.v, st.rvol_note)
        self._dirty = True

    # ── entry ───────────────────────────────────────────────────────────────
    def _try_enter(self, sym: str, st: SymState, now: datetime) -> None:
        sig = st.signal
        assert sig is not None
        if (now - sig.since).total_seconds() > self.cfg.entry_retry_sec:
            log.info("[%s] %s signal expired without a valid entry", self.v.name, sym)
            st.signal = None
            self._dirty = True
            return
        if now >= self.eod_dt:
            st.signal, st.done, st.done_reason = None, True, "after_squareoff_time"
            self._dirty = True
            return

        spot = self.p.latest_price(sym)
        if spot is None:
            return
        if (sig.is_call and spot <= sig.sl_level) or (not sig.is_call and spot >= sig.sl_level):
            log.info("[%s] %s spot %.2f already through SL %.2f - signal dropped", self.v.name, sym, spot, sig.sl_level)
            st.signal = None
            self._dirty = True
            return

        con = self.p.pick_contract(sym, sig.is_call, spot, now.date())
        if con is None:
            log.warning("[%s] %s no eligible contract (will retry)", self.v.name, sym)
            return
        q = self.p.option_quote(con.symbol)
        reason = self._quote_problem(q)
        if reason:
            log.info("[%s] %s %s quote rejected: %s (will retry)", self.v.name, sym, con.symbol, reason)
            return

        px = q.ask + self.cfg.extra_slip_per_share
        st.pos = Position(
            contract=con.symbol, underlying=sym, is_call=sig.is_call, strike=con.strike,
            expiry=con.expiry.isoformat(), multiplier=con.multiplier, qty=self.cfg.contracts,
            entry_ts=now, entry_px=px, entry_spot=spot, sl_level=sig.sl_level,
            rvol=st.rvol or 0.0, or_high=st.or_high, or_low=st.or_low,
            entry_spread_pct=q.spread_pct or 0.0, last_bid=q.bid,
        )
        st.signal = None
        self._dirty = True
        log.info("[%s] %s ENTER %s x%d @ %.2f (bid %.2f ask %.2f, spot %.2f, SL %.2f)",
                 self.v.name, sym, con.symbol, self.cfg.contracts, px, q.bid, q.ask, spot, sig.sl_level)

    def _quote_problem(self, q: Optional[Quote]) -> Optional[str]:
        if q is None or not q.valid:
            return "no valid bid/ask"
        if q.ask < self.cfg.min_premium:
            return f"ask {q.ask:.2f} < min premium {self.cfg.min_premium:.2f}"
        if q.spread_pct is None or q.spread_pct > self.cfg.max_spread_pct:
            return f"spread {0 if q.spread_pct is None else q.spread_pct:.1%} > {self.cfg.max_spread_pct:.0%}"
        return None

    # ── position management ─────────────────────────────────────────────────
    def _manage(self, sym: str, st: SymState, now: datetime, bars: List[Bar]) -> None:
        pos = st.pos
        assert pos is not None
        spot = self.p.latest_price(sym)
        q = self.p.option_quote(pos.contract)
        bid = q.bid if (q and q.valid) else None
        if bid is not None:
            pos.last_bid = bid
            self._dirty = True

        reason: Optional[str] = None
        if now >= self.eod_dt:
            reason = "EOD"
        elif self._sl_hit(pos, spot, bars):
            reason = "SL"
        elif self.v.trail_step_usd and bid is not None:
            gross = (bid - pos.entry_px) * pos.multiplier * pos.qty
            if gross > pos.peak_gross:
                pos.peak_gross = gross
                self._dirty = True
            steps = math.floor(pos.peak_gross / self.v.trail_step_usd)
            if steps >= 1 and gross <= (steps - 1) * self.v.trail_step_usd:
                reason = "TRAIL_LOCK"

        if reason:
            self._exit(sym, st, now, reason, spot, force=reason in ("EOD", "SL"))

    @staticmethod
    def _sl_hit(pos: Position, spot: Optional[float], bars: List[Bar]) -> bool:
        """SL if the latest price OR any 1-minute bar after the entry minute crossed the level."""
        entry_min = pos.entry_ts.replace(second=0, microsecond=0)
        after = [b for b in bars if b.ts > entry_min]
        if pos.is_call:
            lows = [b.l for b in after] + ([spot] if spot is not None else [])
            return bool(lows) and min(lows) <= pos.sl_level
        highs = [b.h for b in after] + ([spot] if spot is not None else [])
        return bool(highs) and max(highs) >= pos.sl_level

    def _exit(self, sym: str, st: SymState, now: datetime, reason: str,
              spot: Optional[float], force: bool) -> None:
        pos = st.pos
        assert pos is not None
        q = self.p.option_quote(pos.contract)
        if q and q.valid:
            bid, stale = q.bid, False
        elif force:
            # Cannot see a live bid: book at the last bid we saw (or entry if we never saw one).
            bid, stale = (pos.last_bid if pos.last_bid is not None else pos.entry_px), True
            log.warning("[%s] %s exiting on STALE price %.2f (%s)", self.v.name, sym, bid, reason)
        else:
            return  # try again next poll
        px = max(bid - self.cfg.extra_slip_per_share, 0.0)
        gross = (px - pos.entry_px) * pos.multiplier * pos.qty
        fees = 2 * self.cfg.fee_per_contract * pos.qty
        net = gross - fees
        self.day_pnl += net
        self.day_trades += 1
        self.store.append_trade(self.v.name, {
            "date": pos.entry_ts.date().isoformat(), "variant": self.v.name, "symbol": sym,
            "side": "CALL" if pos.is_call else "PUT", "contract": pos.contract, "strike": pos.strike,
            "expiry": pos.expiry, "entry_time": pos.entry_ts.isoformat(timespec="seconds"),
            "exit_time": now.isoformat(timespec="seconds"), "qty": pos.qty,
            "entry_px": round(pos.entry_px, 4), "exit_px": round(px, 4), "reason": reason,
            "gross_pnl": round(gross, 2), "fees": round(fees, 2), "net_pnl": round(net, 2),
            "peak_gross": round(pos.peak_gross, 2), "rvol": round(pos.rvol, 2),
            "or_high": pos.or_high, "or_low": pos.or_low, "sl_level": round(pos.sl_level, 4),
            "spot_entry": pos.entry_spot, "spot_exit": spot,
            "entry_spread_pct": round(pos.entry_spread_pct, 4), "stale_exit": stale,
        })
        log.info("[%s] %s EXIT %s %s @ %.2f net %.2f USD (gross %.2f fees %.2f)",
                 self.v.name, sym, pos.contract, reason, px, net, gross, fees)
        st.pos = None
        st.done, st.done_reason = True, f"traded({reason})"
        self._dirty = True

    # ── persistence ─────────────────────────────────────────────────────────
    def _save(self) -> None:
        payload = {"day_pnl": self.day_pnl, "day_trades": self.day_trades,
                   "symbols": {s: asdict(st) for s, st in self.state.items()}}
        self.store.save_state(self.v.name, self.session.day, payload)
        self._dirty = False

    def _restore(self, saved: dict) -> None:
        self.day_pnl = saved.get("day_pnl", 0.0)
        self.day_trades = saved.get("day_trades", 0)
        for sym, d in saved.get("symbols", {}).items():
            if sym not in self.state:
                continue
            d = dict(d)
            if d.get("signal"):
                d["signal"] = Signal(**d["signal"])
            if d.get("pos"):
                d["pos"] = Position(**d["pos"])
            self.state[sym] = SymState(**d)
