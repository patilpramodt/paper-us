"""Drive run_forever with a fake clock through a whole simulated day."""

import threading
from datetime import timedelta

from conftest import DAY, FakeProvider, OPEN, day_bars, t

from paper_us.config import Config
from paper_us.engine import Orb30Engine
from paper_us.runner import run_forever
from paper_us.store import Store


def test_full_day_loop_enters_and_squares_off(tmp_path):
    cfg = Config(universe=("AAA",), poll_sec=60, settle_sec=15)
    prov = FakeProvider()
    prov.bars["AAA"] = day_bars()
    prov.hist["AAA"] = [1000.0] * 14
    prov.price["AAA"] = 102.0
    store = Store(str(tmp_path))
    engines = [Orb30Engine(v, cfg, prov, store) for v in cfg.variants]

    stop = threading.Event()
    clock = {"now": t(9, 0)}                    # service wakes up pre-open
    end = t(16, 10)

    def sleep(sec):
        clock["now"] += timedelta(seconds=sec)
        if clock["now"] > end:
            stop.set()

    run_forever(cfg, prov, engines, stop, clock=lambda: clock["now"], sleep=sleep)

    import csv
    for v in cfg.variants:
        rows = list(csv.DictReader(open(store.trades_path(v.name))))
        assert len(rows) == 1 and rows[0]["reason"] == "EOD"
