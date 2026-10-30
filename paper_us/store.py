"""Trade CSV + per-day JSON state (so a restart mid-day does not forget positions)."""

from __future__ import annotations

import csv
import json
import os
from datetime import date, datetime
from typing import Any, Dict, Optional

TRADE_FIELDS = [
    "date", "variant", "symbol", "side", "contract", "strike", "expiry",
    "entry_time", "exit_time", "qty", "entry_px", "exit_px", "reason",
    "gross_pnl", "fees", "net_pnl", "peak_gross", "rvol", "or_high", "or_low",
    "sl_level", "spot_entry", "spot_exit", "entry_spread_pct", "stale_exit",
]


class Store:
    def __init__(self, data_dir: str):
        self.dir = data_dir
        os.makedirs(self.dir, exist_ok=True)

    # ── trades ──────────────────────────────────────────────────────────────
    def trades_path(self, variant: str) -> str:
        return os.path.join(self.dir, f"{variant.lower()}_trades.csv")

    def append_trade(self, variant: str, row: Dict[str, Any]) -> None:
        path = self.trades_path(variant)
        new = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=TRADE_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)

    # ── state ───────────────────────────────────────────────────────────────
    def state_path(self, variant: str, day: date) -> str:
        return os.path.join(self.dir, f"state_{variant.lower()}_{day.isoformat()}.json")

    def save_state(self, variant: str, day: date, payload: Dict[str, Any]) -> None:
        path = self.state_path(variant, day)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, default=_enc)
        os.replace(tmp, path)

    def load_state(self, variant: str, day: date) -> Optional[Dict[str, Any]]:
        path = self.state_path(variant, day)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f, object_hook=_dec)


def _enc(o: Any) -> Any:
    if isinstance(o, datetime):
        return {"__dt__": o.isoformat()}
    if isinstance(o, date):
        return {"__d__": o.isoformat()}
    raise TypeError(f"not serialisable: {type(o)}")


def _dec(d: Dict[str, Any]) -> Any:
    if "__dt__" in d:
        return datetime.fromisoformat(d["__dt__"])
    if "__d__" in d:
        return date.fromisoformat(d["__d__"])
    return d
