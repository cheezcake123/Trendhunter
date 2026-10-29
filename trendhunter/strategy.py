"""TrendHunter strategy: higher-timeframe four-MA trend filter (default 1D) + Donchian breakout on the
signal timeframe (default 4H).

Everything here is a pure function so the live bot and the backtester
share exactly the same rules.
"""
from __future__ import annotations

import bisect

from indicators import adx, atr, donchian, ema, moving_average, rsi

LONG = "LONG"
SHORT = "SHORT"
NEUTRAL = "NEUTRAL"

REASON_KO = {
    "WARMUP": "데이터 부족(지표 계산 전)",
    "REGIME_NEUTRAL": "큰 추세 중립(이평선 혼조)",
    "NO_BREAKOUT": "아직 돌파 없음",
    "BELOW_EMA": "20EMA 반대편",
    "WEAK_TREND": "추세 강도 약함(ADX 미달)",
    "OVERHEATED": "과열(RSI 극단) — 추격 금지",
    "CHASE": "평균선에서 너무 멀어짐 — 추격 금지",
    "SPIKE": "급등락 장대봉 — 추격 금지",
    "BAD_ATR": "변동성 계산 오류",
    "DIRECTION_OFF": "이 방향 매매는 꺼져 있음",
    "BREAKOUT_LONG": "롱 진입 신호",
    "BREAKOUT_SHORT": "숏 진입 신호",
}

REGIME_KO = {LONG: "상승 정배열", SHORT: "하락 역배열", NEUTRAL: "중립"}


def candles_from_klines(raw, now_ms=None):
    """Binance kline rows -> dict of lists. Drops candles that have not closed yet."""
    out = {"t": [], "o": [], "h": [], "l": [], "c": [], "v": [], "tc": []}
    for k in raw:
        close_time = int(k[6])
        if now_ms is not None and close_time >= now_ms:
            continue
        out["t"].append(int(k[0]))
        out["o"].append(float(k[1]))
        out["h"].append(float(k[2]))
        out["l"].append(float(k[3]))
        out["c"].append(float(k[4]))
        out["v"].append(float(k[5]))
        out["tc"].append(close_time)
    return out


def prepare_1h(c, p):
    hi, lo = donchian(c["h"], c["l"], int(p["donchian_len"]))
    return {
        "t": c["t"], "tc": c["tc"], "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"],
        "ema20": ema(c["c"], 20),
        "atr": atr(c["h"], c["l"], c["c"], int(p["atr_len"])),
        "adx": adx(c["h"], c["l"], c["c"], int(p["adx_len"])),
        "rsi": rsi(c["c"], int(p["rsi_len"])),
        "dch": hi,
        "dcl": lo,
    }


def prepare_4h(c, p):
    periods = [int(x) for x in p["ma_periods"]]
    kind = p.get("ma_type", "EMA")
    rule = p.get("regime_rule", "strict")
    mas = [moving_average(c["c"], n, kind) for n in periods]
    regime = []
    for i in range(len(c["c"])):
        vals = [m[i] for m in mas]
        if any(v is None for v in vals):
            regime.append(NEUTRAL)
            continue
        m_fast, m_mid, m_slow, m_base = vals[0], vals[1], vals[2], vals[3]
        close = c["c"][i]
        if rule == "loose":  # price on the right side of the slowest MA + fast above mid
            up = m_fast > m_mid and close > m_base
            down = m_fast < m_mid and close < m_base
        else:  # strict: full 20 > 60 > 120 alignment (정배열) + price beyond 200
            up = m_fast > m_mid > m_slow and close > m_base
            down = m_fast < m_mid < m_slow and close < m_base
        regime.append(LONG if up else SHORT if down else NEUTRAL)
    return {"tc": c["tc"], "c": c["c"], "regime": regime, "mas": dict(zip(periods, mas))}


def regime_at(r4, t_close_ms):
    """Regime of the last trend-timeframe candle that closed at or before t_close_ms."""
    if not r4 or not r4["tc"]:
        return NEUTRAL
    j = bisect.bisect_right(r4["tc"], t_close_ms) - 1
    if j < 0:
        return NEUTRAL
    return r4["regime"][j]


