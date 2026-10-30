"""Spot sleeve: execution (live / paper), daily rebalancing, valuation.

The spot wallet is run by the bot: USDT plus every coin in `spot_candidates` counts as sleeve
capital (other coins are ignored). Once a day, shortly after the 00:00 UTC (09:00 KST) daily close,
the sleeve recomputes its momentum/volatility targets (spot_strategy.py) and trades with MARKET
orders: sells first, then buys. No leverage, no stops needed — at worst it simply holds coins.

With spot_earn on, idle USDT waits in Binance Simple Earn (flexible USDT) instead of the spot wallet:
redeemed just before the daily trades/transfers need it, parked again afterwards (2 USDT stay in the
wallet). Parked USDT is sleeve money and is always part of the valuation. Earn trouble never stops
trading: it is reported and retried the next day.
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone

import spot_strategy as SS
from binance_api import BinanceError, NetworkError
from binance_spot import parse_spot_filters
from risk import floor_step, fmt_step

USDT = "USDT"
EARN_BUFFER = 2.0     # USDT always left in the spot wallet when parking
EARN_GAP_S = 3.5      # Binance: subscribe/redeem at most once per 3 s per account
EARN_POLLS = 4        # balance checks while a redemption lands in the spot wallet
PAPER_EARN_ID = "PAPER-USDT"


def _cents(x):
    return f"{math.floor(max(x, 0.0) * 100) / 100:.2f}"


def _cents_up(x):
    return f"{math.ceil(max(x, 0.0) * 100 - 1e-9) / 100:.2f}"


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- execution back-ends

class LiveSpot:
    name = "live"

    def __init__(self, api, filters, cfg, log):
        self.api = api
        self.filters = filters
        self.cfg = cfg
        self.log = log

    def balances(self):
        return self.api.balances()

    def prices(self, syms):
        return self.api.prices(list(syms))

    def _cid(self, tag):
        return f"ths{tag}{int(self.api.clock() * 1000)}"[:36]

    def order(self, sym, side, qty=None, quote=None, tag="R"):
        f = self.filters[sym]
        cid = self._cid(tag)
        params = {"symbol": sym, "side": side, "type": "MARKET", "newClientOrderId": cid, "newOrderRespType": "FULL"}
        if quote is not None:
            params["quoteOrderQty"] = _cents(quote)
        else:
            params["quantity"] = fmt_step(floor_step(qty, f["step"]), f["step"])
        try:
            r = self.api.new_order(**params)
        except (NetworkError, BinanceError) as e:
            if isinstance(e, BinanceError) and (e.status or 0) < 500:
                raise
            # outcome unknown: look it up by our client id for ~20 s. Never send it again blindly — a late
            # original plus a re-send would double the trade. Tomorrow's run re-plans from real balances.
            r = None
            t_end = time.monotonic() + 25  # bounded: the main loop also runs the futures stops
            for _ in range(8):
                self.api.sleep(2.5)
                try:
                    r = self.api.get_order(sym, client_id=cid, retries=0)
                    break
                except BinanceError as e2:
                    if e2.code != -2013:
                        raise
                except NetworkError:
                    pass
                if time.monotonic() > t_end:
                    break
            if r is None:
                raise NetworkError(f"{sym} {side} 주문 결과를 확인하지 못했습니다(다음 리밸런싱에서 잔고 기준으로 다시 계산)")
        for _ in range(4):
            if r.get("status") in ("FILLED", "EXPIRED", "CANCELED", "REJECTED", "EXPIRED_IN_MATCH"):
                break
            self.api.sleep(0.5)
            r = self.api.get_order(sym, order_id=r.get("orderId"))
        qty_f = float(r.get("executedQty") or 0)
        quote_f = float(r.get("cummulativeQuoteQty") or 0)
        fee = {}
        for fl in r.get("fills") or []:
            a = fl.get("commissionAsset") or USDT
            fee[a] = fee.get(a, 0.0) + float(fl.get("commission") or 0)
        return {"qty": qty_f, "quote": quote_f, "avg": quote_f / qty_f if qty_f else 0.0, "status": r.get("status"),
                "order_id": r.get("orderId"), "fee": fee, "client_id": cid}

    def transfer(self, to_futures, amount):
        r = self.api.transfer("MAIN_UMFUTURE" if to_futures else "UMFUTURE_MAIN", USDT, _cents(amount))
        return r.get("tranId")

    # ------------------------------------------------------------ Simple Earn (flexible USDT)
    def earn_product(self):
        """The USDT flexible product open for subscription: {id, min, apr} or None."""
        for r in self.api.earn_flexible_list(USDT, retries=0):
            if r.get("asset", USDT) == USDT and r.get("canPurchase", True) and not r.get("isSoldOut") \
                    and r.get("status", "PURCHASING") == "PURCHASING":
                return {"id": str(r["productId"]), "min": float(r.get("minPurchaseAmount") or 0.0),
                        "apr": float(r.get("latestAnnualPercentageRate") or 0.0)}
        return None

    def earn_position(self):
        rows = [r for r in self.api.earn_flexible_position(USDT, retries=0) if r.get("asset", USDT) == USDT]
        return {"amount": sum(float(r.get("totalAmount") or 0) for r in rows),
                "rewards": sum(float(r.get("cumulativeTotalRewards") or 0) for r in rows),
                "apr": float(rows[0].get("latestAnnualPercentageRate") or 0) if rows else None,
                "product": str(rows[0]["productId"]) if rows else None}

    def earn_subscribe(self, product_id, amount):
        r = self.api.earn_subscribe(product_id, _cents(amount))
        if r.get("success") is False:
            raise BinanceError(200, None, f"예치가 거절됐습니다: {r}", "/sapi/v1/simple-earn/flexible/subscribe")
        return r.get("purchaseId")

    def earn_redeem(self, product_id, amount=None, redeem_all=False):
        r = self.api.earn_redeem(product_id, amount=None if redeem_all else _cents_up(amount), redeem_all=redeem_all)
        if r.get("success") is False:
            raise BinanceError(200, None, f"환매가 거절됐습니다: {r}", "/sapi/v1/simple-earn/flexible/redeem")
        return r.get("redeemId")


class PaperSpot:
    """Simulated spot wallet that fills at live spot prices (+slippage, 0.1% fee)."""
    name = "paper"

    def __init__(self, api, filters, cfg, path, log, clock=time.time):
        self.api = api
        self.filters = filters
        self.cfg = cfg
        self.path = path
        self.log = log
        self.clock = clock
        self.st = self._load()

    def _load(self):
        if self.path and os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                pass
        return {"usdt": 0.0, "assets": {}, "seq": 0, "earn": 0.0, "earn_rewards": 0.0, "earn_ts": None}

    def _save(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.st, fh)
        os.replace(tmp, self.path)

    def reset(self, usdt=0.0):
        self.st = {"usdt": float(usdt), "assets": {}, "seq": 0, "earn": 0.0, "earn_rewards": 0.0, "earn_ts": None}
        self._save()

    # ------------------------------------------------------------ Simple Earn, simulated (never calls Binance)
    def _earn_accrue(self):
        """Interest at spot_cash_yield per year on the virtual Earn balance, up to now."""
        now = self.clock()
        amt = float(self.st.get("earn") or 0.0)
        last = self.st.get("earn_ts")
        if amt > 0 and last and now > last:
            gain = amt * float(self.cfg.get("spot_cash_yield", 0.03)) * (now - last) / (365 * 86400.0)
            self.st["earn"] = amt + gain
            self.st["earn_rewards"] = float(self.st.get("earn_rewards") or 0.0) + gain
        self.st["earn_ts"] = now

    def earn_product(self):
        return {"id": PAPER_EARN_ID, "min": 0.1, "apr": float(self.cfg.get("spot_cash_yield", 0.03))}

    def earn_position(self):
        self._earn_accrue()
        self._save()
        amt = float(self.st.get("earn") or 0.0)
        return {"amount": amt, "rewards": float(self.st.get("earn_rewards") or 0.0),
                "apr": float(self.cfg.get("spot_cash_yield", 0.03)), "product": PAPER_EARN_ID if amt > 0 else None}

    def earn_subscribe(self, product_id, amount):
        self._earn_accrue()
        amount = min(float(amount), float(self.st["usdt"]))
        self.st["usdt"] = float(self.st["usdt"]) - amount
        self.st["earn"] = float(self.st.get("earn") or 0.0) + amount
        self.st["seq"] += 1
        self._save()
        return self.st["seq"]

    def earn_redeem(self, product_id, amount=None, redeem_all=False):
        self._earn_accrue()
        held = float(self.st.get("earn") or 0.0)
        amount = held if redeem_all else min(float(amount), held)
        self.st["earn"] = held - amount
        self.st["usdt"] = float(self.st["usdt"]) + amount
        self.st["seq"] += 1
        self._save()
        return self.st["seq"]

    def adjust_usdt(self, delta):
        self.st["usdt"] = float(self.st["usdt"]) + float(delta)
        self._save()

    def balances(self):
        out = {USDT: {"free": float(self.st["usdt"]), "locked": 0.0}}
        for a, q in self.st["assets"].items():
            if q > 0:
                out[a] = {"free": float(q), "locked": 0.0}
        return out

    def prices(self, syms):
        return self.api.prices(list(syms))

    def order(self, sym, side, qty=None, quote=None, tag="R"):
        f = self.filters[sym]
        base = f["base"]
        px = self.api.prices([sym])[sym]
        slip = float(self.cfg.get("spot_slippage_bps", 5)) / 1e4
        fee_rate = float(self.cfg.get("spot_fee_rate", 0.001))
        self.st["seq"] += 1
        if side == "BUY":
            fill = px * (1 + slip)
            spend = min(float(quote), float(self.st["usdt"]))
            q = floor_step(spend / fill, f["step"])
            cost = q * fill
            fee_q = q * fee_rate
            self.st["usdt"] -= cost
            self.st["assets"][base] = self.st["assets"].get(base, 0.0) + q - fee_q
            res = {"qty": q, "quote": cost, "avg": fill, "status": "FILLED", "order_id": self.st["seq"],
                   "fee": {base: fee_q}}
        else:
            fill = px * (1 - slip)
            q = min(floor_step(qty, f["step"]), self.st["assets"].get(base, 0.0))
            proceeds = q * fill
            fee_u = proceeds * fee_rate
            self.st["assets"][base] = self.st["assets"].get(base, 0.0) - q
            if self.st["assets"][base] <= 1e-12:
                self.st["assets"].pop(base, None)
            self.st["usdt"] += proceeds - fee_u
            res = {"qty": q, "quote": proceeds, "avg": fill, "status": "FILLED", "order_id": self.st["seq"],
                   "fee": {USDT: fee_u}}
        self._save()
        return res


# ---------------------------------------------------------------- manager

class SpotManager:
    def __init__(self, cfg, store, api, public_api, log, send, state, clock=time.time, sleep=time.sleep):
        self.cfg = cfg
        self.store = store
        self.api = api                # signed spot client (keys)
        self.public = public_api      # unsigned client for prices/klines (paper mode uses it too)
        self.log = log
        self.send = send
        self.state = state            # dict persisted by the trader (state["spot"])
        self.clock = clock
        self.sleep = sleep
        self.filters = {}
        self.ex = None
        self.cache = None
        self.cache_ts = 0.0
        for k, v in (("last_day", None), ("universe", []), ("universe_month", None), ("targets", {}),
                     ("diag", {}), ("net_in", None), ("fails", 0), ("retry_at", 0.0), ("last_trades", []),
                     ("earn", {})):
            self.state.setdefault(k, v)
        self._earn_last = -1e9  # monotonic time of the last Earn subscribe/redeem

    # ------------------------------------------------------------ setup
    def candidates(self):
        return list(self.cfg.get("spot_candidates") or [])

    def load_filters(self):
        try:
            info = self.public.exchange_info(self.candidates())
        except BinanceError as e:
            if e.code != -1121:  # one candidate was delisted -> fall back to the full list
                raise
            info = self.public.exchange_info()
            self.send("⚠️ 현물 후보 중 바이낸스에 없는 종목이 있어 제외했습니다. /settings 의 spot_candidates 를 확인하세요.")
        self.filters.clear()
        self.filters.update(parse_spot_filters(info, set(self.candidates())))
        return self.filters

    def make_ex(self, mode):
        if mode == "live":
            self.ex = LiveSpot(self.api, self.filters, self.cfg, self.log)
        else:
            self.ex = PaperSpot(self.public, self.filters, self.cfg, self.store.path("paper_spot.json"), self.log,
                                self.clock)
        self.cache = None
        self.state["earn"]["ts"] = 0.0  # read the Earn balance of this back-end afresh
        return self.ex

    def tradable(self):
        return [s for s in self.candidates() if s in self.filters and self.filters[s]["status"] == "TRADING"]

    def known(self):
        """Every candidate Binance still lists, whatever its status (a paused coin still counts as money)."""
        return [s for s in self.candidates() if s in self.filters]

    # ------------------------------------------------------------ valuation
    def value(self, refresh=False, max_age=600):
        now = self.clock()
        if self.cache and not refresh and now - self.cache_ts < max_age:
            return self.cache
        bal = self.ex.balances()
        syms = [s for s in self.known() if self.filters[s]["base"] in bal]
        prices = self.ex.prices(syms) if syms else {}
        usdt = bal.get(USDT, {"free": 0.0, "locked": 0.0})
        hold = {}
        eq = usdt["free"] + usdt["locked"]
        for s in syms:
            b = bal[self.filters[s]["base"]]
            q = b["free"] + b["locked"]
            p = prices.get(s, 0.0)
            hold[s] = {"qty": q, "free": b["free"], "px": p, "val": q * p}
            eq += q * p
        earn = self.earn_value()  # parked USDT is sleeve money: leaving it out would skew the spot/futures split
        eq += earn
        self.cache = {"equity": eq, "usdt": usdt["free"] + usdt["locked"], "usdt_free": usdt["free"],
                      "earn": earn, "holdings": hold, "ts": now}
        self.cache_ts = now
        if self.state.get("net_in") is None:
            self.state["net_in"] = eq  # whatever was already in the wallet counts as the starting capital
        return self.cache

    # ------------------------------------------------------------ Simple Earn (idle USDT)
    def earn_on(self):
        return bool(self.cfg.get("spot_earn", False))

    def earn_touched(self):
        """Earn is used only when switched on, or while USDT parked earlier is still there."""
        return self.earn_on() or float(self.state["earn"].get("held") or 0.0) > 0

    def earn_ok(self, now):
        return self.ex is not None and self.earn_touched() and self.state["earn"].get("fail_day") != utc_day(now)

    def earn_value(self, refresh=False, max_age=3600):
        """USDT parked in flexible Earn. Never raises: on a failed lookup the last known amount stands.
        Read at most hourly (IP weight 150): between reads only the bot's own moves (booked locally) and the
        interest change it."""
        E = self.state["earn"]
        if self.ex is None or not self.earn_touched():
            return 0.0
        now = self.clock()
        if refresh or now - float(E.get("ts") or 0.0) >= max_age:
            E["ts"] = now
            try:
                pos = self.ex.earn_position()
                E["held"], E["rewards"] = round(pos["amount"], 8), pos["rewards"]
                if pos.get("apr") is not None:
                    E["apr"] = pos["apr"]
                if pos.get("product"):
                    E["product"] = pos["product"]
            except (BinanceError, NetworkError, KeyError, TypeError, ValueError) as e:
                self.log.warning("earn position: %s", e)
        return float(E.get("held") or 0.0)

    def earn_product(self, now):
        """USDT flexible product {id, min}, looked up at most once a day. Raises on API trouble."""
        E = self.state["earn"]
        if E.get("product_day") == utc_day(now) and E.get("pid"):
            return {"id": E["pid"], "min": float(E.get("min") or 0.0)}
        p = self.ex.earn_product()
        if p:
            E.update(pid=p["id"], min=p["min"], product_day=utc_day(now))
            if p.get("apr"):
                E["apr"] = p["apr"]
        return p

    def _earn_pace(self):
        wait = self._earn_last + EARN_GAP_S - time.monotonic()
        if wait > 0:
            self.sleep(wait)
        self._earn_last = time.monotonic()

    def _earn_book(self, now, kind, amount, ref, why):
        self.store.append("earn.jsonl", {"ts": now, "mode": self.ex.name, "kind": kind, "asset": USDT,
                                         "amount": round(amount, 6), "id": ref, "why": why})
        self.cache = None

    def _earn_failed(self, now, what, e):
        E = self.state["earn"]
        E["fail_day"] = utc_day(now)  # no more Earn calls today
        E["last_error"] = f"{what}: {e}"[:200]
        self.log.warning("earn %s failed: %s", what, e)
        self.send(f"⚠️ 이자 예치 {what} 실패: {e}\n매매·이체·선물 손절 관리는 그대로 진행합니다. 내일 다시 시도합니다.")

    def _earn_redeem(self, now, amount, held, why):
        pid = self.state["earn"].get("product") or (self.earn_product(now) or {}).get("id")
        if not pid:
            raise RuntimeError("USDT 유연 예치 상품을 찾지 못했습니다")
        whole = amount >= held - 0.01
        self._earn_pace()
        ref = self.ex.earn_redeem(pid, amount=None if whole else amount, redeem_all=whole)
        amount = held if whole else amount
        self.state["earn"]["held"] = max(held - amount, 0.0)
        self._earn_book(now, "redeem", amount, ref, why)
        return amount

    def earn_fund(self, now, need, why="리밸런싱·이체 자금"):
        """Before the daily trades/transfers: redeem enough parked USDT to cover `need` (bounded wait until it
        lands). Never raises; returns the amount redeemed (0 when nothing was or could be)."""
        if need <= 0.01 or not self.earn_ok(now):
            return 0.0
        try:
            held = self.earn_value(refresh=True)
            if held <= 0.01:
                return 0.0
            before = self.value(refresh=True)["usdt_free"]
            got = self._earn_redeem(now, min(held, need), held, why)
        except Exception as e:  # noqa: BLE001 — Earn must never stop trading or transfers
            self._earn_failed(now, "환매", e)
            return 0.0
        for _ in range(EARN_POLLS):  # usually instant; never hold the main loop for long
            try:
                if self.value(refresh=True)["usdt_free"] >= before + got * 0.99:
                    break
            except (BinanceError, NetworkError):
                pass
            self.sleep(1.5)
        return got

    def earn_park(self, now):
        """After the daily trades/transfers: park spot USDT above the 2 USDT buffer (or, with spot_earn off,
        bring back what is still parked). Never raises; returns the USDT moved into Earn (negative = out)."""
        if not self.earn_ok(now):
            return 0.0
        if not self.earn_on():
            try:
                held = self.earn_value(refresh=True)
                if held <= 0.01:
                    self.state["earn"]["held"] = 0.0
                    return 0.0
                return -self._earn_redeem(now, held, held, "이자 예치 끔")
            except Exception as e:  # noqa: BLE001
                self._earn_failed(now, "환매", e)
                return 0.0
        try:
            free = self.value(refresh=True)["usdt_free"]
            amt = math.floor((free - EARN_BUFFER) * 100) / 100
            if amt <= 0:
                return 0.0
            prod = self.earn_product(now)
            if not prod:
                raise RuntimeError("예치할 수 있는 USDT 유연 상품이 없습니다")
            if amt < prod["min"]:
                return 0.0
            self._earn_pace()
            ref = self.ex.earn_subscribe(prod["id"], amt)
        except Exception as e:  # noqa: BLE001
            self._earn_failed(now, "예치", e)
            return 0.0
        E = self.state["earn"]
        E["held"] = float(E.get("held") or 0.0) + amt
        E["product"] = prod["id"]
        self._earn_book(now, "subscribe", amt, ref, "남는 USDT 예치")
        return amt

    def earn_line(self):
        E = self.state.get("earn") or {}
        held = float(E.get("held") or 0.0)
        if not self.earn_on():
            if held > 0.01:
                return f"이자 예치 꺼짐 — 남은 예치 ${held:,.2f}는 다음 리밸런싱 때 현물 지갑으로 돌려받습니다"
            return "이자 예치 꺼짐 (켜기: /set spot_earn true)"
        line = f"이자 예치(유연 USDT) ${held:,.2f}"
        if E.get("apr"):
            line += f" · 연 {float(E['apr']) * 100:.2f}%"
        line += f" · 누적 이자 ${float(E.get('rewards') or 0.0):,.4f}"
        if E.get("fail_day") == utc_day(self.clock()):
            line += f"\n⚠️ 오늘 예치/환매 실패({E.get('last_error', '')[:80]}) — 내일 다시 시도"
        return line

    # ------------------------------------------------------------ schedule
    def due(self, now):
        if not self.cfg.get("spot_enabled", True):
            return False
        if now < float(self.state.get("retry_at") or 0):
            return False
        secs = now % 86400
        return self.state.get("last_day") != utc_day(now) and secs >= int(self.cfg.get("spot_rebalance_delay_s", 300))

    def fetch_series(self, now, budget_s=120):
        now_ms = int(now * 1000)
        out = {}
        t0 = time.time()
        for sym in self.tradable():
            if time.time() - t0 > budget_s:  # do not hold up the main loop (futures stops) on a slow network
                raise RuntimeError("현물 일봉 데이터 받기가 너무 느립니다 — 10분 뒤 다시 시도")
            rows = self.public.klines(sym, "1d", limit=int(self.cfg.get("spot_kline_days", 420)))
            s = SS.daily_series(rows, now_ms)
            if s["c"]:
                out[sym] = s
        return out

    def compute_targets(self, now, series):
        month = SS.month_key(now * 1000)
        if self.state.get("universe_month") != month or not self.state.get("universe"):
            uni = SS.select_universe(series, self.tradable(), int(self.cfg.get("spot_universe_size", 6)),
                                     int(self.cfg.get("spot_universe_lookback_days", 365)),
                                     int(self.cfg.get("spot_universe_min_days", 365)))
            if uni:
                old = self.state.get("universe") or []
                self.state["universe"], self.state["universe_month"] = uni, month
                if old and set(old) != set(uni):
                    self.send("🔁 현물 운용 종목 교체(월간 거래대금 순위): "
                              + ", ".join(s[:-4] for s in uni)
                              + f"\n빠짐: {', '.join(s[:-4] for s in old if s not in uni) or '-'} / "
                              f"새로: {', '.join(s[:-4] for s in uni if s not in old) or '-'}")
        w, diag = SS.target_weights(series, self.state["universe"], self.cfg)
        # for the screens: BTC's signal even while the gate is switched off (None = not enough BTC history)
        g = SS.btc_gate(series, dict(self.cfg, spot_btc_gate=True))
        self.state["btc_gate"] = {"ts": now, "info": g,
                                  "n": len(self.cfg.get("spot_lookbacks") or [20, 60, 120, 250])}
        return w, diag

    # ------------------------------------------------------------ the daily job
    def rebalance(self, now, reserve=0.0, series=None):
        """One rebalance. reserve = USDT to leave free (pending transfer to futures). Returns summary dict."""
        series = series if series is not None else self.fetch_series(now)
        if not series:
            raise RuntimeError("현물 일봉 데이터를 받지 못했습니다")
        w, diag = self.compute_targets(now, series)
        v = self.value(refresh=True)
        E = max(v["equity"] - max(reserve, 0.0), 0.0)
        band = float(self.cfg.get("spot_band_pct", 5)) / 100.0
        live = set(self.tradable())
        mn = {s: float(self.filters[s]["min_notional"]) * 1.05 for s in live}
        vals = {s: h["val"] for s, h in v["holdings"].items() if s in live}  # a paused coin is valued, not traded
        w = {s: x for s, x in w.items() if s in live}
        plan = SS.plan_trades(vals, w, E, band, mn)
        if self.earn_touched():  # buys + the pending transfer to futures, minus wallet cash and sell proceeds
            buys = sum(d for _, d, _ in plan if d > 0)
            sells = -sum(d for _, d, _ in plan if d < 0)
            need = buys + max(reserve, 0.0) - v["usdt_free"] - sells * 0.99
            if need > 0:
                self.earn_fund(now, need + 1.0)
        done, errors = [], []
        for sym, d, full in plan:
            f = self.filters[sym]
            try:
                if d < 0:
                    h = v["holdings"].get(sym)
                    if not h or h["px"] <= 0:
                        continue
                    q = h["free"] if full else min(h["free"], -d / h["px"])
                    q = floor_step(q, f["step"])
                    if q < float(f["min_qty"]) or q * h["px"] < float(f["min_notional"]):
                        continue
                    r = self.ex.order(sym, "SELL", qty=q, tag="S")
                else:
                    cash = self.value(refresh=True)["usdt_free"] - max(reserve, 0.0)
                    spend = min(d, cash * 0.998)
                    if spend < float(f["min_notional"]) * 1.02:
                        continue
                    px_ = (v["holdings"].get(sym) or {}).get("px") or self.ex.prices([sym]).get(sym)
                    if not px_:
                        continue
                    est = floor_step(spend / px_, f["step"])
                    if est < float(f["min_qty"]) or est * px_ < float(f["min_notional"]) * 1.01:
                        continue  # the exchange's lot size makes this amount untradeable
                    r = self.ex.order(sym, "BUY", quote=spend, tag="B")
            except (BinanceError, NetworkError) as e:
                errors.append(f"{sym[:-4]}: {e}")
                self.log.warning("spot order %s: %s", sym, e)
                continue
            if r["qty"] <= 0:
                continue
            side = "SELL" if d < 0 else "BUY"
            rec = {"ts": now, "mode": self.ex.name, "sym": sym, "side": side, "qty": r["qty"], "quote": round(r["quote"], 6),
                   "avg": r["avg"], "fee": r["fee"], "target_w": round(w.get(sym, 0.0), 4), "order_id": r.get("order_id")}
            self.store.append("spot_trades.jsonl", rec)
            done.append(rec)
            self.cache = None
        self.state.update(targets={s: round(x, 4) for s, x in w.items()},
                          diag={s: {"frac": d_["frac"], "vol": round(d_["vol"], 3) if d_["vol"] else None}
                                for s, d_ in diag.items()},
                          last_day=utc_day(now), fails=0, retry_at=0.0, last_run=now,
                          last_trades=[{"sym": x["sym"], "side": x["side"], "quote": x["quote"]} for x in done])
        return {"trades": done, "errors": errors, "targets": w, "diag": diag, "equity": E}

    # ------------------------------------------------------------ text
    def gate_line(self):
        """'BTC 신호 n/4 · 알트 허용/차단' from the last rebalance."""
        if not self.cfg.get("spot_btc_gate", True):
            return "BTC 게이트 꺼짐 · 알트 허용"
        st = self.state.get("btc_gate")
        if not st:
            return "BTC 신호: 다음 리밸런싱(09:05 KST) 때 계산"
        g = st.get("info")
        if not g:
            return "BTC 신호 데이터 부족 · 알트 허용(게이트 미적용)"
        n = int(st.get("n") or 4)
        return f"BTC 신호 {int(round(g['frac'] * n))}/{n} · 알트 {'허용' if g['open'] else '차단'}"

    def lines(self, detail=True):
        v = self.cache or {}
        eq = v.get("equity", 0.0)
        out = []
        tg = self.state.get("targets") or {}
        if not eq:
            out.append("현물 잔고 없음")
        else:
            earn = v.get("earn", 0.0)
            cash_pct = (v.get("usdt", 0.0) + earn) / eq * 100 if eq else 0
            parked = f" (이자 예치 {earn / eq * 100:.0f}%)" if earn > 0.01 else ""
            out.append(f"현물 평가 ${eq:,.2f} | USDT {cash_pct:.0f}%{parked}")
            if detail:
                hold = v.get("holdings", {})
                for s in sorted(set(hold) | set(tg), key=lambda x: -(hold.get(x, {}).get("val", 0.0))):
                    h = hold.get(s, {})
                    cur = h.get("val", 0.0) / eq * 100 if eq else 0
                    d = (self.state.get("diag") or {}).get(s, {})
                    sig = f"신호 {int(round(d.get('frac', 0) * 4))}/4" if d else ""
                    vol = f"변동성 {d['vol'] * 100:.0f}%" if d.get("vol") else ""
                    if cur < 0.5 and tg.get(s, 0) < 0.005:
                        continue
                    out.append(f"  {s[:-4]} {cur:.0f}% (목표 {tg.get(s, 0) * 100:.0f}%) {sig} {vol}".rstrip())
        return out
