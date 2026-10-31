"""Service entry point for the QQQ momentum paper bot. Runs apart from the ORB30 service."""

from __future__ import annotations

import logging
import os
import signal
import threading
from dataclasses import replace

from .alpaca_data import AlpacaProvider
from .config import load_config
from .momentum import MomentumEngine
from .runner import run_forever, setup_logging
from .store import Store

log = logging.getLogger("paper_us.momentum_runner")


def main() -> None:
    try:  # manual runs: pick up ./.env (systemd passes it via EnvironmentFile)
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    cfg = load_config()
    cfg = replace(cfg, universe=(cfg.mom_symbol,))
    setup_logging(cfg.log_dir, "qqq_momentum.log")
    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY (paper keys) in the environment / .env")

    provider = AlpacaProvider(key, secret, cfg, paper=True)
    engine = MomentumEngine(cfg, provider, Store(cfg.data_dir))

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    log.info("qqq momentum starting: symbol=%s qty=%d feeds=%s/%s",
             cfg.mom_symbol, cfg.mom_qty, cfg.mom_history_feed, cfg.stock_feed)
    run_forever(cfg, provider, [engine], stop)


if __name__ == "__main__":
    main()
