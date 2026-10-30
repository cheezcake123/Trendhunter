"""Spot sleeve rules: time-series momentum ensemble + volatility targeting on a volume-ranked universe.

Pure functions (standard library only) shared by the live bot and the in-bot backtest, so both
follow exactly the same rules as the research tournament:
  signal_i  = average over lookbacks L of [close_t > close_{t-L}]          (0, .25, .5, .75, 1)
  weight_i  = signal_i * (vol_target / N) / max(ann_vol_60d_i, 15%)        (capped at max_weight)
  gross     = sum(weight) <= 1  (spot: no leverage, the rest stays in USDT)
  universe  = top-N candidates by average 365-day quote volume, refreshed monthly
  BTC gate  = when BTC's own signal is below spot_btc_gate_min, every coin except BTC gets weight 0
"""
from __future__ import annotations

import math
import time

DAY_MS = 86_400_000
BTC = "BTCUSDT"


def daily_series(rows, now_ms=None):
    """Binance kline rows -> dict(t, c, qv) of CLOSED daily candles."""
    t, c, qv = [], [], []
    for k in rows:
        close_time = int(k[6])
        if now_ms is not None and close_time >= now_ms:
            continue
        t.append(int(k[0]))
        c.append(float(k[4]))
        qv.append(float(k[7]) if len(k) > 7 else float(k[5]) * float(k[4]))
    return {"t": t, "c": c, "qv": qv}


def momentum_fraction(closes, lookbacks):
    """Share of lookbacks with a positive return. Missing history counts as 'not up'."""
    if not closes:
        return 0.0
    last = closes[-1]
    up = 0
    for L in lookbacks:
        L = int(L)
        if len(closes) > L and last > closes[-1 - L]:
            up += 1
    return up / float(len(lookbacks))


def ann_vol(closes, n=60, floor=0.15):
    """Annualised close-to-close volatility (sample std, 365 days). None if not enough data."""
    if len(closes) < n + 1:
        return None
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(len(closes) - n, len(closes))]
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return max(math.sqrt(var) * math.sqrt(365.0), floor)


def select_universe(series, candidates, n=6, lookback=365, min_days=365):
    """Top-n candidates by average quote volume over the last `lookback` closed days."""
    scored = []
    for sym in candidates:
        s = series.get(sym)
        if not s or len(s["c"]) < min_days:
            continue
        qv = s["qv"][-lookback:]
        scored.append((sum(qv) / max(len(qv), 1), sym))
    scored.sort(reverse=True)
    return [sym for _, sym in scored[:int(n)]]


def btc_gate(series, p):
    """BTC trend gate. None when it is switched off or BTC lacks the daily history for every lookback
    (then nothing is gated); else {"frac": BTC momentum fraction, "min": threshold, "open": alts allowed}."""
    if not p.get("spot_btc_gate", True):
        return None
    lbs = [int(x) for x in p.get("spot_lookbacks", [20, 60, 120, 250])]
    s = series.get(BTC)
    if not s or not lbs or len(s["c"]) <= max(lbs):
        return None
    frac = momentum_fraction(s["c"], lbs)
    lo = float(p.get("spot_btc_gate_min", 0.5))
    return {"frac": frac, "min": lo, "open": frac >= lo}


def target_weights(series, universe, p):
    """Returns ({sym: weight}, {sym: diag}) for the universe. Weights sum to <= max_gross.
    A closed BTC gate zeroes every coin except BTC, whose weight stays what it would have been."""
    lbs = [int(x) for x in p.get("spot_lookbacks", [20, 60, 120, 250])]
    vt = float(p.get("spot_vol_target", 0.6))
    vn = int(p.get("spot_vol_days", 60))
    max_w = float(p.get("spot_max_weight", 1.0))
    max_gross = float(p.get("spot_max_gross", 1.0))
    min_hist = int(p.get("spot_min_history_days", 200))
    live = [s for s in universe if s in series and len(series[s]["c"]) >= min_hist]
    n = max(len(live), 1)
    w, diag = {}, {}
    for sym in live:
        c = series[sym]["c"]
        frac = momentum_fraction(c, lbs)
        v = ann_vol(c, vn)
        if v is None:
            w[sym] = 0.0
            diag[sym] = {"frac": frac, "vol": None}
            continue
        w[sym] = max(0.0, min(max_w, frac * (vt / n) / v))
        diag[sym] = {"frac": frac, "vol": v}
    g = sum(w.values())
    if g > max_gross > 0:
        k = max_gross / g
        w = {s: x * k for s, x in w.items()}
    gate = btc_gate(series, p)
    if gate and not gate["open"]:
        w = {s: (x if s == BTC else 0.0) for s, x in w.items()}
    return w, diag


