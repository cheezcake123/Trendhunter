"""Spot sleeve: execution (live / paper), daily rebalancing, valuation.

The spot wallet is run by the bot: USDT plus every coin in `spot_candidates` counts as sleeve
capital (other coins are ignored). Once a day, shortly after the 00:00 UTC (09:00 KST) daily close,
the sleeve recomputes its momentum/volatility targets (spot_strategy.py) and trades with MARKET
orders: sells first, then buys. No leverage, no stops needed — at worst it simply holds coins.
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


def _cents(x):
    return f"{math.floor(max(x, 0.0) * 100) / 100:.2f}"


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
        return {"usdt": 0.0, "assets": {}, "seq": 0}

    def _save(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.st, fh)
        os.replace(tmp, self.path)

    def reset(self, usdt=0.0):
        self.st = {"usdt": float(usdt), "assets": {}, "seq": 0}
        self._save()

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
                     ("diag", {}), ("net_in", None), ("fails", 0), ("retry_at", 0.0), ("last_trades", [])):
            self.state.setdefault(k, v)

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
        self.cache = {"equity": eq, "usdt": usdt["free"] + usdt["locked"], "usdt_free": usdt["free"],
                      "holdings": hold, "ts": now}
        self.cache_ts = now
        if self.state.get("net_in") is None:
            self.state["net_in"] = eq  # whatever was already in the wallet counts as the starting capital
        return self.cache

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
    def lines(self, detail=True):
        v = self.cache or {}
        eq = v.get("equity", 0.0)
        out = []
        tg = self.state.get("targets") or {}
        if not eq:
            out.append("현물 잔고 없음")
        else:
            cash_pct = v.get("usdt", 0.0) / eq * 100 if eq else 0
            out.append(f"현물 평가 ${eq:,.2f} | USDT {cash_pct:.0f}%")
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
