"""Configuration. Every tunable lives here; env vars override a few of them.

NOTE: the numeric defaults below are my translation of the NSE rules in
paper-main (orb30_rvol_strategy.py) to US stocks. They have NOT been validated
on US data. Treat the first weeks of paper results as the test.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import time
from typing import Optional, Tuple

DEFAULT_UNIVERSE = ("NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "META", "GOOGL")


@dataclass(frozen=True)
class Variant:
    """One strategy variant. Both share every rule except the RVOL bar and the exit."""

    name: str
    rvol_min: float
    trail_step_usd: Optional[float] = None  # None = no trail (candle SL / EOD only)


@dataclass(frozen=True)
class Config:
    # ── universe / session (all times America/New_York) ─────────────────────
    universe: Tuple[str, ...] = DEFAULT_UNIVERSE
    bar_minutes: int = 30
    last_entry_bar_start: time = time(14, 30)  # candle closing 15:00 is the last trigger
    squareoff_before_close_min: int = 15       # 15:45 on a normal day, 12:45 on a half day
    settle_sec: int = 15                       # wait this long after a candle ends before using it
    max_signal_age_sec: int = 180              # ignore a breakout older than this (restart safety)

    # ── levels (fractions of price; original NSE rules used absolute points) ─
    mark_buffer_pct: float = 0.0005            # OR high/low +/- 0.05%
    sl_buffer_pct: float = 0.0010              # SL beyond the breakout candle by 0.10%

    # ── RVOL ────────────────────────────────────────────────────────────────
    rvol_lookback: int = 14
    rvol_min_days: int = 10

    # ── option selection / entry gates ──────────────────────────────────────
    contracts: int = 1
    min_dte: int = 2                           # skip expiries closer than this many calendar days
    expiry_window_days: int = 14               # look for expiries within min_dte .. min_dte+this
    strike_band_pct: float = 0.08              # only fetch strikes within +/-8% of spot
    min_premium: float = 0.50                  # ask must be at least this (USD per share)
    max_spread_pct: float = 0.10               # (ask-bid)/mid must be <= this
    entry_retry_sec: int = 90                  # keep retrying a signal this long if the quote is bad

    # ── paper fills / costs ─────────────────────────────────────────────────
    fee_per_contract: float = 0.65             # per side, per contract (broker-dependent: set yours)
    extra_slip_per_share: float = 0.00         # added to ask on buys, subtracted from bid on sells

    # ── QQQ momentum (backtest/momentum.py settings, don't tune) ─────────────
    mom_symbol: str = "QQQ"
    mom_look: int = 14                         # sessions behind sigma
    mom_vm: float = 1.0                        # band width in sigmas
    mom_every: int = 30                        # minutes between checks
    mom_qty: int = 50                          # shares, same as the option-replay comparison
    mom_cost_bps: float = 0.2                  # per side
    mom_history_feed: str = "sip"              # same feed as the backtest, for past sessions only
    mom_bar_wait_sec: int = 60                 # how long after a check to wait for its bar

    # ── runtime ─────────────────────────────────────────────────────────────
    poll_sec: int = 10
    prestart_min: int = 10                     # load history this long before the open
    stock_feed: str = "iex"                    # iex (free, real-time) | delayed_sip
    option_feed: str = "indicative"            # indicative (free) | opra (paid)
    data_dir: str = "data"
    log_dir: str = "logs"

    variants: Tuple[Variant, ...] = field(
        default_factory=lambda: (
            Variant("ORB30_RVOL", rvol_min=2.0, trail_step_usd=None),
            # $100 per contract = $1.00 premium move. Original NSE rule was Rs1000 per lot.
            Variant("ORB30_RVOL_TRAIL", rvol_min=2.5, trail_step_usd=100.0),
        )
    )


def load_config() -> Config:
    """Defaults + a handful of env overrides (see .env.example)."""
    cfg = Config()
    kw = {}
    if os.getenv("UNIVERSE"):
        kw["universe"] = tuple(s.strip().upper() for s in os.environ["UNIVERSE"].split(",") if s.strip())
    for env, attr, cast in (
        ("POLL_SEC", "poll_sec", int),
        ("FEE_PER_CONTRACT", "fee_per_contract", float),
        ("MIN_DTE", "min_dte", int),
        ("MAX_SPREAD_PCT", "max_spread_pct", float),
        ("STOCK_FEED", "stock_feed", str),
        ("OPTION_FEED", "option_feed", str),
        ("DATA_DIR", "data_dir", str),
        ("LOG_DIR", "log_dir", str),
    ):
        if os.getenv(env):
            kw[attr] = cast(os.environ[env])
    cfg = replace(cfg, **kw)
    if os.getenv("TRAIL_STEP_USD"):
        step = float(os.environ["TRAIL_STEP_USD"])
        cfg = replace(
            cfg,
            variants=tuple(
                replace(v, trail_step_usd=step) if v.trail_step_usd is not None else v for v in cfg.variants
            ),
        )
    return cfg
