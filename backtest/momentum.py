"""Index step 2: intraday momentum on SPY/QQQ underlying, no options. Rules fixed before looking at results.
NOISE: Zarattini/Aziz/Barbon noise-area. Bands = max/min(open, prev close) * (1 +/- vm * sigma_m), where
sigma_m = mean |close_m/open - 1| at that minute of day over the previous `look` days (today excluded).
Checks every `every` minutes from 10:00; fills at the NEXT bar's open; stop = VWAP or band; flat at the close.
GAO: sign of the first half hour (from prev close) sets the side for the last half hour.
Run from the repo root: python backtest/momentum.py"""
import pickle, sys
from collections import defaultdict
import numpy as np

CACHE = "data/backtest_cache"
N = 390

def load(sym):
    sessions = pickle.load(open(f"{CACHE}/idx_sessions.pkl", "rb"))
    days = sorted(sessions)
    ix = {d: i for i, d in enumerate(days)}
    A = np.full((len(days), N, 6), np.nan)     # o h l c v vwap
    for rows in pickle.load(open(f"{CACHE}/idx_{sym}.pkl", "rb")).values():
        for ts, o, h, l, c, v, vw in rows:
            i = ix[ts.date()]
            m = int((ts - sessions[ts.date()][0]).total_seconds() // 60)
            if 0 <= m < N:
                A[i, m] = (o, h, l, c, v, vw)
    last = np.array([int((sessions[d][1] - sessions[d][0]).total_seconds() // 60) for d in days])  # bars in session
    keep = np.isfinite(A[:, 0, 0]) & np.isfinite(A[:, :, 3]).any(axis=1)
    return dict(days=[d for d, k in zip(days, keep) if k], A=A[keep], L=last[keep])

def prep(D, look=14, vm=1.0):
    A, L = D["A"], D["L"]
    op = A[:, 0, 0]
    C = A[:, :, 3].copy()
    for m in range(1, N):                      # carry the last close over empty minutes, within the session
        C[:, m] = np.where(np.isnan(C[:, m]) & (m < L), C[:, m - 1], C[:, m])
    O = np.where(np.isfinite(A[:, :, 0]), A[:, :, 0], np.roll(C, 1, axis=1))
    O[:, 0] = op
    pc = np.array([np.nan] + [C[i, min(L[i], N) - 1] for i in range(len(L) - 1)])
    mv = np.abs(C / op[:, None] - 1)
    sig = np.full(C.shape, np.nan)
    for i in range(look, len(L)):
        sig[i] = np.nanmean(mv[i - look:i], axis=0)
    hi, lo = np.maximum(op, pc)[:, None], np.minimum(op, pc)[:, None]
    v = np.nan_to_num(A[:, :, 4])
    vw = np.nan_to_num(A[:, :, 5])
    with np.errstate(invalid="ignore", divide="ignore"):
        cv = np.cumsum(vw * v, axis=1) / np.cumsum(v, axis=1)
    dr = np.array([np.nan] + [C[i, min(L[i], N) - 1] / C[i - 1, min(L[i - 1], N) - 1] - 1 for i in range(1, len(L))])
    return dict(C=C, O=O, UB=hi * (1 + vm * sig), LB=lo * (1 - vm * sig), vwap=cv, dr=dr)

def noise(D, P, every=30, offset=0, delay=0):
    """Returns trades (day_idx, side, entry_m, exit_m, ret)."""
    C, O, UB, LB, VW, L = P["C"], P["O"], P["UB"], P["LB"], P["vwap"], D["L"]
    tr = []
    for d in range(len(L)):
        n = min(L[d], N)
        if not np.isfinite(UB[d, 29]):
            continue
        side, em, ep = 0, 0, 0.0
        def close(m, px):
            tr.append((d, side, em, m, side * (px / ep - 1)))
        for m in range(29 + offset, n - 1 - delay, every):
            c, ub, lb = C[d, m], UB[d, m], LB[d, m]
            f = m + 1 + delay
            want = side
            if side == 1:
                if c < max(ub, VW[d, m]):
                    want = -1 if c < lb else 0
            elif side == -1:
                if c > min(lb, VW[d, m]):
                    want = 1 if c > ub else 0
            elif c > ub:
                want = 1
            elif c < lb:
                want = -1
            if want != side:
                if side:
                    close(f, O[d, f])
                side = want
                if side:
                    em, ep = f, O[d, f]
        if side:
            close(n - 1, C[d, n - 1])
    return tr

def daily(D, tr, cost_bps, sized=None):
    r = np.zeros(len(D["L"]))
    for d, s, a, b, x in tr:
        r[d] += (x - 2 * cost_bps / 1e4) * (1 if sized is None else sized[d])
    return r

def sizing(P, target=0.02, cap=4.0, look=14):
    x = np.full(len(P["dr"]), 0.0)
    for i in range(look + 1, len(x)):
        s = np.std(P["dr"][i - look:i], ddof=1)
        x[i] = min(cap, target / s) if s > 0 else 0
    return x

def perf(r, start=15):
    r = r[start:]
    cum = np.cumsum(r)
    dd = (np.maximum.accumulate(cum) - cum).max()
    sd = r.std(ddof=1)
    rng = np.random.default_rng(1)
    b = rng.choice(r, (4000, len(r))).mean(axis=1) * 252 * 100
    return dict(ann=r.mean() * 252 * 100, sharpe=r.mean() / sd * 252 ** .5 if sd else 0, dd=dd * 100,
                ci=(np.percentile(b, 2.5), np.percentile(b, 97.5)), tot=r.sum() * 100)

def pline(lbl, r):
    p = perf(r)
    return (f"{lbl:<30} ann={p['ann']:>6.1f}% CI[{p['ci'][0]:>6.1f},{p['ci'][1]:>6.1f}] sharpe={p['sharpe']:>5.2f} "
            f"maxDD={p['dd']:>5.1f}%")

COST = 0.2  # bps per side: about half a cent spread + a little slippage on a $500 share
if __name__ == "__main__":
    res = {}
    for sym in ("SPY", "QQQ"):
        D = load(sym)
        days, L = D["days"], D["L"]
        P = prep(D)
        tr = noise(D, P)
        r0 = daily(D, tr, 0)
        r = daily(D, tr, COST)
        z = sizing(P)
        rv = daily(D, tr, COST, z)
        res[sym] = (days, r, rv)
        yrs = np.array([d.year for d in days])
        C = P["C"]
        print(f"\n===== {sym} {days[0]}..{days[-1]} days={len(days)} early_close={int((L<N).sum())} "
              f"missing_minutes={int(np.isnan(D['A'][:,:,3][np.arange(N)[None,:]<L[:,None]]).sum())}")
        oc = np.array([C[i, min(L[i], N) - 1] / D['A'][i, 0, 0] - 1 for i in range(len(L))])
        print(pline("benchmark close-close", np.nan_to_num(P["dr"])))
        print(pline("benchmark open-close long", oc))
        rets = np.array([t[4] for t in tr])
        hold = np.array([t[3] - t[2] for t in tr])
        print(f"NOISE trades={len(tr)} per_day={len(tr)/len(days):.2f} win={np.mean(rets>0):.0%} "
              f"avg_trade={rets.mean()*1e4:.2f}bp hold={hold.mean():.0f}m | avg_win={rets[rets>0].mean()*1e4:.0f}bp avg_loss={rets[rets<=0].mean()*1e4:.0f}bp")
        print(pline("NOISE gross 1x", r0))
        for c in (0.2, 0.5, 1.0, 2.0):
            print(pline(f"NOISE net {c}bp/side 1x", daily(D, tr, c)))
        print(pline("NOISE net vol-target 2%, cap 4x", rv))
        for s, l in ((1, "long only"), (-1, "short only")):
            print(pline(f"NOISE {l}", daily(D, [t for t in tr if t[1] == s], COST)))
        ex = np.sort(r)[::-1]
        print(f"top days: best5 sum={ex[:5].sum()*100:.1f}% best20={ex[:20].sum()*100:.1f}% total={r.sum()*100:.1f}%")
        m = r[15:] ; b = oc[15:]
        print(f"beta to open-close={np.cov(m,b)[0,1]/b.var():.3f} corr={np.corrcoef(m,b)[0,1]:.3f}")
        print("-- by year, 1x net")
        for y in sorted(set(yrs)):
            k = yrs == y
            print(f"{y} n={int((np.array([t[0] for t in tr])[:, None] == np.where(k)[0]).any(axis=1).sum()):<4} net={r[k].sum()*100:>6.1f}% sharpe={perf(r[k],0)['sharpe']:>5.2f}  bench_oc={oc[k].sum()*100:>6.1f}%")
        k = yrs <= 2021
        print(pline("first half 2016-21", r[k]))
        print(pline("second half 2022-26", r[~k]))
        print("-- robustness, 1x net at 0.2bp: fill delay / check offset")
        for dl in (0, 1, 2, 5):
            print(pline(f"delay {dl}m", daily(D, noise(D, P, delay=dl), COST)))
        for of in (5, 10, 15, 20):
            print(pline(f"offset +{of}m", daily(D, noise(D, P, offset=of), COST)))
        print("-- grid sharpe, 1x net (rows look, cols vm 0.75/1/1.5/2), every=30")
        for look in (7, 14, 30, 60):
            row = []
            for vm in (.75, 1, 1.5, 2):
                row.append(perf(daily(D, noise(D, prep(D, look, vm)), COST), 61)["sharpe"])
            print(f"look {look:<3}", " ".join(f"{x:>6.2f}" for x in row))
        for ev in (15, 60):
            print(f"every {ev}m sharpe={perf(daily(D, noise(D, P, every=ev), COST))['sharpe']:.2f}")
        # GAO: full days only
        g = np.zeros(len(L)); gs = []
        for i in range(1, len(L)):
            if L[i] < N:
                continue
            s = np.sign(C[i, 29] / np.nan_to_num(C[i-1, min(L[i-1], N)-1], nan=C[i, 29]) - 1)
            g[i] = s * (C[i, 389] / D["A"][i, 360, 0] - 1) - 2 * COST / 1e4 if np.isfinite(D["A"][i, 360, 0]) else 0
        print(pline("GAO first->last 30m net", g))
        kk = yrs <= 2021
        print(pline("GAO 2016-21", g[kk])); print(pline("GAO 2022-26", g[~kk]))
    d = res["SPY"][0]
    if d == res["QQQ"][0]:
        print("\n===== 50/50 SPY+QQQ")
        print(pline("1x net", (res["SPY"][1] + res["QQQ"][1]) / 2))
        print(pline("vol-target net", (res["SPY"][2] + res["QQQ"][2]) / 2))
        print(f"corr SPY/QQQ strat={np.corrcoef(res['SPY'][1], res['QQQ'][1])[0,1]:.2f}")
    pickle.dump({s: (v[0], v[1]) for s, v in res.items()}, open(f"{CACHE}/momentum_daily.pkl", "wb"))
