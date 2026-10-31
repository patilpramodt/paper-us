"""Replays paper_us.momentum over cached SIP bars and compares with backtest/momentum.py trades, then
repeats with IEX bars as the live feed to size the feed drift. Run from the repo root: python backtest/mom_parity.py [days]"""
import os, pickle, sys, tempfile
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv()
import momentum as bt
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from paper_us.config import Config
from paper_us.momentum import NAME, MomentumEngine
from paper_us.store import Store
from paper_us.types import ET, Bar, Session

N_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 500
CACHE = "data/backtest_cache"
sessions = pickle.load(open(f"{CACHE}/idx_sessions.pkl", "rb"))
sip = {}
for rows in pickle.load(open(f"{CACHE}/idx_QQQ.pkl", "rb")).values():
    for ts, o, h, l, c, v, vw in rows:
        sip.setdefault(ts.date(), []).append(Bar(ts, o, h, l, c, v, vw or 0.0))
D = bt.load("QQQ")
days = D["days"]
sess = {d: Session(d, *sessions[d]) for d in days}
for rows in sip.values():
    rows.sort(key=lambda b: b.ts)
ref = [(days[t[0]], t[1], t[2], t[3], t[4]) for t in bt.noise(D, bt.prep(D))]
test_days = days[-N_DAYS:]


class Replay:
    def __init__(self, live):
        self.live, self.now, self.day = live, None, None

    def history_sessions(self, symbol, before_day, n):
        i = days.index(before_day)
        return [(sess[d], sip[d]) for d in days[i - n:i]]

    def latest_price(self, symbol):  # the backtest's fill: the open of the bar after the check
        s = sess[self.day]
        ts = s.open + timedelta(minutes=int((self.now - s.open).total_seconds() // 60))
        hit = [b for b in sip[self.day] if b.ts == ts]
        return hit[0].o if hit else [b for b in sip[self.day] if b.ts < ts][-1].c


def replay(live, day_list):
    cfg = Config(universe=("QQQ",))
    tmp = tempfile.mkdtemp()
    prov, st = Replay(live), Store(tmp)
    e = MomentumEngine(cfg, prov, st)
    for d in day_list:
        s = sess[d]
        e.start_day(s)
        prov.day = d
        rows = live.get(d, [])
        now = s.open + timedelta(minutes=30, seconds=20)
        while now <= s.close + timedelta(seconds=80):
            prov.now = now
            cut = now - timedelta(minutes=1)
            e.poll(now, {"QQQ": [b for b in rows if b.ts <= cut]})
            now += timedelta(seconds=30)
        e.finish_day()
    import csv
    p = st.trades_path(NAME)
    return list(csv.DictReader(open(p))) if os.path.exists(p) else []


def key(r):
    a, b = datetime.fromisoformat(r["entry_time"]).astimezone(ET), datetime.fromisoformat(r["exit_time"]).astimezone(ET)
    d = a.date()
    o = sess[d].open
    return d, (1 if r["side"] == "LONG" else -1), int((a - o).total_seconds() // 60), int((b - o).total_seconds() // 60)


s0 = set(test_days)
want = {(d, s, a, b): x for d, s, a, b, x in ref if d in s0}
rows = replay(sip, test_days)
got = {key(r): r for r in rows}
common = want.keys() & got.keys()
print(f"SIP replay {test_days[0]}..{test_days[-1]}: backtest {len(want)} trades, engine {len(got)}, identical {len(common)}")
for k in sorted(want.keys() - got.keys())[:10]:
    print(" missing", k)
for k in sorted(got.keys() - want.keys())[:10]:
    print(" extra  ", k)
err = max((abs(want[k] - float(got[k]["gross_pnl"]) / (50 * float(got[k]["entry_px"]))) for k in common), default=0)
print(f"largest per-trade return gap {err*1e4:.3f}bp")

# ── live feed drift ─────────────────────────────────────────────────────────
stock = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
p = f"{CACHE}/idx_QQQ_iex.pkl"
if os.path.exists(p):
    iex = pickle.load(open(p, "rb"))
else:
    res = stock.get_stock_bars(StockBarsRequest(
        symbol_or_symbols="QQQ", timeframe=TimeFrame.Minute, adjustment=Adjustment.RAW,
        start=sess[test_days[0]].open.astimezone(timezone.utc), end=sess[test_days[-1]].close.astimezone(timezone.utc),
        feed=DataFeed.IEX)).data.get("QQQ", [])
    iex = {}
    for b in res:
        ts = b.timestamp.astimezone(ET)
        s = sess.get(ts.date())
        if s and s.open <= ts < s.close:
            iex.setdefault(ts.date(), []).append(Bar(ts, b.open, b.high, b.low, b.close, b.volume, b.vwap or 0.0))
    pickle.dump(iex, open(p, "wb"))
cov = [len(iex.get(d, [])) / max(1, len(sip[d])) for d in test_days]
print(f"IEX minute coverage vs SIP: mean {sum(cov)/len(cov):.1%}, worst {min(cov):.1%}; opening bar present {sum(1 for d in test_days if iex.get(d) and iex[d][0].ts == sess[d].open)}/{len(test_days)}")
gaps = [abs(iex[d][0].o / sip[d][0].o - 1) * 1e4 for d in test_days if iex.get(d) and iex[d][0].ts == sess[d].open]
gaps.sort()
print(f"open gap IEX vs SIP: median {gaps[len(gaps)//2]:.2f}bp, 95th {gaps[int(len(gaps)*.95)]:.2f}bp, max {gaps[-1]:.2f}bp")
rows2 = replay(iex, test_days)
got2 = {key(r): r for r in rows2}
same = got.keys() & got2.keys()
pnl = lambda g: sum(float(r["net_pnl"]) for r in g.values())
print(f"IEX-driven engine: {len(got2)} trades, identical to SIP-driven {len(same)}, only SIP {len(got.keys()-got2.keys())}, only IEX {len(got2.keys()-got.keys())}")
print(f"net P&L, 50 shares, same SIP fills: SIP-driven ${pnl(got):,.0f}  IEX-driven ${pnl(got2):,.0f}")
