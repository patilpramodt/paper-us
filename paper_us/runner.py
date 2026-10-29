"""Long-running service loop: waits for the session, polls, squares off, sleeps to next day."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler

from .alpaca_data import AlpacaProvider
from .config import Config, load_config
from .engine import Orb30Engine
from .store import Store
from .types import ET

log = logging.getLogger("paper_us.runner")


def now_et() -> datetime:
    return datetime.now(ET)


def setup_logging(log_dir: str) -> None:
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(os.path.join(log_dir, "paper_us.log"), maxBytes=5_000_000, backupCount=5)
    sh = logging.StreamHandler()
    for h in (fh, sh):
        h.setFormatter(fmt)
        root.addHandler(h)


def run_forever(cfg: Config, provider, engines, stop: threading.Event, clock=now_et, sleep=None) -> None:
    sleep = sleep or (lambda s: stop.wait(s))
    started: date | None = None
    finished: date | None = None
    errors = 0

    while not stop.is_set():
        now = clock()
        day = now.date()
        try:
            sess = provider.session(day)
        except Exception:
            log.exception("calendar lookup failed; retrying in 60s")
            sleep(60)
            continue

        if sess is None:  # weekend / holiday
            log.info("%s: market closed, sleeping", day)
            sleep(30 * 60)
            continue

        if now >= sess.close + timedelta(minutes=2):
            if started == day and finished != day:
                for e in engines:
                    e.finish_day()
                finished = day
            sleep(15 * 60)
            continue

        if now < sess.open - timedelta(minutes=cfg.prestart_min):
            sleep(min((sess.open - timedelta(minutes=cfg.prestart_min) - now).total_seconds(), 15 * 60))
            continue

        if started != day:
            try:
                for e in engines:
                    e.start_day(sess)
                started = day
            except Exception:
                log.exception("start_day failed; retrying in 30s")
                sleep(30)
                continue

        if now < sess.open:
            sleep(5)
            continue

        try:
            bars = provider.minute_bars(cfg.universe, sess, now)
            for e in engines:
                e.poll(now, bars)
            errors = 0
        except Exception:
            errors += 1
            log.exception("poll failed (%d in a row)", errors)
            sleep(min(5 * errors, 60))
            continue
        sleep(cfg.poll_sec)

    log.info("stop requested - persisting state")
    for e in engines:
        if e.session is not None:
            e._save()


def main() -> None:
    try:  # manual runs: pick up ./.env (systemd passes it via EnvironmentFile)
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    cfg = load_config()
    setup_logging(cfg.log_dir)
    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY (paper keys) in the environment / .env")

    provider = AlpacaProvider(key, secret, cfg, paper=True)
    store = Store(cfg.data_dir)
    engines = [Orb30Engine(v, cfg, provider, store) for v in cfg.variants]

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    log.info("paper-us starting: variants=%s universe=%s feeds=%s/%s",
             [v.name for v in cfg.variants], list(cfg.universe), cfg.stock_feed, cfg.option_feed)
    run_forever(cfg, provider, engines, stop)


if __name__ == "__main__":
    main()