def plan_trades(holdings_val, targets_w, equity, band, min_notional):
    """Decide which assets to trade (same band rule as the research simulator).

    holdings_val: {sym: current value in USDT}; targets_w: {sym: weight}; min_notional: {sym: USDT} or a number.
    A position below min_notional counts as zero (dust). A trade happens when a position opens/closes
    or the change exceeds band x equity. Returns [(sym, delta_usdt, full_exit)] — sells first, then buys.
    """
    out = []
    for sym in set(holdings_val) | set(targets_w):
        mn = float(min_notional.get(sym, 5.0)) if isinstance(min_notional, dict) else float(min_notional)
        cur = float(holdings_val.get(sym, 0.0))
        tgt = float(targets_w.get(sym, 0.0)) * equity
        cur_z, tgt_z = cur < mn, tgt < mn
        if cur_z and tgt_z:
            continue
        if tgt_z:
            tgt = 0.0
        d = tgt - cur
        if not ((cur_z != tgt_z) or abs(d) > band * equity):
            continue
        if tgt_z:
            out.append((sym, -cur, True))
        elif cur_z and d > 0:
            out.append((sym, max(d, mn), False))  # opening from dust: at least the exchange minimum
        elif abs(d) >= mn:
            out.append((sym, d, False))
    sells = sorted([x for x in out if x[1] < 0], key=lambda x: x[1])
    buys = sorted([x for x in out if x[1] > 0], key=lambda x: -x[1])
    return sells + buys


def month_key(ms):
    g = time.gmtime(ms / 1000.0)
    return g.tm_year * 100 + g.tm_mon


# ---------------------------------------------------------------- backtest (pure python)

def simulate(series, p, start_ms, fee=0.001, slip=0.0005, start_equity=100.0):
    """Daily spot simulation of the live rules. series: {sym: daily_series}. Returns [(t_close_ms, equity)]."""
    cands = list(series)
    days = sorted({t for s in series.values() for t in s["t"] if t >= start_ms})
    idx = {sym: {t: i for i, t in enumerate(s["t"])} for sym, s in series.items()}
    cash = float(start_equity)
    qty = {s: 0.0 for s in cands}
    last_px = {}
    universe, uni_month = [], None
    curve = []
    band = float(p.get("spot_band_pct", 5)) / 100.0
    n_uni = int(p.get("spot_universe_size", 6))
    lb = int(p.get("spot_universe_lookback_days", 365))
    for t in days:
        view = {}
        for sym in cands:
            i = idx[sym].get(t)
            if i is None:
                continue
            last_px[sym] = series[sym]["c"][i]
            s = series[sym]
            view[sym] = {"t": s["t"][:i + 1], "c": s["c"][:i + 1], "qv": s["qv"][:i + 1]}
        month = month_key(t + DAY_MS)
        if uni_month != month or not universe:
            universe = select_universe(view, cands, n_uni, lb, int(p.get("spot_universe_min_days", 365))) or universe
            uni_month = month
        eq = cash + sum(qty[s] * last_px.get(s, 0.0) for s in cands)
        w, _ = target_weights(view, universe, p)
        vals = {s: qty[s] * last_px[s] for s in cands if qty[s] > 0 and s in last_px}
        for sym, d, full in plan_trades(vals, w, eq, band, 5.0):
            px_ = last_px.get(sym)
            if not px_:
                continue
            if d < 0:
                q = qty[sym] if full else min(qty[sym], -d / px_)
                cash += q * px_ * (1 - slip) * (1 - fee)
                qty[sym] -= q
            else:
                spend = min(d, cash)
                if spend <= 0:
                    continue
                qty[sym] += spend / (px_ * (1 + slip)) * (1 - fee)
                cash -= spend
        eq = cash + sum(qty[s] * last_px.get(s, 0.0) for s in cands)
        curve.append((t + DAY_MS, eq))
    return curve
