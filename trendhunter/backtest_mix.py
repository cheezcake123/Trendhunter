#!/usr/bin/env python3
"""Backtest of the whole v2 system: spot momentum sleeve + futures breakout sleeve + monthly re-split.

python3 backtest_mix.py --data ~/trendhunter-data --days 730
Uses the same rule code as the live bot (spot_strategy.py, strategy.py/risk.py via backtest.py).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import backtest as BT  # noqa: E402
import spot_strategy as SS  # noqa: E402
from binance_api import BinanceError, BinanceFutures, NetworkError, parse_filters  # noqa: E402
from binance_spot import BinanceSpot  # noqa: E402

DAY = 86_400_000
KST = timezone(timedelta(hours=9))


def fetch_spot_daily(api, sym, start_ms, end_ms, cache_dir=None, pause=0.1):
    path = os.path.join(cache_dir, f"spot_{sym}_1d.json") if cache_dir else None
    cached = []
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                cached = json.load(fh)
        except Exception:
            cached = []
    rows = [r for r in cached if int(r[0]) >= start_ms]
    if not rows or int(rows[0][0]) > start_ms + DAY:
        rows, cursor = [], start_ms
    else:
        cursor = int(rows[-1][0]) + DAY
    while cursor < end_ms:
        batch = api.klines(sym, "1d", limit=1000, start_time=cursor, end_time=end_ms)
        if not batch:
            break
        rows.extend(batch)
        cursor = int(batch[-1][0]) + DAY
        if len(batch) < 1000:
            break
        if pause:
            time.sleep(pause)
    uniq = {int(r[0]): r for r in rows}
    closed = [uniq[k] for k in sorted(uniq) if int(uniq[k][6]) < end_ms]
    if path:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(closed, fh)
        os.replace(tmp, path)
    return closed


def to_daily(curve):
    """[(ms, equity)] -> {utc_day_start_ms: last equity of that day}"""
    out = {}
    for t, e in curve:
        out[(int(t) - 1) // DAY * DAY] = e
    return out


def mix(spot, fut, share):
    """Monthly re-split to `share` in spot. Inputs: {day_ms: equity}. Output: [(day_ms, equity)] starting at 100."""
    days = sorted(set(spot) & set(fut))
    if not days:
        return []
    out, vs, vf = [], 100.0 * share, 100.0 * (1 - share)
    prev_s, prev_f, prev_m = spot[days[0]], fut[days[0]], None
    for d in days:
        if prev_s > 0:
            vs *= spot[d] / prev_s
        if prev_f > 0:
            vf *= fut[d] / prev_f
        prev_s, prev_f = spot[d], fut[d]
        m = SS.month_key(d)
        if prev_m is not None and m != prev_m:
            tot = vs + vf
            vs, vf = tot * share, tot * (1 - share)
        prev_m = m
        out.append((d, vs + vf))
    return out


def stats(points):
    if len(points) < 2:
        return {"ret": 0.0, "mdd": 0.0, "cagr": None}
    e0 = points[0][1]
    peak, mdd = e0, 0.0
    for _, e in points:
        peak = max(peak, e)
        if peak > 0:
            mdd = min(mdd, (e - peak) / peak)
    final = points[-1][1]
    yrs = (points[-1][0] - points[0][0]) / DAY / 365.25
    cagr = ((final / e0) ** (1 / yrs) - 1) * 100 if yrs >= 0.99 and final > 0 and e0 > 0 else None
    return {"ret": (final / e0 - 1) * 100, "mdd": mdd * 100, "cagr": cagr}


def run(cfg, days, fut_api=None, spot_api=None, cache_dir=None, log=None, now_ms=None):
    fut_api = fut_api or BinanceFutures()
    spot_api = spot_api or BinanceSpot()
    now_ms = now_ms or int(fut_api.clock() * 1000)
    try:
        filters = parse_filters(fut_api.exchange_info(), cfg["symbols"])
    except (BinanceError, NetworkError):
        filters = None
    data, start = BT.load_data(fut_api, cfg, days, cache_dir, now_ms, log)
    if not data:
        raise RuntimeError("선물 데이터를 받지 못했습니다")
    pre = BT.precompute(cfg, data)
    fut_res = BT.simulate(cfg, pre, start, None, 100.0, filters)
    series = {}
    warm = int(cfg.get("spot_kline_days", 420))
    for sym in cfg.get("spot_candidates") or []:
        try:
            rows = fetch_spot_daily(spot_api, sym, start - warm * DAY, now_ms, cache_dir)
        except (BinanceError, NetworkError) as e:
            if log:
                log.warning("spot klines %s: %s", sym, e)
            continue
        s = SS.daily_series(rows, now_ms)
        if s["c"]:
            series[sym] = s
    if not series:
        raise RuntimeError("현물 데이터를 받지 못했습니다")
    spot_curve = SS.simulate(series, cfg, start - DAY, fee=float(cfg.get("spot_fee_rate", 0.001)),
                             slip=float(cfg.get("spot_slippage_bps", 5)) / 1e4, start_equity=10_000.0)
    spot_d = {d: e / 100.0 for d, e in to_daily(spot_curve).items()}
    fut_d = to_daily(fut_res["curve"])
    share = float(cfg.get("alloc_spot_pct", 50)) / 100.0
    mixes = {s: mix(spot_d, fut_d, s) for s in sorted({0.0, 0.3, 0.5, 0.7, 1.0, round(share, 2)})}
    return {"start": start, "end": now_ms, "fut": fut_res, "spot_d": spot_d, "fut_d": fut_d, "mixes": mixes,
            "share": round(share, 2), "n_spot": len(series)}


def format_report(cfg, days, out):
    def line(st):
        c = f" | 연환산 {st['cagr']:+.0f}%" if st["cagr"] is not None else ""
        return f"{st['ret']:+.1f}% | 최대낙폭 {st['mdd']:.1f}%{c}"

    d0 = datetime.fromtimestamp(out["start"] / 1000, KST).strftime("%Y-%m-%d")
    d1 = datetime.fromtimestamp(out["end"] / 1000, KST).strftime("%Y-%m-%d")
    share = out["share"]
    comb = stats(out["mixes"][share])
    sp = stats(out["mixes"][1.0])
    fu = stats(out["mixes"][0.0])
    m = BT.metrics(out["fut"])
    pf = "∞" if m["pf"] == float("inf") else f"{m['pf']:.2f}"
    final = out["mixes"][share][-1][1] if out["mixes"][share] else 100.0
    fund = BT.FUNDING_KO.get(out["fut"].get("funding_mode"), "펀딩비")
    lines = [f"📊 백테스트 v2 — 최근 {days}일 ({d0}~{d1})",
             f"수수료·슬리피지·{fund} 반영, 시작 $100, 매월 1일 배분 재조정", "",
             f"[현재 설정: 현물 {share * 100:.0f}% + 선물 {100 - share * 100:.0f}%]",
             f"합계 ${final:,.2f} ({line(comb)})",
             f"· 현물 모멘텀({out['n_spot']}개 후보 중 거래대금 상위 {cfg.get('spot_universe_size', 6)}): {line(sp)}",
             f"· 선물 돌파(1회 위험 {cfg['risk_per_trade_pct']:g}%): {line(fu)}",
             f"  거래 {m['n']}회 · 승률 {m['win']:.0f}% · 평균 {m['avg_r']:+.2f}R · 수익팩터 {pf}", "",
             "[배분별 비교]"]
    for s, pts in sorted(out["mixes"].items(), reverse=True):
        tag = "현물 100%" if s == 1.0 else "선물 100%" if s == 0.0 else f"현물 {s * 100:.0f}% : 선물 {100 - s * 100:.0f}%"
        lines.append(f"{tag}: {line(stats(pts))}" + (" ← 현재" if s == share else ""))
    lines += ["", "※ 과거 데이터 결과이며 미래 수익을 보장하지 않습니다.",
              "기간이 짧을수록 결과가 운에 크게 좌우됩니다. 730일 이상을 권장합니다."]
    return "\n".join(lines)


def report(cfg, days, fut_api=None, spot_api=None, cache_dir=None, log=None, now_ms=None):
    out = run(cfg, days, fut_api, spot_api, cache_dir, log, now_ms)
    return format_report(cfg, days, out), out


def main():
    from store import Store, load_config
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.environ.get("TH_DATA_DIR", os.path.expanduser("~/trendhunter-data")))
    ap.add_argument("--days", type=int, default=730)
    a = ap.parse_args()
    store = Store(a.data)
    cfg = load_config(APP_DIR, store)
    text, _ = report(cfg, a.days, cache_dir=store.path("cache"))
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
