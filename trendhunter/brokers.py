"""Execution layer. LiveBroker talks to Binance; PaperBroker simulates fills on live mark prices.

Both expose the same methods so the trading logic does not care which one it drives.
"""
from __future__ import annotations

import json
import os
import time

from binance_api import BinanceError, NetworkError
from risk import fmt_step

WEEK_MS = 7 * 24 * 3600 * 1000 - 60_000


def _chunks(since_ms, until_ms):
    start = since_ms
    while start < until_ms:
        end = min(start + WEEK_MS, until_ms)
        yield start, end
        start = end + 1


class LiveBroker:
    name = "live"

    def __init__(self, api, filters, cfg, log):
        self.api = api
        self.filters = filters
        self.cfg = cfg
        self.log = log
        self.lev_cache = {}
        self.prepared = set()

    def tick(self, marks):
        return None

    # ------------------------------------------------------------ account
    def equity(self):
        a = self.api.account()
        eq = float(a.get("totalMarginBalance") or 0)
        avail = float(a.get("availableBalance") or 0)
        wallet = float(a.get("totalWalletBalance") or 0)
        upnl = float(a.get("totalUnrealizedProfit") or 0)
        if eq == 0 and wallet == 0:
            for row in a.get("assets", []):
                if row.get("asset") == "USDT":
                    eq = float(row.get("marginBalance") or 0)
                    avail = float(row.get("availableBalance") or 0)
                    wallet = float(row.get("walletBalance") or 0)
                    upnl = float(row.get("unrealizedProfit") or 0)
        return {"equity": eq, "available": avail, "wallet": wallet, "upnl": upnl}

    def positions(self):
        out = {}
        for r in self.api.position_risk():
            amt = float(r.get("positionAmt") or 0)
            if amt == 0:
                continue
            out[r["symbol"]] = {
                "amt": amt,
                "entry": float(r.get("entryPrice") or 0),
                "mark": float(r.get("markPrice") or 0),
                "upnl": float(r.get("unRealizedProfit", r.get("unrealizedProfit", 0)) or 0),
                "liq": float(r.get("liquidationPrice") or 0),
                "side_mode": r.get("positionSide", "BOTH"),
            }
        return out

    def prepare(self, sym):
        if sym in self.prepared:
            return
        try:
            self.api.set_margin_type(sym, self.cfg.get("margin_type", "ISOLATED"))
        except BinanceError as e:
            self.log.warning("margin type %s: %s", sym, e)
        self.prepared.add(sym)

    def set_leverage(self, sym, lev):
        lev = int(lev)
        if self.lev_cache.get(sym) != lev:
            self.api.set_leverage(sym, lev)
            self.lev_cache[sym] = lev

    # ------------------------------------------------------------ orders
    def _cid(self, tag):
        return f"th{tag}{int(self.api.clock() * 1000)}"[:36]

    def market(self, sym, side, qty, reduce_only=False, tag="E"):
        f = self.filters[sym]
        cid = self._cid(tag)
        params = {"symbol": sym, "side": side, "type": "MARKET", "quantity": fmt_step(qty, f["step"]),
                  "newClientOrderId": cid, "newOrderRespType": "RESULT"}
        if reduce_only:
            params["reduceOnly"] = True
        try:
            r = self.api.new_order(**params)
        except (NetworkError, BinanceError) as e:
            if isinstance(e, BinanceError) and (e.status or 0) < 500:
                raise
            # outcome unknown (timeout / 5xx): look the order up by our client id before doing anything else.
            # An exit (reduce-only) may be sent again — it can never overshoot the position. An entry is never
            # re-sent: a late original + a re-send would double the position; if it shows up later, reconcile
            # adopts it and puts a stop on it.
            r = None
            t_end = time.monotonic() + 15  # bounded: other positions' stops are managed by the same loop
            for attempt in range(6):
                self.api.sleep(1.0 if attempt == 0 else 2.0)
                if time.monotonic() > t_end:
                    break
                try:
                    r = self.api.get_order(sym, client_id=cid, retries=0)
                    break
                except BinanceError as e2:
                    if e2.code != -2013:
                        raise
                    if reduce_only and attempt >= 1:
                        params["newClientOrderId"] = self._cid(tag + "r")
                        r = self.api.new_order(**params)
                        break
                except NetworkError:
                    continue
            if r is None:
                raise NetworkError(f"{sym} 주문 결과를 확인하지 못했습니다 — 포지션이 생기면 봇이 찾아서 손절을 겁니다")
        for _ in range(6):
            avg = float(r.get("avgPrice") or 0)
            if r.get("status") == "FILLED" and avg > 0:
                break
            self.api.sleep(0.5)
            r = self.api.get_order(sym, order_id=r.get("orderId"), client_id=None if r.get("orderId") else cid)
        return {"avg": float(r.get("avgPrice") or 0), "qty": float(r.get("executedQty") or 0),
                "status": r.get("status"), "order_id": r.get("orderId"), "client_id": cid}

    def place_stop(self, sym, close_side, qty, trigger):
        f = self.filters[sym]
        r = self.api.new_algo_order(
            symbol=sym, side=close_side, type="STOP_MARKET",
            triggerPrice=fmt_step(trigger, f["tick"]), quantity=fmt_step(qty, f["step"]),
            reduceOnly=True, workingType=self.cfg.get("stop_working_type", "MARK_PRICE"),
            clientAlgoId=self._cid("sl"))
        algo_id = r.get("algoId")
        if not algo_id:
            raise BinanceError(200, r.get("code"), f"algoOrder without algoId: {r}", "/fapi/v1/algoOrder")
        return str(algo_id)

    def cancel_stop(self, sym, stop_id):
        try:
            self.api.cancel_algo_order(algo_id=int(stop_id))
            return True
        except BinanceError as e:
            self.log.warning("cancel stop %s %s: %s", sym, stop_id, e)
            return False

    def open_stops(self, sym):
        out = []
        for r in self.api.open_algo_orders(sym):
            if r.get("symbol") != sym:
                continue
            otype = r.get("orderType") or r.get("type")
            if otype not in ("STOP_MARKET", "STOP"):
                continue
            out.append({
                "id": str(r.get("algoId")),
                "trigger": float(r.get("triggerPrice") or 0),
                "side": r.get("side"),
                "qty": float(r.get("quantity") or 0),
                "reduce_only": str(r.get("reduceOnly")).lower() == "true",
                "close_position": str(r.get("closePosition")).lower() == "true",
                "client_id": r.get("clientAlgoId", ""),
            })
        return out

    # ------------------------------------------------------------ accounting
    def realized(self, sym, since_ms, side, min_order_id=None):
        now_ms = int(self.api.clock() * 1000) + 5000
        trades = []
        for a, b in _chunks(since_ms, now_ms):
            trades.extend(self.api.user_trades(sym, start_time=a, end_time=b))
        seen = set()
        uniq = []
        for t in trades:
            if t.get("id") in seen:
                continue
            seen.add(t.get("id"))
            if int(t.get("time", 0)) < since_ms:
                continue
            if min_order_id is not None and int(t.get("orderId") or 0) < int(min_order_id):
                continue  # belongs to an earlier position on the same symbol
            uniq.append(t)
        pnl = sum(float(t.get("realizedPnl") or 0) for t in uniq)
        fee = 0.0
        bnb = None
        for t in uniq:
            c = float(t.get("commission") or 0)
            asset = t.get("commissionAsset", "USDT")
            if asset in ("USDT", "USDC", "FDUSD"):
                fee += c
            elif asset == "BNB":
                if bnb is None:
                    try:
                        bnb = self.api.mark_prices(["BNBUSDT"]).get("BNBUSDT", 0.0)
                    except Exception:
                        bnb = 0.0
                fee += c * bnb
        close_side = "SELL" if side == "LONG" else "BUY"
        closing = [t for t in uniq if t.get("side") == close_side]
        cq = sum(float(t.get("qty") or 0) for t in closing)
        exit_price = (sum(float(t["price"]) * float(t["qty"]) for t in closing) / cq) if cq else 0.0
        funding = 0.0
        try:
            for a, b in _chunks(since_ms, now_ms):
                for row in self.api.income(symbol=sym, income_type="FUNDING_FEE", start_time=a, end_time=b):
                    funding += float(row.get("income") or 0)
        except BinanceError as e:
            self.log.warning("funding lookup %s: %s", sym, e)
        return {"pnl": pnl, "fee": fee, "funding": funding, "closed_qty": cq,
                "exit_price": exit_price, "n": len(uniq)}


