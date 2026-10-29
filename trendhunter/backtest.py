#!/usr/bin/env python3
"""Backtester that reuses the live rules (strategy.py + risk.py).

python3 backtest.py --data ~/trendhunter-data --days 365 [--risks 1,2,3,5] [--csv trades.csv]
Downloads Binance USD-M futures klines (public endpoint, no key needed) and caches them.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import risk as R  # noqa: E402
import strategy as S  # noqa: E402
from binance_api import BinanceError, BinanceFutures, NetworkError, parse_filters  # noqa: E402

TF_MS = {"15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
         "6h": 21_600_000, "12h": 43_200_000, "1d": 86_400_000}
KST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------- data

def fetch_klines(api, sym, tf, start_ms, end_ms, cache_dir=None, pause=0.15):
    tfm = TF_MS[tf]
    path = None
    cached = []
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, f"{sym}_{tf}.json")
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    cached = json.load(fh)
            except Exception:
                cached = []
    rows = [r for r in cached if int(r[0]) >= start_ms]
    if not rows or int(rows[0][0]) > start_ms + tfm:
        rows, cursor = [], start_ms
    else:
        cursor = int(rows[-1][0]) + tfm
    while cursor < end_ms:
        batch = api.klines(sym, tf, limit=1500, start_time=cursor, end_time=end_ms)
        if not batch:
            break
        rows.extend(batch)
        cursor = int(batch[-1][0]) + tfm
        if len(batch) < 1500:
            break
        if pause:
            time.sleep(pause)
    uniq = {int(r[0]): r for r in rows}
    closed = [uniq[k] for k in sorted(uniq) if int(uniq[k][6]) < end_ms]
    if path:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(closed, fh)
        os.replace(tmp, path)
    return closed


def load_data(api, cfg, days, cache_dir=None, now_ms=None, log=None):
    now_ms = now_ms or int(time.time() * 1000)
    start = now_ms - days * 86_400_000
    data = {}
    for sym in cfg["symbols"]:
        try:
            r1 = fetch_klines(api, sym, cfg["signal_tf"], start - 300 * TF_MS[cfg["signal_tf"]], now_ms, cache_dir)
            r4 = fetch_klines(api, sym, cfg["trend_tf"], start - 600 * TF_MS[cfg["trend_tf"]], now_ms, cache_dir)
        except (BinanceError, NetworkError) as e:
            if log:
                log.warning("backtest data %s: %s", sym, e)
            continue
        c1 = S.candles_from_klines(r1, now_ms)
        c4 = S.candles_from_klines(r4, now_ms)
        if len(c1["c"]) > 300 and len(c4["c"]) > max(cfg["ma_periods"]) + 10:
            data[sym] = {"c1": c1, "c4": c4}
    return data, start


def precompute(cfg, data):
    pre = {}
    for sym, d in data.items():
        ind = S.prepare_1h(d["c1"], cfg)
        r4 = S.prepare_4h(d["c4"], cfg)
        sigs = [S.evaluate(ind, i, S.regime_at(r4, ind["tc"][i]), cfg) for i in range(len(ind["c"]))]
        pre[sym] = {"ind": ind, "sigs": sigs, "idx": {t: i for i, t in enumerate(ind["t"])}}
    return pre


# ---------------------------------------------------------------- simulation

def simulate(cfg, pre, start_ms, risk_pct=None, start_equity=100.0, filters=None, fee_rate=None,
             slip_bps=None, stop_slip_bps=5.0, funding_bps_8h=1.0):
    """Bar-by-bar portfolio simulation on closed candles.

    Entries fill at the signal candle's close (+slippage); stops are checked against the next
    candles' high/low (gap -> fill at the open); trailing stops move at each close.
    Funding is charged to both sides (conservative)."""
    p = dict(cfg)
    if risk_pct is not None:
        p["risk_per_trade_pct"] = float(risk_pct)
    fee = float(fee_rate if fee_rate is not None else p.get("fee_rate", 0.0005))
    slip = float(slip_bps if slip_bps is not None else p.get("slippage_bps", 2)) / 1e4
    sslip = float(stop_slip_bps) / 1e4
    tf_ms = TF_MS[p["signal_tf"]]
    fund_bar = float(funding_bps_8h) / 1e4 * tf_ms / (8 * 3_600_000)
    times = sorted({t for s in pre.values() for t in s["ind"]["t"] if t >= start_ms})
    cash = float(start_equity)
    L = R.new_ledger(cash, start_ms / 1000)
    pos, trades, curve, skips, last_close = {}, [], [], {}, {}
    peak, max_dd = cash, 0.0

    def close(sym, exit_px, reason, t_close):
        nonlocal cash
        q = pos.pop(sym)
        d = 1 if q["side"] == S.LONG else -1
        gross = (exit_px - q["entry"]) * q["qty"] * d
        xfee = q["qty"] * exit_px * fee
        cash += gross - xfee
        net = gross - xfee - q["fee_in"] - q["funding"]
        R.on_exit(L, sym, net, t_close / 1000, p)
        trades.append({"sym": sym, "side": q["side"], "t_in": q["opened_ms"], "t_out": t_close,
                       "entry": q["entry"], "exit": exit_px, "qty": q["qty"], "pnl": net,
                       "r": net / q["risk_amt"] if q["risk_amt"] else 0.0, "reason": reason, "lev": q["lev"]})

    def mtm():
        u = 0.0
        for sym, q in pos.items():
            c = last_close.get(sym, q["entry"])
            u += (c - q["entry"]) * q["qty"] * (1 if q["side"] == S.LONG else -1)
        return cash + u

    for t in times:
        t_close = t + tf_ms
        now_s = t_close / 1000
        for sym in list(pos):
            s = pre[sym]
            i = s["idx"].get(t)
            if i is None:
                continue
            ind = s["ind"]
            o, h, lo, c = ind["o"][i], ind["h"][i], ind["l"][i], ind["c"][i]
            q = pos[sym]
            locked = (q["side"] == S.LONG and q["stop"] > q["entry"]) or (q["side"] == S.SHORT and q["stop"] < q["entry"])
            if q["side"] == S.LONG and lo <= q["stop"]:
                close(sym, min(o, q["stop"]) * (1 - sslip), "TRAIL" if locked else "STOP", t_close)
                continue
            if q["side"] == S.SHORT and h >= q["stop"]:
                close(sym, max(o, q["stop"]) * (1 + sslip), "TRAIL" if locked else "STOP", t_close)
                continue
            fc = q["qty"] * c * fund_bar
            cash -= fc
            q["funding"] += fc
            q["extreme"] = max(q["extreme"], h) if q["side"] == S.LONG else min(q["extreme"], lo)
            a = ind["atr"][i] or q["atr"]
            mk = c * (1 - slip) if q["side"] == S.LONG else c * (1 + slip)
            if S.time_stop_due(q["opened_ms"], t_close, q["side"], q["entry"], q["extreme"], q["r_dist"], p):
                close(sym, mk, "TIME", t_close)
                continue
            sig = s["sigs"][i]
            if sig["side"] and sig["side"] != q["side"]:
                close(sym, mk, "REVERSE", t_close)
                continue
            q["stop"] = S.trail_stop(q["side"], q["entry"], q["r_dist"], q["stop"], q["extreme"], a, p)
        for sym, s in pre.items():
            i = s["idx"].get(t)
            if i is not None:
                last_close[sym] = s["ind"]["c"][i]
        eq = mtm()
        R.roll_day(L, now_s, eq)
        cands = []
        for sym, s in pre.items():
            i = s["idx"].get(t)
            if i is None or sym in pos:
                continue
            sig = s["sigs"][i]
            if sig["side"]:
                cands.append((sym, sig))
        cands.sort(key=lambda x: -x[1]["strength"])
        for sym, sig in cands:
            ok, code = R.can_enter(L, pos, sym, sig["side"], now_s, eq, p)
            if not ok:
                skips[code] = skips.get(code, 0) + 1
                continue
            side = sig["side"]
            entry = sig["close"] * (1 + slip if side == S.LONG else 1 - slip)
            stop, dist = S.initial_stop(side, entry, sig["atr"], p)
            used = sum(q["margin"] for q in pos.values())
            sz = R.size_position(eq, eq - used, entry, dist, p, (filters or {}).get(sym))
            if not sz["ok"]:
                skips[sz["code"]] = skips.get(sz["code"], 0) + 1
                continue
            fin = sz["qty"] * entry * fee
            cash -= fin
            pos[sym] = {"side": side, "entry": entry, "qty": sz["qty"], "stop": stop, "r_dist": dist,
                        "atr": sig["atr"], "extreme": entry, "opened_ms": t_close, "fee_in": fin, "funding": 0.0,
                        "margin": sz["margin"], "risk_amt": sz["qty"] * dist, "lev": sz["lev"]}
            R.on_entry(L, now_s)
            eq = mtm()
        eq = mtm()
        curve.append((t_close, eq))
        peak = max(peak, eq)
        if peak > 0:
            max_dd = min(max_dd, (eq - peak) / peak)
        if eq <= start_equity * 0.02:
            break
    for sym in list(pos):
        close(sym, last_close.get(sym, pos[sym]["entry"]), "END", curve[-1][0] if curve else start_ms)
    final = cash
    return {"trades": trades, "curve": curve, "final": final, "start": float(start_equity),
            "max_dd": max_dd * 100.0, "skips": skips, "risk_pct": p["risk_per_trade_pct"]}


def metrics(res):
    tr = res["trades"]
    n = len(tr)
    out = {"n": n, "final": res["final"], "ret": (res["final"] / res["start"] - 1) * 100, "max_dd": res["max_dd"]}
    if not n:
        out.update(win=0.0, avg_r=0.0, pf=0.0, streak=0, per_sym={}, monthly=[], reasons={}, long=(0, 0.0),
                   short=(0, 0.0), best=None, worst=None, avg_win_r=0.0, avg_loss_r=0.0)
        return out
    wins = [t for t in tr if t["pnl"] > 0]
    losses = [t for t in tr if t["pnl"] <= 0]
    gw = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in losses)
    streak = cur = 0
    for t in tr:
        cur = cur + 1 if t["pnl"] <= 0 else 0
        streak = max(streak, cur)
    per = {}
    for t in tr:
        a = per.setdefault(t["sym"], [0, 0.0])
        a[0] += 1
        a[1] += t["pnl"]
    reasons = {}
    for t in tr:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    monthly, prev_eq, prev_m = [], res["start"], None
    for ts, eq in res["curve"]:
        m = datetime.fromtimestamp(ts / 1000, KST).strftime("%y-%m")
        if prev_m is None:
            prev_m = m
        if m != prev_m:
            monthly.append((prev_m, (month_end / prev_eq - 1) * 100 if prev_eq > 0 else 0.0))
            prev_eq, prev_m = month_end, m
        month_end = eq
    if res["curve"]:
        monthly.append((prev_m, (res["curve"][-1][1] / prev_eq - 1) * 100 if prev_eq > 0 else 0.0))
    lo = [t for t in tr if t["side"] == S.LONG]
    sh = [t for t in tr if t["side"] == S.SHORT]
    out.update(
        win=len(wins) / n * 100, avg_r=sum(t["r"] for t in tr) / n, pf=(gw / gl) if gl > 0 else float("inf"),
        streak=streak, per_sym=per, monthly=monthly, reasons=reasons,
        long=(len(lo), sum(t["pnl"] for t in lo)), short=(len(sh), sum(t["pnl"] for t in sh)),
        best=max(tr, key=lambda t: t["pnl"]), worst=min(tr, key=lambda t: t["pnl"]),
        avg_win_r=(sum(t["r"] for t in wins) / len(wins)) if wins else 0.0,
        avg_loss_r=(sum(t["r"] for t in losses) / len(losses)) if losses else 0.0)
    return out


REASON_SHORT = {"STOP": "손절", "TRAIL": "추적", "TIME": "시간", "REVERSE": "반대", "END": "종료"}


def format_report(cfg, days, start_ms, end_ms, results, cur_risk, n_syms):
    def usd(x):
        return f"${x:,.2f}"

    m = metrics(results[cur_risk])
    d0 = datetime.fromtimestamp(start_ms / 1000, KST).strftime("%Y-%m-%d")
    d1 = datetime.fromtimestamp(end_ms / 1000, KST).strftime("%Y-%m-%d")
    pf = "∞" if m["pf"] == float("inf") else f"{m['pf']:.2f}"
    lines = [f"📊 백테스트 — 최근 {days}일 ({d0}~{d1}), {n_syms}종목",
             "수수료 0.05%·슬리피지·펀딩비(보수적) 반영, 시작 $100", "",
             f"[현재 설정: 1회 위험 {cur_risk:g}%]",
             f"최종 {usd(m['final'])} ({m['ret']:+.1f}%) | 최대낙폭 {m['max_dd']:.1f}%",
             f"거래 {m['n']}회 · 승률 {m['win']:.0f}% · 평균 {m['avg_r']:+.2f}R · 수익팩터 {pf}"]
    if m["n"]:
        lines.append(f"평균 수익 {m['avg_win_r']:+.2f}R / 평균 손실 {m['avg_loss_r']:+.2f}R | 최장 연패 {m['streak']}회")
        lines.append(f"롱 {m['long'][0]}회 {m['long'][1]:+.2f}$ / 숏 {m['short'][0]}회 {m['short'][1]:+.2f}$")
        lines.append("청산 사유: " + ", ".join(f"{REASON_SHORT.get(k, k)} {v}" for k, v in sorted(m["reasons"].items())))
        lines.append("종목: " + " · ".join(f"{s[:-4] if s.endswith('USDT') else s} {v[1]:+.1f}$({v[0]})"
                                          for s, v in sorted(m["per_sym"].items(), key=lambda x: -x[1][1])))
        lines.append("월별: " + " · ".join(f"{k} {v:+.0f}%" for k, v in m["monthly"][-12:]))
    lines += ["", "[1회 위험별 비교]"]
    for r in sorted(results):
        mm = metrics(results[r])
        flag = " ← 현재" if r == cur_risk else ""
        lines.append(f"{r:g}%: {usd(mm['final'])} ({mm['ret']:+.0f}%) | 낙폭 {mm['max_dd']:.0f}% | {mm['n']}회{flag}")
    lines += ["", "※ 과거 데이터 결과이며 미래 수익을 보장하지 않습니다.",
              "낙폭이 감당 못 할 수준이면 /risk 로 위험을 낮추세요."]
    return "\n".join(lines)


def report(cfg, days, risks=None, cache_dir=None, api=None, log=None, now_ms=None):
    api = api or BinanceFutures()
    now_ms = now_ms or int(api.clock() * 1000)
    try:
        filters = parse_filters(api.exchange_info(), cfg["symbols"])
    except (BinanceError, NetworkError):
        filters = None
    data, start = load_data(api, cfg, days, cache_dir, now_ms, log)
    if not data:
        return "⚠️ 백테스트 데이터를 받지 못했습니다(네트워크 또는 종목 확인).", {}
    pre = precompute(cfg, data)
    cur = float(cfg["risk_per_trade_pct"])
    levels = sorted(set([1.0, 2.0, 3.0, 5.0] + [cur] + [float(x) for x in (risks or [])]))
    results = {r: simulate(cfg, pre, start, r, 100.0, filters) for r in levels}
    return format_report(cfg, days, start, now_ms, results, cur, len(data)), results


def main():
    from store import Store, load_config
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.environ.get("TH_DATA_DIR", os.path.expanduser("~/trendhunter-data")))
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--risks", default="")
    ap.add_argument("--csv", default="")
    a = ap.parse_args()
    store = Store(a.data)
    cfg = load_config(APP_DIR, store)
    risks = [float(x) for x in a.risks.split(",") if x.strip()]
    api = BinanceFutures(base_url=os.environ.get("TH_BINANCE_BASE", "https://fapi.binance.com"))
    text, results = report(cfg, a.days, risks, store.path("cache"), api)
    print(text)
    if a.csv:
        cur = float(cfg["risk_per_trade_pct"])
        with open(a.csv, "w", encoding="utf-8") as fh:
            fh.write("sym,side,t_in,t_out,entry,exit,qty,pnl,r,reason,lev\n")
            for t in results[cur]["trades"]:
                fh.write(",".join(str(t[k]) for k in ("sym", "side", "t_in", "t_out", "entry", "exit", "qty",
                                                       "pnl", "r", "reason", "lev")) + "\n")
        print(f"거래 내역 저장: {a.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
