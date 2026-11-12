#!/usr/bin/env python3
"""전략 검증용 실험실: 저장소의 백테스트 코드(strategy.py / risk.py / backtest.py / spot_strategy.py)를
실제 바이낸스 캔들에 그대로 돌려 설정을 비교한다. 봇과 함께 배포되지 않는다(zip 에 들어가지 않음).

데이터: https://github.com/finom/static-klines 의 .klines-cache (바이낸스 현물 캔들, 10종목, 매일 갱신)
    git clone --depth 1 https://github.com/finom/static-klines
    python3 research/lab.py --klines static-klines/.klines-cache

한계: 선물 캔들 대신 현물 캔들을 쓰고(4시간봉 종가는 거의 같다), 펀딩비는 실제 기록 대신
--funding-bps (8시간당, 기본 3 = 0.03%) 고정값을 쓴다. 이 가정에서 v2.3 README 의 실제 펀딩비 결과
(선물 연 +87% / 낙폭 -46%)와 거의 같은 값(+84% / -45%)이 나온다. 서버의 /backtest 는 실제 펀딩비를 쓴다.
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trendhunter")
sys.path.insert(0, APP)
import backtest as BT  # noqa: E402
import backtest_mix as MIX  # noqa: E402
import spot_strategy as SS  # noqa: E402
import strategy as S  # noqa: E402

DAY = 86_400_000
_rows, _pre = {}, {}


def ms(y, m=1, d=1):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


def rows(root, sym, tf):
    """All cached windows of one symbol/interval, oldest first (native Binance kline rows)."""
    if (sym, tf) not in _rows:
        out, d = {}, os.path.join(root, sym, tf)
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(d, fn), encoding="utf-8") as fh:
                        for r in json.load(fh):
                            if isinstance(r, list) and len(r) >= 8:
                                out[int(r[0])] = r
                except (OSError, ValueError):
                    continue
        _rows[(sym, tf)] = [out[k] for k in sorted(out)]
    return _rows[(sym, tf)]


def run_futures(root, cfg, start_ms, end_ms, funding_bps, equity=10_000.0):
    key = json.dumps({k: v for k, v in cfg.items() if k != "risk_per_trade_pct"}, sort_keys=True, default=str)
    if key not in _pre:
        data = {}
        for sym in cfg["symbols"]:
            c1 = S.candles_from_klines(rows(root, sym, cfg["signal_tf"]), end_ms)
            c4 = S.candles_from_klines(rows(root, sym, cfg["trend_tf"]), end_ms)
            if len(c1["c"]) > 300 and len(c4["c"]) > max(cfg["ma_periods"]) + 10:
                data[sym] = {"c1": c1, "c4": c4, "funding": None}
        _pre[key] = BT.precompute(cfg, data)
    return BT.simulate(cfg, _pre[key], start_ms, None, equity, None, funding_bps_8h=funding_bps)


def run_spot(root, cfg, start_ms, end_ms):
    series = {s: SS.daily_series(rows(root, s, "1d"), end_ms) for s in cfg["spot_candidates"]}
    return SS.simulate(series, cfg, start_ms - DAY, fee=float(cfg.get("spot_fee_rate", 0.001)),
                       slip=float(cfg.get("spot_slippage_bps", 5)) / 1e4, start_equity=10_000.0)


def stats(curve, a=None, b=None):
    pts = [(t, e) for t, e in curve if (a is None or t >= a) and (b is None or t <= b)]
    if len(pts) < 2:
        return None
    peak, mdd, daily = pts[0][1], 0.0, {}
    for t, e in pts:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
        daily[(t - 1) // DAY] = e
    ds = sorted(daily)
    r = [daily[y] / daily[x] - 1 for x, y in zip(ds, ds[1:]) if daily[x] > 0]
    mu = sum(r) / len(r)
    sd = math.sqrt(sum((x - mu) ** 2 for x in r) / max(len(r) - 1, 1))
    yrs = (pts[-1][0] - pts[0][0]) / DAY / 365.25
    return {"cagr": ((pts[-1][1] / pts[0][1]) ** (1 / yrs) - 1) * 100, "mdd": mdd * 100,
            "sharpe": mu / sd * math.sqrt(365) if sd else 0.0, "ret": (pts[-1][1] / pts[0][1] - 1) * 100}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--klines", required=True, help="static-klines 의 .klines-cache 폴더")
    ap.add_argument("--funding-bps", type=float, default=3.0, help="8시간당 펀딩비 가정(bp)")
    ap.add_argument("--start", type=int, default=2020)
    a = ap.parse_args()
    with open(os.path.join(APP, "config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    start, end = ms(a.start), max(int(r[0]) for r in rows(a.klines, "BTCUSDT", "1d")) + DAY
    periods = [("전체", start, None), ("2020-23", ms(2020), ms(2024)), ("2024-", ms(2024), None), ("2025-", ms(2025), None)]

    def line(name, curve):
        cells = []
        for _, x, y in periods:
            st = stats(curve, x, y)
            cells.append(f"{st['cagr']:+5.0f}%/{st['mdd']:4.0f}% S{st['sharpe']:.2f}" if st else "      n/a      ")
        print(f"{name:30s} " + " | ".join(cells))

    print(f"{'(연수익/최대낙폭 샤프)':27s} " + " | ".join(f"{p[0]:^17s}" for p in periods))
    fut = {}
    for name, over in (("선물 위험 2%", {"risk_per_trade_pct": 2.0, "pyramid_max_adds": 0}),
                       ("선물 위험 3%", {"risk_per_trade_pct": 3.0, "pyramid_max_adds": 0}),
                       ("선물 위험 4%", {"risk_per_trade_pct": 4.0, "pyramid_max_adds": 0}),
                       ("선물 위험 2% + 불타기 2", {"risk_per_trade_pct": 2.0, "pyramid_max_adds": 2}),
                       ("선물 위험 2.5% + 불타기 2", {"risk_per_trade_pct": 2.5, "pyramid_max_adds": 2}),
                       ("선물 위험 3% + 불타기 2", {"risk_per_trade_pct": 3.0, "pyramid_max_adds": 2})):
        fut[name] = run_futures(a.klines, dict(cfg, **over), start, end, a.funding_bps)
        line(name, fut[name]["curve"])
    spot = run_spot(a.klines, cfg, start, end)
    line("현물", spot)
    sd = MIX.to_daily(spot)
    print("--- 위험 단계(현물 % / 선물 %)")
    for name, sp, key in (("안정 70/30", 0.7, "선물 위험 2% + 불타기 2"), ("기본 50/50", 0.5, "선물 위험 2.5% + 불타기 2"),
                          ("공격 40/60", 0.4, "선물 위험 3% + 불타기 2"), ("v2.3 기본 50/50", 0.5, "선물 위험 3%")):
        line(name, [(d + DAY, e) for d, e in MIX.mix(sd, MIX.to_daily(fut[key]["curve"]), sp)])
    print("--- 해마다(수익/낙폭): 선물 위험 3% | 선물 위험 2% + 불타기 2 | 현물")
    for y in range(a.start, datetime.fromtimestamp(end / 1000, timezone.utc).year + 1):
        cells = []
        for c in (fut["선물 위험 3%"]["curve"], fut["선물 위험 2% + 불타기 2"]["curve"], spot):
            st = stats(c, ms(y), ms(y + 1))
            cells.append(f"{st['ret']:+6.0f}%/{st['mdd']:4.0f}%" if st else "    n/a    ")
        print(y, " | ".join(cells))


if __name__ == "__main__":
    main()