class PaperBroker:
    """Simulated account that fills at live mark prices (+slippage, taker fee)."""
    name = "paper"

    def __init__(self, api, filters, cfg, path, log, clock=time.time):
        self.api = api
        self.filters = filters
        self.cfg = cfg
        self.path = path
        self.log = log
        self.clock = clock
        self.marks = {}
        self.st = self._load()

    def _load(self):
        if self.path and os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                pass
        return {"balance": float(self.cfg.get("paper_start_balance", 100)), "positions": {},
                "stops": {}, "trades": [], "seq": 0, "lev": {}}

    def _save(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        self.st["trades"] = self.st["trades"][-500:]
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.st, fh)
        os.replace(tmp, self.path)

    def reset(self, balance=None):
        self.st = {"balance": float(balance if balance is not None else self.cfg.get("paper_start_balance", 100)),
                   "positions": {}, "stops": {}, "trades": [], "seq": 0, "lev": {}}
        self._save()

    def adjust_balance(self, delta):
        """Paper wallet transfer in (+) / out (-)."""
        self.st["balance"] = float(self.st["balance"]) + float(delta)
        self._save()

    def _mark(self, sym):
        m = self.marks.get(sym)
        if m is None:
            self.marks.update(self.api.mark_prices([sym]))
            m = self.marks[sym]
        return m

    def tick(self, marks):
        self.marks.update(marks)
        changed = False
        for sid, s in list(self.st["stops"].items()):
            pos = self.st["positions"].get(s["sym"])
            if not pos:
                del self.st["stops"][sid]
                changed = True
                continue
            m = marks.get(s["sym"])
            if m is None:
                continue
            hit = (s["side"] == "SELL" and m <= s["trigger"]) or (s["side"] == "BUY" and m >= s["trigger"])
            if hit:
                slip = float(self.cfg.get("slippage_bps", 2)) / 1e4
                px = min(m, s["trigger"]) * (1 - slip) if s["side"] == "SELL" else max(m, s["trigger"]) * (1 + slip)
                self._fill(s["sym"], s["side"], min(s["qty"], abs(pos["amt"])), px, True)
                self.st["stops"].pop(sid, None)
                changed = True
        if changed:
            self._save()

    def equity(self):
        upnl = 0.0
        used = 0.0
        for sym, p in self.st["positions"].items():
            m = self.marks.get(sym, p["entry"])
            upnl += (m - p["entry"]) * p["amt"]
            used += abs(p["amt"]) * p["entry"] / max(1, int(self.st["lev"].get(sym, 1)))
        bal = self.st["balance"]
        return {"equity": bal + upnl, "available": bal - used, "wallet": bal, "upnl": upnl}

    def positions(self):
        out = {}
        for sym, p in self.st["positions"].items():
            m = self.marks.get(sym, p["entry"])
            out[sym] = {"amt": p["amt"], "entry": p["entry"], "mark": m,
                        "upnl": (m - p["entry"]) * p["amt"], "liq": 0.0, "side_mode": "BOTH"}
        return out

    def prepare(self, sym):
        return None

    def set_leverage(self, sym, lev):
        self.st["lev"][sym] = int(lev)
        self._save()

    def market(self, sym, side, qty, reduce_only=False, tag="E"):
        m = self._mark(sym)
        slip = float(self.cfg.get("slippage_bps", 2)) / 1e4
        px = m * (1 + slip) if side == "BUY" else m * (1 - slip)
        return self._fill(sym, side, qty, px, reduce_only)

    def _fill(self, sym, side, qty, price, reduce_only):
        fee_rate = float(self.cfg.get("fee_rate", 0.0005))
        pos = self.st["positions"].get(sym, {"amt": 0.0, "entry": 0.0})
        amt = pos["amt"]
        signed = qty if side == "BUY" else -qty
        realized = 0.0
        if amt == 0 or (amt > 0) == (signed > 0):
            if reduce_only:
                return {"avg": 0.0, "qty": 0.0, "status": "EXPIRED", "order_id": None}
            new_amt = amt + signed
            entry = (abs(amt) * pos["entry"] + qty * price) / abs(new_amt)
            filled = qty
        else:
            close_qty = min(qty, abs(amt))
            realized = (price - pos["entry"]) * close_qty * (1 if amt > 0 else -1)
            new_amt = amt + (close_qty if signed > 0 else -close_qty)
            entry = pos["entry"]
            filled = close_qty
            rest = qty - close_qty
            if rest > 1e-12 and not reduce_only:
                new_amt = rest if signed > 0 else -rest
                entry = price
                filled = qty
        fee = filled * price * fee_rate
        self.st["balance"] += realized - fee
        self.st["seq"] += 1
        self.st["trades"].append({
            "id": self.st["seq"], "orderId": self.st["seq"], "symbol": sym, "side": side, "qty": filled, "price": price,
            "realizedPnl": realized, "commission": fee, "commissionAsset": "USDT",
            "time": int(self.clock() * 1000)})
        if abs(new_amt) < 1e-12:
            self.st["positions"].pop(sym, None)
        else:
            self.st["positions"][sym] = {"amt": new_amt, "entry": entry}
        self._save()
        return {"avg": price, "qty": filled, "status": "FILLED", "order_id": self.st["seq"]}

    def place_stop(self, sym, close_side, qty, trigger):
        m = self._mark(sym)
        if (close_side == "SELL" and m <= trigger) or (close_side == "BUY" and m >= trigger):
            raise BinanceError(400, -2021, "Order would immediately trigger.", "paper")
        self.st["seq"] += 1
        sid = f"P{self.st['seq']}"
        self.st["stops"][sid] = {"sym": sym, "side": close_side, "qty": qty, "trigger": trigger}
        self._save()
        return sid

    def cancel_stop(self, sym, stop_id):
        self.st["stops"].pop(str(stop_id), None)
        self._save()
        return True

    def open_stops(self, sym):
        return [{"id": sid, "trigger": s["trigger"], "side": s["side"], "qty": s["qty"],
                 "reduce_only": True, "close_position": False, "client_id": sid}
                for sid, s in self.st["stops"].items() if s["sym"] == sym]

    def realized(self, sym, since_ms, side, min_order_id=None):
        rows = [t for t in self.st["trades"] if t["symbol"] == sym and t["time"] >= since_ms
                and (min_order_id is None or t.get("orderId", t["id"]) >= int(min_order_id))]
        close_side = "SELL" if side == "LONG" else "BUY"
        closing = [t for t in rows if t["side"] == close_side]
        cq = sum(t["qty"] for t in closing)
        return {"pnl": sum(t["realizedPnl"] for t in rows), "fee": sum(t["commission"] for t in rows),
                "funding": 0.0, "closed_qty": cq,
                "exit_price": (sum(t["price"] * t["qty"] for t in closing) / cq) if cq else 0.0,
                "n": len(rows)}