def evaluate(ind, i, regime, p):
    """Entry signal on the CLOSED signal-timeframe candle i. Returns a dict with side or a reason code."""
    c = ind["c"][i]
    res = {
        "side": None, "code": None, "regime": regime, "close": c, "i": i,
        "atr": ind["atr"][i], "adx": ind["adx"][i], "rsi": ind["rsi"][i],
        "ema20": ind["ema20"][i], "dch": ind["dch"][i], "dcl": ind["dcl"][i],
        "strength": 0.0,
    }
    needed = ("atr", "adx", "rsi", "ema20", "dch", "dcl")
    if any(res[k] is None for k in needed):
        res["code"] = "WARMUP"
        return res
    a = res["atr"]
    if a <= 0:
        res["code"] = "BAD_ATR"
        return res
    res["dist_long_pct"] = (res["dch"] - c) / c * 100.0
    res["dist_short_pct"] = (c - res["dcl"]) / c * 100.0
    if regime == NEUTRAL:
        res["code"] = "REGIME_NEUTRAL"
        return res
    h, l = ind["h"][i], ind["l"][i]
    candle_atr = (h - l) / a
    if (regime == LONG and not p.get("allow_long", True)) or (regime == SHORT and not p.get("allow_short", True)):
        res["code"] = "DIRECTION_OFF"
        return res
    if regime == LONG:
        if c <= res["dch"]:
            res["code"] = "NO_BREAKOUT"
        elif c <= res["ema20"]:
            res["code"] = "BELOW_EMA"
        elif res["adx"] < float(p["adx_min"]):
            res["code"] = "WEAK_TREND"
        elif res["rsi"] > float(p["rsi_max_long"]):
            res["code"] = "OVERHEATED"
        elif (c - res["ema20"]) / a > float(p["anti_chase_atr"]):
            res["code"] = "CHASE"
        elif candle_atr > float(p["max_candle_atr"]):
            res["code"] = "SPIKE"
        else:
            res["side"] = LONG
            res["code"] = "BREAKOUT_LONG"
    else:
        if c >= res["dcl"]:
            res["code"] = "NO_BREAKOUT"
        elif c >= res["ema20"]:
            res["code"] = "BELOW_EMA"
        elif res["adx"] < float(p["adx_min"]):
            res["code"] = "WEAK_TREND"
        elif res["rsi"] < float(p["rsi_min_short"]):
            res["code"] = "OVERHEATED"
        elif (res["ema20"] - c) / a > float(p["anti_chase_atr"]):
            res["code"] = "CHASE"
        elif candle_atr > float(p["max_candle_atr"]):
            res["code"] = "SPIKE"
        else:
            res["side"] = SHORT
            res["code"] = "BREAKOUT_SHORT"
    res["strength"] = res["adx"]
    return res


def initial_stop(side, entry, atr_value, p):
    """Stop = stop_atr_mult x ATR away, clamped to [min_stop_pct, max_stop_pct] of entry."""
    dist = float(p["stop_atr_mult"]) * atr_value
    pct = dist / entry * 100.0
    pct = min(max(pct, float(p["min_stop_pct"])), float(p["max_stop_pct"]))
    dist = entry * pct / 100.0
    stop = entry - dist if side == LONG else entry + dist
    return stop, dist


def mfe_r(side, entry, extreme, r_dist):
    """Maximum favourable excursion measured in R."""
    if r_dist <= 0 or extreme is None:
        return 0.0
    move = (extreme - entry) if side == LONG else (entry - extreme)
    return move / r_dist


def trail_stop(side, entry, r_dist, cur_stop, extreme, atr_value, p):
    """Candidate stop. Never looser than cur_stop.

    1) Chandelier: extreme -/+ trail_atr_mult x ATR
    2) Breakeven lock: once MFE >= breakeven_at_r, stop >= entry +/- breakeven_lock_r x R
    """
    k = float(p["trail_atr_mult"])
    be_at = float(p["breakeven_at_r"])
    be_lock = float(p["breakeven_lock_r"])
    m = mfe_r(side, entry, extreme, r_dist)
    if side == LONG:
        cand = cur_stop
        if atr_value:
            cand = max(cand, extreme - k * atr_value)
        if m >= be_at:
            cand = max(cand, entry + be_lock * r_dist)
        return cand
    cand = cur_stop
    if atr_value:
        cand = min(cand, extreme + k * atr_value)
    if m >= be_at:
        cand = min(cand, entry - be_lock * r_dist)
    return cand


prepare_signal = None  # set below (readable aliases)


def time_stop_due(opened_ms, now_ms, side, entry, extreme, r_dist, p):
    limit = float(p.get("time_stop_hours") or 0)
    if limit <= 0:  # 0 = time stop disabled
        return False
    hours = (now_ms - opened_ms) / 3_600_000.0
    if hours < limit:
        return False
    return mfe_r(side, entry, extreme, r_dist) < float(p["time_stop_min_r"])


prepare_signal = prepare_1h   # signal timeframe indicators (name kept for compatibility)
prepare_trend = prepare_4h    # trend timeframe regime
