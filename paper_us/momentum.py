"""QQQ noise-area intraday momentum engine (paper only, shares, no orders sent).

Live version of backtest/momentum.py with its settings unchanged (look 14, vm 1, 30-minute checks):

  BANDS   upper = max(open, prev close) * (1 + vm * sigma_m), lower = min(open, prev close) * (1 - vm * sigma_m)
          sigma_m = mean |close_m / open - 1| at that minute of day over the previous `look` sessions
  CHECK   at each 30-minute mark from 10:00 (last one 30 minutes before the close), on the bar just ended:
          flat: close > upper -> long, close < lower -> short
          long: close < max(upper, VWAP) -> exit, and reverse if close < lower (mirrored for short)
  EXIT    otherwise held to the close

History (bands, prev close) comes from the consolidated SIP feed, as in the backtest. Today's bars come
from the free real-time IEX feed. Fills are simulated at the last trade when the check runs (the backtest
used the next bar's open) and at the last bar's close at EOD, with `mom_cost_bps` per side as the fee.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from .config import Config
from .store import MOM_TRADE_FIELDS, Store
from .types import Bar, DataProvider, Session

log = logging.getLogger("paper_us.momentum")

NAME = "QQQ_MOMENTUM"
MINUTES = 390


def bars_in_session(session: Session) -> int:
    return int((session.close - session.open).total_seconds() // 60)


def close_series(session: Session, bars: Sequence[Bar]) -> List[Optional[float]]:
    """Close per minute of the session, carrying the last close over minutes with no bar."""
    by_min = {int((b.ts - session.open).total_seconds() // 60): b.c for b in bars}
    out: List[Optional[float]] = []
    for m in range(min(bars_in_session(session), MINUTES)):
        out.append(by_min.get(m, out[-1] if out else None))
    return out


def sigma_profile(history: Sequence[Tuple[Session, Sequence[Bar]]]) -> List[Optional[float]]:
    """Mean |close_m / open - 1| per minute of day. Early-close days only count for their own minutes."""
    moves: List[List[float]] = [[] for _ in range(MINUTES)]
    for session, bars in history:
        op = bars[0].o
        for m, c in enumerate(close_series(session, bars)):
            if c is not None:
                moves[m].append(abs(c / op - 1))
    return [sum(x) / len(x) if x else None for x in moves]


def decide(side: int, close: float, upper: float, lower: float, vwap: float) -> int:
    """The side to hold after a check: 1 long, -1 short, 0 flat."""
    if side == 1:
        if close < max(upper, vwap):
            return -1 if close < lower else 0
    elif side == -1:
        if close > min(lower, vwap):
            return 1 if close > upper else 0
    elif close > upper:
        return 1
    elif close < lower:
        return -1
    return side


class MomentumEngine:
    def __init__(self, cfg: Config, provider: DataProvider, store: Store):
        self.cfg = cfg
        self.p = provider
        self.store = store
        self.sym = cfg.mom_symbol
        self.session: Optional[Session] = None
        self.sigma: List[Optional[float]] = []
        self.prev_close = 0.0
        self.checks: List[int] = []
        self.pos: Optional[dict] = None
        self.last_m = -1
        self.day_pnl = 0.0
        self.day_trades = 0

    # ── day lifecycle ───────────────────────────────────────────────────────
    def start_day(self, session: Session) -> None:
        history = self.p.history_sessions(self.sym, session.day, self.cfg.mom_look)
        if len(history) < self.cfg.mom_look:
            raise RuntimeError(f"only {len(history)} sessions of {self.sym} history")
        self.session = session
        self.sigma = sigma_profile(history)
        self.prev_close = history[-1][1][-1].c
        self.checks = list(range(self.cfg.mom_every - 1, min(bars_in_session(session), MINUTES) - 1, self.cfg.mom_every))
        self.pos, self.last_m, self.day_pnl, self.day_trades = None, -1, 0.0, 0
        self._book_orphan(history[-1])
        saved = self.store.load_state(NAME, session.day)
        if saved:
            self.pos, self.last_m = saved["pos"], saved["last_m"]
            self.day_pnl, self.day_trades = saved["day_pnl"], saved["day_trades"]
            log.info("restored state for %s: pos=%s last_m=%d", session.day, self.pos, self.last_m)
        log.info("%s %s: prev close %.2f, %d history sessions, %d checks",
                 NAME, session.day, self.prev_close, len(history), len(self.checks))

    def _book_orphan(self, last: Tuple[Session, Sequence[Bar]]) -> None:
        """A position left open because the bot was down at the previous close goes out at that close."""
        session, bars = last
        saved = self.store.load_state(NAME, session.day)
        if not saved or not saved["pos"]:
            return
        log.warning("booking position left open on %s at its close", session.day)
        self._append_trade(saved["pos"], session.day, session.close, bars[-1].c, "EOD", stale=True)
        saved["pos"] = None
        self.store.save_state(NAME, session.day, saved)

    def finish_day(self) -> None:
        # Safety: normally flat by now, because poll() books the close.
        if self.pos:
            try:
                px = self.p.latest_price(self.sym)
            except Exception:
                log.exception("no price for the stale exit")
                px = None
            self._close(self.session.close, px or self.pos["entry_px"], "EOD", stale=True)
            self._save()
        log.info("%s DAY DONE %s: %d trades, net %.2f USD", NAME, self.session.day, self.day_trades, self.day_pnl)

    # ── main poll ───────────────────────────────────────────────────────────
    def poll(self, now: datetime, bars: Dict[str, List[Bar]]) -> None:
        sess = self.session
        rows = sorted((b for b in bars.get(self.sym, []) if sess.open <= b.ts < sess.close), key=lambda b: b.ts)
        if now >= sess.close + timedelta(seconds=self.cfg.settle_sec):
            if self.pos and self._eod_ready(now, rows):
                self._close(sess.close, rows[-1].c if rows else self.p.latest_price(self.sym), "EOD")
                self._save()
            return

        due = [m for m in self.checks if now >= self._check_time(m) + timedelta(seconds=self.cfg.settle_sec)]
        if not due or due[-1] == self.last_m:
            return
        m = due[-1]
        age = (now - self._check_time(m)).total_seconds()
        if age > self.cfg.max_signal_age_sec:
            log.warning("check m=%d skipped, %.0fs late", m, age)
            self.last_m = m
            self._save()
            return
        bar_start = sess.open + timedelta(minutes=m)
        if age < self.cfg.mom_bar_wait_sec and not any(b.ts == bar_start for b in rows):
            return  # the bar that closed at the check may still be arriving
        self._check(now, m, [b for b in rows if b.ts <= bar_start])

    def _check_time(self, m: int) -> datetime:
        return self.session.open + timedelta(minutes=m + 1)

    def _eod_ready(self, now: datetime, rows: Sequence[Bar]) -> bool:
        late = now >= self.session.close + timedelta(seconds=self.cfg.mom_bar_wait_sec)
        return late or (bool(rows) and rows[-1].ts >= self.session.close - timedelta(minutes=1))

    def _check(self, now: datetime, m: int, rows: Sequence[Bar]) -> None:
        if not rows:
            log.warning("check m=%d skipped, no bars today", m)
            self.last_m = m
            self._save()
            return
        if rows[0].ts != self.session.open:
            log.warning("no opening bar, using the first bar at %s", rows[0].ts)
        op, close = rows[0].o, rows[-1].c
        vol = sum(b.v for b in rows)
        vwap = sum((b.vw or b.c) * b.v for b in rows) / vol if vol else close
        sig = self.cfg.mom_vm * self.sigma[m]
        upper = max(op, self.prev_close) * (1 + sig)
        lower = min(op, self.prev_close) * (1 - sig)
        side = self.pos["side"] if self.pos else 0
        want = decide(side, close, upper, lower, vwap)
        log.info("check m=%d close=%.2f upper=%.2f lower=%.2f vwap=%.2f side=%d -> %d",
                 m, close, upper, lower, vwap, side, want)
        if want != side:
            px = self.p.latest_price(self.sym)
            if not px:
                log.warning("check m=%d: no last trade, retrying", m)
                return
            if side:
                self._close(now, px, "FLIP" if want else "SIGNAL")
            if want:
                self.pos = dict(side=want, entry_ts=now, entry_px=px, entry_close=close, upper=upper,
                                lower=lower, vwap=vwap)
                log.info("ENTER %s %d %s @ %.2f", "LONG" if want > 0 else "SHORT", self.cfg.mom_qty, self.sym, px)
        self.last_m = m
        self._save()

    # ── bookkeeping ─────────────────────────────────────────────────────────
    def _close(self, ts: datetime, px: float, reason: str, stale: bool = False) -> None:
        net = self._append_trade(self.pos, self.session.day, ts, px, reason, stale)
        log.info("EXIT %s %s @ %.2f net %.2f", reason, self.sym, px, net)
        self.day_pnl += net
        self.day_trades += 1
        self.pos = None

    def _append_trade(self, pos: dict, day, ts: datetime, px: float, reason: str, stale: bool) -> float:
        qty, entry = self.cfg.mom_qty, pos["entry_px"]
        gross = pos["side"] * qty * (px - entry)
        fees = qty * (entry + px) * self.cfg.mom_cost_bps / 1e4
        self.store.append_trade(NAME, dict(
            date=day.isoformat(), symbol=self.sym, side="LONG" if pos["side"] > 0 else "SHORT", qty=qty,
            entry_time=pos["entry_ts"].isoformat(), exit_time=ts.isoformat(), entry_px=entry, exit_px=px,
            reason=reason, gross_pnl=round(gross, 2), fees=round(fees, 2), net_pnl=round(gross - fees, 2),
            entry_close=pos["entry_close"], upper=round(pos["upper"], 4), lower=round(pos["lower"], 4),
            vwap=round(pos["vwap"], 4), stale_exit=stale,
        ), MOM_TRADE_FIELDS)
        return gross - fees

    def _save(self) -> None:
        self.store.save_state(NAME, self.session.day, dict(
            pos=self.pos, last_m=self.last_m, day_pnl=self.day_pnl, day_trades=self.day_trades))
