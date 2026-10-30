#!/usr/bin/env python3
"""Account / API checks. Read-only by default; `--live` runs a tiny real order round trip.

python3 selftest.py --data ~/trendhunter-data [--live] [--fix]
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from binance_api import BinanceError, BinanceFutures, NetworkError, parse_filters  # noqa: E402
from binance_spot import BinanceSpot, parse_spot_filters  # noqa: E402
from risk import ceil_step, floor_step, fmt_step, round_step  # noqa: E402


def public_ip():
    try:
        with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=6) as r:
            return r.read().decode().strip()
    except Exception:
        return None


def _explain(e):
    if isinstance(e, BinanceError):
        if e.status == 451:
            return "이 서버 위치(국가)에서 바이낸스 API가 막혀 있습니다. 서울 등 비(非)미국 리전 서버를 쓰세요."
        if e.code == -2015:
            return "API 키·IP 제한·선물 권한 중 하나가 맞지 않습니다(키 재확인, 서버 IP 등록, Enable Futures 체크)."
        if e.code in (-1022, -2014):
            return "API 키 또는 Secret 값이 잘못 입력됐습니다."
        if e.code == -1021:
            return "서버 시간이 어긋났습니다(시간 동기화 필요)."
    return str(e)


def run_checks(api, cfg, symbols, fix=False):
    res = []

    def add(name, ok, detail, **kw):
        d = {"name": name, "ok": ok, "detail": detail}
        d.update(kw)
        res.append(d)

    try:
        off = api.sync_time()
        add("서버 시간", abs(off) < 1000, f"바이낸스와 차이 {off:+.0f}ms")
    except (BinanceError, NetworkError) as e:
        add("바이낸스 접속", False, _explain(e))
        return res
    try:
        filters = parse_filters(api.exchange_info())
        bad = [s for s in symbols if s not in filters or filters[s]["status"] != "TRADING"
               or filters[s].get("contract") not in (None, "PERPETUAL")]
        add("감시 종목", not bad, "모두 거래 가능" if not bad else f"문제 종목: {', '.join(bad)}", blocking=False)
    except (BinanceError, NetworkError) as e:
        add("거래소 정보", False, _explain(e))
        return res
    try:
        acc = api.account()
        eq = float(acc.get("totalMarginBalance") or 0)
        avail = float(acc.get("availableBalance") or 0)
        add("API 키·선물 권한", True, "읽기 성공")
        add("USDⓈ-M 선물 잔고", eq >= 10, f"총 {eq:.2f} USDT / 가용 {avail:.2f} USDT"
            + ("" if eq >= 10 else " — 현물 지갑에서 USDⓈ-M 선물 지갑으로 이체하세요"), equity=eq)
    except (BinanceError, NetworkError) as e:
        add("API 키·선물 권한", False, _explain(e))
        ip = public_ip()
        if ip:
            add("이 서버의 공인 IP", None, f"{ip} — 바이낸스 API 키의 '신뢰할 수 있는 IP'에 이 값을 넣으세요", blocking=False)
        return res
    try:
        has_pos = any(float(r.get("positionAmt") or 0) != 0 for r in api.position_risk())
        dual = api.get_position_mode()
        if dual and fix and not has_pos:
            api.set_position_mode(False)
            dual = False
        add("포지션 모드", not dual, "단방향(One-way)" if not dual else
            ("헤지 모드 — 실전 전환 때 단방향으로 자동 변경" if not has_pos else "헤지 모드 — 포지션을 닫고 단방향으로 바꿔야 합니다"),
            fixable=not has_pos)
        multi = api.get_multi_assets()
        if multi and fix and not has_pos:
            api.set_multi_assets(False)
            multi = False
        add("자산 모드", not multi, "단일자산(격리 가능)" if not multi else
            ("멀티에셋 모드 — 실전 전환 때 단일자산으로 자동 변경" if not has_pos else "멀티에셋 모드 — 포지션을 닫고 단일자산으로 바꿔야 합니다"),
            fixable=not has_pos)
    except (BinanceError, NetworkError) as e:
        add("계정 모드", False, _explain(e))
    ip = public_ip()
    if ip:
        add("이 서버의 공인 IP", None, ip, blocking=False)
    return res


def run_spot_checks(sapi, cfg):
    """Spot sleeve readiness: key permissions (spot trading, universal transfer, NO withdrawals) + spot wallet."""
    res = []

    def add(name, ok, detail, **kw):
        d = {"name": name, "ok": ok, "detail": detail, "spot": True}
        d.update(kw)
        res.append(d)

    try:
        sapi.sync_time()
    except (BinanceError, NetworkError) as e:
        add("현물 API 접속", False, _explain(e))
        return res
    try:
        r = sapi.api_restrictions()
        trade = bool(r.get("enableSpotAndMarginTrading"))
        move = bool(r.get("permitsUniversalTransfer"))
        wd = bool(r.get("enableWithdrawals"))
        ipr = bool(r.get("ipRestrict"))
        add("현물 거래 권한", trade, "켜짐" if trade else
            "꺼짐 — 바이낸스 API 관리 → 이 키 '제한 편집' → 'Enable Spot & Margin Trading' 체크")
        add("현물↔선물 범용 이체 권한", move, "켜짐 (봇이 두 지갑 비율을 자동으로 맞춤)" if move else
            "꺼짐 — 'Permits Universal Transfer' 체크 (출금이 아니라 내 지갑끼리 이동만 가능한 권한)")
        add("출금 권한", not wd, "꺼짐 (안전)" if not wd else "켜져 있음! 봇은 출금이 필요 없습니다. 즉시 끄세요")
        add("IP 제한", ipr, "서버 IP만 허용 (안전)" if ipr else "제한 없음 — 서버 IP만 허용하도록 설정하세요",
            blocking=False)
    except (BinanceError, NetworkError) as e:
        add("API 키 권한 조회", False, _explain(e))
    earn = 0.0
    if cfg.get("spot_earn"):
        # Simple Earn subscribe/redeem are TRADE endpoints: the key's spot trading permission covers them
        try:
            rows = sapi.earn_flexible_list("USDT", retries=0)
            prod = next((r for r in rows if r.get("canPurchase", True) and not r.get("isSoldOut")), None)
            earn = sum(float(r.get("totalAmount") or 0) for r in sapi.earn_flexible_position("USDT", retries=0))
            add("Simple Earn 유연 예치(USDT)", bool(prod),
                (f"상품 {prod['productId']} · 최소 {float(prod.get('minPurchaseAmount') or 0):g} USDT · "
                 f"연 {float(prod.get('latestAnnualPercentageRate') or 0) * 100:.2f}% · 현재 예치 {earn:.2f} USDT")
                if prod else "예치 가능한 USDT 유연 상품이 없습니다", blocking=False)
        except (BinanceError, NetworkError) as e:
            add("Simple Earn 유연 예치(USDT)", False, _explain(e) + " — 예치·환매에는 'Enable Spot & Margin Trading' "
                "권한이 필요합니다. 실패해도 매매는 계속되고 이자만 받지 못합니다.", blocking=False)
    else:
        add("Simple Earn 유연 예치(USDT)", None, "꺼짐 — 켜기: /set spot_earn true (필요 권한: 현물 거래)",
            blocking=False)
    try:
        bal = sapi.balances()
        usdt = sum(bal.get("USDT", {}).get(k, 0.0) for k in ("free", "locked"))
        bases = {sym[:-4]: sym for sym in cfg.get("spot_candidates") or [] if sym.endswith("USDT")}
        held = {a: b for a, b in bal.items() if a in bases}
        prices = sapi.prices([bases[a] for a in held]) if held else {}
        vals = {a: (b["free"] + b["locked"]) * prices.get(bases[a], 0.0) for a, b in held.items()}
        coins = sum(vals.values())
        listing = ", ".join(f"{a} ${v:,.2f}" for a, v in sorted(vals.items(), key=lambda x: -x[1]) if v >= 1.0)
        parked = f" + 이자 예치 {earn:.2f}" if earn > 0.01 else ""
        add("현물 지갑", None, f"USDT {usdt:.2f} + 코인 {coins:.2f}{parked} = {usdt + coins + earn:.2f} USDT"
            + (f" ({listing})" if listing else ""), blocking=False, spot_equity=usdt + coins + earn,
            spot_coins=coins, spot_listing=listing)
    except (BinanceError, NetworkError) as e:
        add("현물 지갑", False, _explain(e))
    return res


def format_report(res):
    icon = {True: "✅", False: "❌", None: "ℹ️"}
    lines = ["🩺 점검 결과"]
    for r in res:
        lines.append(f"{icon[r['ok']]} {r['name']}: {r['detail']}")
    return "\n".join(lines)


def live_roundtrip(api, cfg, filters, log, exclude=(), candidates=("DOGEUSDT", "XRPUSDT", "ADAUSDT", "TRXUSDT")):
    """Smallest possible real round trip: market buy -> algo stop -> cancel -> market sell."""
    steps = []
    state = {"sym": None, "qty": 0.0, "algo": None, "opened": False}

    def ok(msg):
        steps.append("✅ " + msg)

    def fail(msg):
        steps.append("❌ " + msg)

    t0 = int(api.clock() * 1000) - 2000
    try:
        if not filters:
            filters.update(parse_filters(api.exchange_info()))
        held = {r["symbol"] for r in api.position_risk() if float(r.get("positionAmt") or 0) != 0}
        sym = next((s for s in candidates
                    if s in filters and filters[s]["status"] == "TRADING" and s not in held and s not in exclude), None)
        if not sym:
            return {"ok": False, "text": "❌ 테스트용 종목을 찾지 못했습니다(이미 포지션 보유 중)."}
        f = filters[sym]
        state["sym"] = sym
        mark = api.mark_prices([sym]).get(sym)
        qty = ceil_step(max(float(f["min_qty"]), float(f["min_notional"]) * 1.15 / mark), f["step"])
        api.set_margin_type(sym, "ISOLATED")
        api.set_leverage(sym, 5)
        ok(f"{sym} 격리·5배 설정")
        r = api.new_order(symbol=sym, side="BUY", type="MARKET", quantity=fmt_step(qty, f["step"]),
                          newOrderRespType="RESULT", newClientOrderId=f"thtest{int(time.time())}")
        state.update(qty=qty, opened=True)
        ok(f"시장가 매수 {qty:g} @ {float(r.get('avgPrice') or mark):.5f}")
        trig = round_step(mark * 0.9, f["tick"])
        a = api.new_algo_order(symbol=sym, side="SELL", type="STOP_MARKET", triggerPrice=fmt_step(trig, f["tick"]),
                               quantity=fmt_step(qty, f["step"]), reduceOnly=True, workingType="MARK_PRICE",
                               clientAlgoId=f"thtestsl{int(time.time())}")
        state["algo"] = a.get("algoId")
        ok(f"손절(조건부) 주문 등록 algoId={state['algo']} 트리거 {trig}")
        api.sleep(1.0)
        listed = [o for o in api.open_algo_orders(sym) if str(o.get("algoId")) == str(state["algo"])]
        (ok if listed else fail)("미체결 조건부 주문 목록에서 확인" if listed else "조건부 주문이 목록에 없음")
        api.cancel_algo_order(algo_id=state["algo"])
        state["algo"] = None
        ok("조건부 주문 취소")
        api.new_order(symbol=sym, side="SELL", type="MARKET", quantity=fmt_step(qty, f["step"]), reduceOnly=True,
                      newOrderRespType="RESULT", newClientOrderId=f"thtestx{int(time.time())}")
        state["opened"] = False
        ok("시장가 청산")
        api.sleep(1.0)
        trades = api.user_trades(sym, start_time=t0)
        cost = sum(float(t.get("realizedPnl") or 0) - float(t.get("commission") or 0) for t in trades)
        ok(f"왕복 비용 {cost:+.4f} USDT")
        return {"ok": all(s.startswith("✅") for s in steps), "text": "🧪 실주문 왕복 테스트\n" + "\n".join(steps)
                + "\n\n주문·손절 경로가 정상입니다. /live 로 실전 전환할 수 있어요."}
    except Exception as e:
        fail(_explain(e))
        sym = state["sym"]
        if state["algo"]:
            try:
                api.cancel_algo_order(algo_id=state["algo"])
            except Exception:
                pass
        if state["opened"] and sym:
            try:
                f = filters[sym]
                api.new_order(symbol=sym, side="SELL", type="MARKET", quantity=fmt_step(state["qty"], f["step"]),
                              reduceOnly=True, newOrderRespType="RESULT")
                steps.append("↩️ 테스트 포지션 정리 완료")
            except Exception as e2:
                steps.append(f"🚨 테스트 포지션 정리 실패: {e2} — 앱에서 {sym} 포지션을 직접 닫아주세요")
        if log:
            log.error("live roundtrip failed: %s", e)
        return {"ok": False, "text": "🧪 실주문 왕복 테스트\n" + "\n".join(steps)}


def spot_roundtrip(sapi, fapi, cfg, log=None, candidates=("TRXUSDT", "DOGEUSDT", "XRPUSDT", "ADAUSDT")):
    """Tiny real spot round trip (+ wallet transfer test).

    Picks a coin the wallet does not hold, buys 6 USDT of it and sells back exactly what was bought
    (never coins that were already there). If 7 USDT had to come over from futures, it goes back at the
    end even when a step failed."""
    steps = []
    st = {"moved": 0.0}

    def ok(msg):
        steps.append("✅ " + msg)

    def fail(msg):
        steps.append("❌ " + msg)

    def done():
        return {"ok": bool(steps) and all(x.startswith("✅") or x.startswith("ℹ️") for x in steps),
                "text": "🧪 현물 실주문 테스트\n" + "\n".join(steps)}

    try:
        bal = sapi.balances()
        info = parse_spot_filters(sapi.exchange_info(list(candidates)))
        tradable = [x for x in candidates if x in info and info[x]["status"] == "TRADING"]
        held = {a for a, b in bal.items() if (b.get("free") or 0) > 0 or (b.get("locked") or 0) > 0}
        fresh = [x for x in tradable if info[x]["base"] not in held]
        sym = (fresh or tradable or [None])[0]
        if not sym:
            fail("테스트용 현물 종목을 찾지 못했습니다")
            return done()
        f = info[sym]
        if bal.get("USDT", {}).get("free", 0.0) < 7.0:
            acc = fapi.account()
            if float(acc.get("availableBalance") or 0) < 8.0:
                fail("현물·선물 지갑 모두 테스트 금액(7 USDT)이 부족합니다")
                return done()
            sapi.transfer("UMFUTURE_MAIN", "USDT", "7.00")
            st["moved"] = 7.0
            ok("선물 → 현물 7 USDT 이체 (범용 이체 권한 정상)")
            sapi.sleep(1.0)
        r = sapi.new_order(symbol=sym, side="BUY", type="MARKET", quoteOrderQty="6.00", newOrderRespType="FULL",
                           newClientOrderId=f"thstest{int(time.time())}")
        got = float(r.get("executedQty") or 0)
        got -= sum(float(x.get("commission") or 0) for x in r.get("fills") or [] if x.get("commissionAsset") == f["base"])
        ok(f"현물 시장가 매수 {sym} {float(r.get('executedQty') or 0):g} (6 USDT)")
        sapi.sleep(1.0)
        free = sapi.balances().get(f["base"], {}).get("free", 0.0)
        q = floor_step(min(got, free), f["step"])
        px_ = float(r.get("cummulativeQuoteQty") or 0) / max(float(r.get("executedQty") or 0), 1e-12)
        if q * px_ < float(f["min_notional"]):
            steps.append(f"ℹ️ 판매 가능한 양이 최소 주문 금액보다 작아 {f['base']} {q:g}개를 그대로 둡니다")
        else:
            r2 = sapi.new_order(symbol=sym, side="SELL", type="MARKET", quantity=fmt_step(q, f["step"]),
                                newOrderRespType="FULL", newClientOrderId=f"thstestx{int(time.time())}")
            ok(f"현물 시장가 매도 {float(r2.get('executedQty') or 0):g} → {float(r2.get('cummulativeQuoteQty') or 0):.4f} USDT")
    except Exception as e:  # noqa: BLE001
        fail(_explain(e))
        if log:
            log.error("spot roundtrip failed: %s", e)
    finally:
        if st["moved"]:
            try:
                sapi.sleep(1.0)
                back = min(st["moved"], sapi.balances().get("USDT", {}).get("free", 0.0))
                back = math.floor(back * 100) / 100
                if back >= 1.0:
                    sapi.transfer("MAIN_UMFUTURE", "USDT", f"{back:.2f}")
                    ok(f"현물 → 선물 {back:.2f} USDT 되돌림")
            except Exception as e:  # noqa: BLE001
                fail(f"선물 지갑으로 되돌리기 실패: {_explain(e)} — 다음 배분 때 봇이 맞춥니다")
    out = done()
    if out["ok"]:
        out["text"] += "\n(1개 미만 자투리 코인이 남을 수 있습니다 — 정상)"
    return out


def main():
    from store import Store, load_config
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.environ.get("TH_DATA_DIR", os.path.expanduser("~/trendhunter-data")))
    ap.add_argument("--live", action="store_true", help="tiny real order round trip")
    ap.add_argument("--fix", action="store_true", help="switch hedge/multi-asset mode off when safe")
    a = ap.parse_args()
    store = Store(a.data)
    cfg = load_config(APP_DIR, store)
    sec = store.load_secrets()
    api = BinanceFutures(sec.get("binance_key", ""), sec.get("binance_secret", ""),
                         base_url=os.environ.get("TH_BINANCE_BASE", "https://fapi.binance.com"))
    res = run_checks(api, cfg, cfg["symbols"], fix=a.fix)
    sapi = None
    if cfg.get("spot_enabled", True):
        sapi = BinanceSpot(sec.get("binance_key", ""), sec.get("binance_secret", ""),
                           base_url=os.environ.get("TH_SPOT_BASE", "https://api.binance.com"))
        res += run_spot_checks(sapi, cfg)
    print(format_report(res))
    if a.live:
        out = live_roundtrip(api, cfg, {}, None)
        print(out["text"])
        if sapi is not None:
            out2 = spot_roundtrip(sapi, api, cfg)
            print(out2["text"])
            return 0 if out["ok"] and out2["ok"] else 1
        return 0 if out["ok"] else 1
    return 0 if all(r["ok"] is not False for r in res) else 1


if __name__ == "__main__":
    sys.exit(main())
