"""A fake Binance USD-M futures exchange for tests.

FakeExchange keeps deterministic minute-level price paths, an account with one-way isolated
positions, market orders, Algo-Service conditional orders (triggered on the minute "mark" price),
funding every 8h, userTrades and income. It verifies HMAC signatures and timestamps and rejects
the request shapes real Binance rejects (STOP_MARKET on /fapi/v1/order -> -4120, immediate
trigger -> -2021, bad precision -> -1111, reduce-only without position -> -2022).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import random
import threading
import urllib.parse
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MIN = 60_000
TF = {"1m": MIN, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
SPECS = {
    "BTCUSDT": {"base": 60000.0, "tick": "0.10", "step": "0.001", "min_qty": "0.001", "min_notional": "100"},
    "AAAUSDT": {"base": 100.0, "tick": "0.01", "step": "0.01", "min_qty": "0.01", "min_notional": "5"},
    "BBBUSDT": {"base": 2.0, "tick": "0.0001", "step": "1", "min_qty": "1", "min_notional": "5"},
}


class SimClock:
    def __init__(self, t):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def _is_multiple(value, step):
    v, s = Decimal(str(value)), Decimal(str(step))
    return (v / s) == (v / s).to_integral_value()


class FakeExchange:
    def __init__(self, clock, symbols=("AAAUSDT", "BBBUSDT", "BTCUSDT"), balance=100.0,
                 key="k" * 64, secret="s" * 64, history_days=320, test_days=8, seed=11, direction=1,
                 segments=None, spot_balance=0.0):
        self.clock = clock
        self.key = key
        self.secret = secret
        self.lock = threading.RLock()
        self.symbols = list(symbols)
        now_ms = int(clock() * 1000)
        self.path_start = (now_ms - history_days * 86_400_000) // TF["1d"] * TF["1d"]
        total_min = (now_ms - self.path_start) // MIN + test_days * 1440 + 10
        if segments is None:
            hist = (now_ms - self.path_start) // MIN
            d = 1440
            segments = [
                (hist, direction * math.log(2.0) / hist, 0.0007),          # long trend: daily MAs line up
                (4 * d, direction * math.log(1.15) / (4 * d), 0.0008),     # rally -> breakouts
                (2 * d, direction * math.log(0.90) / (2 * d), 0.0010),     # sharp pullback -> stops
                (4 * d, direction * math.log(1.12) / (4 * d), 0.0008),     # rally again
                (3 * d, 0.0, 0.0007),                                      # chop
                (total_min, direction * math.log(1.10) / (6 * d), 0.0008),
            ]
        self.paths = {}
        for k, sym in enumerate(self.symbols):
            rnd = random.Random(seed * 100 + k)
            p = SPECS[sym]["base"]
            arr = []
            for n, drift, vol in segments:
                for _ in range(int(n)):
                    if len(arr) >= total_min:
                        break
                    p *= math.exp(drift + rnd.gauss(0, vol))
                    arr.append(p)
            self.paths[sym] = arr
        self.candles = {}
        for sym in self.symbols:
            for tf in ("1h", "4h", "1d"):
                self.candles[(sym, tf)] = self._aggregate(sym, TF[tf])
        self.wallet = float(balance)
        self.positions = {}
        self.leverage = {s: 20 for s in self.symbols}
        self.margin_type = {s: "CROSSED" for s in self.symbols}
        self.dual = False
        self.multi = False
        self.orders = {}
        self.algos = {}
        self.trades = []
        self.income = []
        self.seq = 1000
        self.last_min = self._minute(now_ms)
        self.reject_algo = False
        self.calls = []
        # ---- spot side (same price paths)
        self.spot_bal = {"USDT": float(spot_balance)}
        self.spot_orders = {}
        self.spot_trades = []
        self.transfers = []
        self.restrictions = {"ipRestrict": True, "enableReading": True, "enableFutures": True,
                             "enableSpotAndMarginTrading": True, "permitsUniversalTransfer": True,
                             "enableWithdrawals": False}
        self.spot_drop_response = 0  # >0: process the next spot order but lose the HTTP response
        self.transfer_drop_response = 0
        self.transfer_5xx_after = 0     # >0: execute the transfer, then answer 503 "unknown"
        self.spot_hide_lookups = 0      # >0: the next N spot order lookups say "does not exist"
        self.spot_exinfo_fail = False
        self.transfer_history_fail = False
        self.spot_status = {}            # symbol -> status override in spot exchangeInfo (e.g. "BREAK")
        self.drop_order_response = 0    # futures: execute the order, then answer 503
        self.hide_order_lookups = 0     # futures: the next N lookups say "does not exist"
        self.funding_rate = None        # futures: set a rate (e.g. 0.0001) to serve /fapi/v1/fundingRate history

    # ------------------------------------------------------------ prices
    def _minute(self, ts_ms):
        return max(0, min(len(next(iter(self.paths.values()))) - 1, (ts_ms - self.path_start) // MIN))

    def price(self, sym, ts_ms=None):
        ts_ms = int(self.clock() * 1000) if ts_ms is None else ts_ms
        return self.paths[sym][self._minute(ts_ms)]

    def _aggregate(self, sym, tfm):
        arr = self.paths[sym]
        per = tfm // MIN
        out = []
        prev = arr[0]
        for j in range(len(arr) // per):
            seg = arr[j * per:(j + 1) * per]
            o = prev
            h = max(max(seg), o) * 1.0006
            lo = min(min(seg), o) * 0.9994
            out.append((self.path_start + j * tfm, o, h, lo, seg[-1]))
            prev = seg[-1]
        return out

    def klines(self, sym, interval, limit=500, start_time=None, end_time=None):
        tfm = TF[interval]
        now_ms = int(self.clock() * 1000)
        end = min(int(end_time) if end_time else now_ms, now_ms)
        full = self.candles[(sym, interval)]
        rows = []
        for (t, o, h, lo, c) in full:
            if t + tfm <= now_ms:
                rows.append([t, f"{o}", f"{h}", f"{lo}", f"{c}", "1", t + tfm - 1])
        # forming candle
        cur = (now_ms - self.path_start) // tfm * tfm + self.path_start
        m0, m1 = self._minute(cur), self._minute(now_ms)
        seg = self.paths[sym][m0:m1 + 1]
        if seg:
            o = self.paths[sym][max(m0 - 1, 0)]
            rows.append([cur, f"{o}", f"{max(seg + [o])}", f"{min(seg + [o])}", f"{seg[-1]}", "1", cur + tfm - 1])
        rows = [r for r in rows if r[0] <= end]
        if start_time:
            rows = [r for r in rows if r[0] >= int(start_time)]
            return rows[:int(limit)]
        return rows[-int(limit):]

    # ------------------------------------------------------------ simulation
    def advance(self):
        now_ms = int(self.clock() * 1000)
        m_now = self._minute(now_ms)
        while self.last_min < m_now:
            self.last_min += 1
            t = self.path_start + self.last_min * MIN
            for aid, a in list(self.algos.items()):
                if a["algoStatus"] != "NEW":
                    continue
                p = self.paths[a["symbol"]][self.last_min]
                trig = float(a["triggerPrice"])
                if (a["side"] == "SELL" and p <= trig) or (a["side"] == "BUY" and p >= trig):
                    pos = self.positions.get(a["symbol"])
                    want_dir = -1 if a["side"] == "SELL" else 1
                    if pos and (pos["amt"] > 0) == (want_dir < 0):
                        q = min(float(a["quantity"]), abs(pos["amt"]))
                        fill = p * (1 - 0.0003) if a["side"] == "SELL" else p * (1 + 0.0003)
                        self._fill(a["symbol"], a["side"], q, fill, t, reduce_only=True)
                        a["algoStatus"] = "FINISHED"
                    else:
                        a["algoStatus"] = "EXPIRED"
                    a["triggerTime"] = t
            if t % (8 * 3_600_000) == 0:
                for sym, pos in self.positions.items():
                    p = self.paths[sym][self.last_min]
                    fee = abs(pos["amt"]) * p * 0.0001 * (1 if pos["amt"] > 0 else -1)
                    self.wallet -= fee
                    self.income.append({"symbol": sym, "incomeType": "FUNDING_FEE", "income": f"{-fee}",
                                        "asset": "USDT", "time": t})

    def _fill(self, sym, side, qty, price, t, reduce_only=False):
        pos = self.positions.get(sym, {"amt": 0.0, "entry": 0.0})
        amt = pos["amt"]
        signed = qty if side == "BUY" else -qty
        realized = 0.0
        if amt == 0 or (amt > 0) == (signed > 0):
            new_amt = amt + signed
            entry = (abs(amt) * pos["entry"] + qty * price) / abs(new_amt)
        else:
            cq = min(qty, abs(amt))
            realized = (price - pos["entry"]) * cq * (1 if amt > 0 else -1)
            new_amt = amt + (cq if signed > 0 else -cq)
            entry = pos["entry"]
        fee = qty * price * 0.0005
        self.wallet += realized - fee
        self.seq += 1
        self.trades.append({"id": self.seq, "orderId": self.seq, "symbol": sym, "side": side, "price": f"{price}",
                            "qty": f"{qty}", "realizedPnl": f"{realized}", "commission": f"{fee}",
                            "commissionAsset": "USDT", "time": t, "positionSide": "BOTH", "maker": False})
        if abs(new_amt) < 1e-12:
            self.positions.pop(sym, None)
        else:
            self.positions[sym] = {"amt": new_amt, "entry": entry}
        return self.seq

    def margin_used(self):
        return sum(abs(p["amt"]) * p["entry"] / self.leverage[s] for s, p in self.positions.items())

    def upnl(self):
        return sum((self.price(s) - p["entry"]) * p["amt"] for s, p in self.positions.items())

    # ------------------------------------------------------------ request handling
    SIGNED_PREFIX = ("/fapi/v1/order", "/fapi/v1/algoOrder", "/fapi/v1/openAlgoOrders", "/fapi/v3/", "/fapi/v2/",
                     "/fapi/v1/leverage", "/fapi/v1/marginType", "/fapi/v1/positionSide", "/fapi/v1/multiAssetsMargin",
                     "/fapi/v1/userTrades", "/fapi/v1/income", "/api/v3/account", "/api/v3/order", "/api/v3/myTrades",
                     "/sapi/")

    def handle_raw(self, method, path, qs, headers):
        with self.lock:
            self.advance()
            self.calls.append((method, path))
            params = dict(urllib.parse.parse_qsl(qs, keep_blank_values=True))
            hdr = {k.lower(): v for k, v in (headers or {}).items()}
            if path.startswith(self.SIGNED_PREFIX):
                if hdr.get("x-mbx-apikey") != self.key:
                    return 401, {"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."}
                if "signature" not in params:
                    return 400, {"code": -1102, "msg": "Mandatory parameter 'signature' was not sent."}
                body, _, sig = qs.rpartition("&signature=")
                good = hmac.new(self.secret.encode(), body.encode(), hashlib.sha256).hexdigest()
                if sig != good:
                    return 400, {"code": -1022, "msg": "Signature for this request is not valid."}
                now_ms = int(self.clock() * 1000)
                if abs(now_ms - int(params["timestamp"])) > int(params.get("recvWindow", 5000)):
                    return 400, {"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."}
                params.pop("signature")
            try:
                return self.route(method, path, params)
            except KeyError as e:
                return 400, {"code": -1102, "msg": f"Mandatory parameter {e} was not sent."}

    def err(self, code, msg, status=400):
        return status, {"code": code, "msg": msg}

    # ------------------------------------------------------------ spot
    QV = {"BTCUSDT": 3e9, "AAAUSDT": 9e8, "BBBUSDT": 2e8}

    def spot_klines(self, sym, interval, limit=500, start_time=None, end_time=None):
        rows = self.klines(sym, interval, min(int(limit), 1000), start_time, end_time)
        qv = self.QV.get(sym, 1e8)
        return [r[:6] + [r[6], f"{qv}", 100, "0", "0", "0"] for r in rows]

    def futures_available(self):
        return self.wallet - self.margin_used() + min(0.0, self.upnl())

    def spot_route(self, method, path, P, now_ms):
        if path == "/api/v3/time":
            return 200, {"serverTime": now_ms}
        if path == "/api/v3/exchangeInfo":
            if self.spot_exinfo_fail:
                return 503, {"code": -1001, "msg": "Internal error; unable to process your request. Please try again."}
            want = json.loads(P["symbols"]) if P.get("symbols") else self.symbols
            bad = [x for x in want if x not in self.symbols]
            if bad:
                return self.err(-1121, "Invalid symbol.")
            rows = []
            for sym in want:
                sp = SPECS[sym]
                rows.append({"symbol": sym, "status": self.spot_status.get(sym, "TRADING"), "baseAsset": sym[:-4],
                             "quoteAsset": "USDT", "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": sp["tick"]},
                    {"filterType": "LOT_SIZE", "stepSize": sp["step"], "minQty": sp["min_qty"], "maxQty": "900000"},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.00000000", "minQty": "0", "maxQty": "100000"},
                    {"filterType": "NOTIONAL", "minNotional": "5.00000000", "applyMinToMarket": True}]})
            return 200, {"symbols": rows}
        if path == "/api/v3/klines":
            if int(P.get("limit", 500)) > 1000:
                return self.err(-1100, "Illegal characters found in parameter 'limit'; legal range is '1-1000'.")
            return 200, self.spot_klines(P["symbol"], P["interval"], P.get("limit", 500), P.get("startTime"), P.get("endTime"))
        if path == "/api/v3/ticker/price":
            want = json.loads(P["symbols"]) if P.get("symbols") else ([P["symbol"]] if P.get("symbol") else self.symbols)
            return 200, [{"symbol": x, "price": f"{self.price(x)}"} for x in want]
        if path == "/api/v3/account":
            return 200, {"balances": [{"asset": a, "free": f"{v:.8f}", "locked": "0.00000000"}
                                      for a, v in self.spot_bal.items() if v > 0 or P.get("omitZeroBalances") != "true"]}
        if path == "/sapi/v1/account/apiRestrictions":
            return 200, dict(self.restrictions)
        if path == "/sapi/v1/asset/transfer" and method == "GET":
            if self.transfer_history_fail:
                return 503, {"code": -1001, "msg": "Internal error"}
            st = int(P.get("startTime") or 0)
            rows = [{"asset": "USDT", "amount": f"{t['amount']:.8f}", "type": t["type"], "status": "CONFIRMED",
                     "tranId": t.get("tranId", 0), "timestamp": t["time"]}
                    for t in self.transfers if t["type"] == P["type"] and t["time"] >= st]
            return 200, {"total": len(rows), "rows": list(reversed(rows))}
        if path == "/sapi/v1/asset/transfer":
            if not self.restrictions["permitsUniversalTransfer"]:
                return self.err(-1002, "You are not authorized to execute this request.", 401)
            amt = float(P["amount"])
            if P["type"] == "MAIN_UMFUTURE":
                if self.spot_bal.get("USDT", 0.0) + 1e-9 < amt:
                    return self.err(-5013, "Asset transfer failed: insufficient balance")
                self.spot_bal["USDT"] -= amt
                self.wallet += amt
            elif P["type"] == "UMFUTURE_MAIN":
                if self.futures_available() + 1e-9 < amt:
                    return self.err(-5013, "Asset transfer failed: insufficient balance")
                self.wallet -= amt
                self.spot_bal["USDT"] = self.spot_bal.get("USDT", 0.0) + amt
            else:
                return self.err(-1102, "bad type")
            self.seq += 1
            self.transfers.append({"type": P["type"], "amount": amt, "time": now_ms, "tranId": self.seq})
            if self.transfer_drop_response > 0:
                self.transfer_drop_response -= 1
                return 599, {"code": -1007, "msg": "Timeout"}
            if self.transfer_5xx_after > 0:
                self.transfer_5xx_after -= 1
                return 503, {"code": -1001, "msg": "Internal error; unable to process your request."}
            return 200, {"tranId": self.seq}
        if path == "/api/v3/order":
            return self.spot_order(method, P, now_ms)
        if path == "/api/v3/myTrades":
            return 200, [t for t in self.spot_trades if t["symbol"] == P["symbol"]]
        return 404, {"code": -5000, "msg": f"no route {path}"}

    def spot_order(self, method, P, now_ms):
        if method == "GET":
            if self.spot_hide_lookups > 0:
                self.spot_hide_lookups -= 1
                return self.err(-2013, "Order does not exist.")
            o = None
            if P.get("orderId"):
                o = self.spot_orders.get(int(P["orderId"]))
            elif P.get("origClientOrderId"):
                o = next((x for x in self.spot_orders.values() if x["clientOrderId"] == P["origClientOrderId"]), None)
            if not o:
                return self.err(-2013, "Order does not exist.")
            o = dict(o)
            o.pop("fills", None)
            return 200, o
        sym, side = P["symbol"], P["side"]
        if P.get("type") != "MARKET":
            return self.err(-1116, "Invalid orderType.")
        sp = SPECS[sym]
        base = sym[:-4]
        px = self.price(sym)
        if side == "BUY":
            if "quoteOrderQty" in P:
                quote = float(P["quoteOrderQty"])
                fill = px * 1.0002
                step = Decimal(sp["step"])
                qty = float((Decimal(str(quote / fill)) / step).to_integral_value(rounding="ROUND_FLOOR") * step)
            else:
                if not _is_multiple(P["quantity"], sp["step"]):
                    return self.err(-1111, "Precision is over the maximum defined for this asset.")
                qty = float(P["quantity"])
                fill = px * 1.0002
            cost = qty * fill
            if cost < 5.0:
                return self.err(-1013, "Filter failure: NOTIONAL")
            if self.spot_bal.get("USDT", 0.0) + 1e-9 < cost:
                return self.err(-2010, "Account has insufficient balance for requested action.")
            fee = qty * 0.001
            self.spot_bal["USDT"] -= cost
            self.spot_bal[base] = self.spot_bal.get(base, 0.0) + qty - fee
            fee_asset = base
        else:
            if not _is_multiple(P["quantity"], sp["step"]):
                return self.err(-1111, "Precision is over the maximum defined for this asset.")
            qty = float(P["quantity"])
            fill = px * 0.9998
            cost = qty * fill
            if cost < 5.0:
                return self.err(-1013, "Filter failure: NOTIONAL")
            if self.spot_bal.get(base, 0.0) + 1e-12 < qty:
                return self.err(-2010, "Account has insufficient balance for requested action.")
            fee = cost * 0.001
            self.spot_bal[base] -= qty
            self.spot_bal["USDT"] = self.spot_bal.get("USDT", 0.0) + cost - fee
            fee_asset = "USDT"
        self.seq += 1
        o = {"symbol": sym, "orderId": self.seq, "clientOrderId": P.get("newClientOrderId", f"auto{self.seq}"),
             "status": "FILLED", "type": "MARKET", "side": side, "executedQty": f"{qty}",
             "cummulativeQuoteQty": f"{cost}", "transactTime": now_ms,
             "fills": [{"price": f"{fill}", "qty": f"{qty}", "commission": f"{fee}", "commissionAsset": fee_asset}]}
        self.spot_orders[self.seq] = o
        self.spot_trades.append({"symbol": sym, "id": self.seq, "orderId": self.seq, "price": f"{fill}", "qty": f"{qty}",
                                 "quoteQty": f"{cost}", "commission": f"{fee}", "commissionAsset": fee_asset,
                                 "time": now_ms, "isBuyer": side == "BUY"})
        if self.spot_drop_response > 0:
            self.spot_drop_response -= 1
            return 599, {"code": -1007, "msg": "Timeout waiting for response from backend server."}
        return 200, dict(o)

    def spot_value(self):
        v = self.spot_bal.get("USDT", 0.0)
        for sym in self.symbols:
            v += self.spot_bal.get(sym[:-4], 0.0) * self.price(sym)
        return v

    def route(self, method, path, P):
        now_ms = int(self.clock() * 1000)
        if path.startswith("/api/") or path.startswith("/sapi/"):
            return self.spot_route(method, path, P, now_ms)
        if path == "/fapi/v1/time":
            return 200, {"serverTime": now_ms}
        if path == "/fapi/v1/exchangeInfo":
            syms = []
            for s in self.symbols:
                sp = SPECS[s]
                syms.append({"symbol": s, "status": "TRADING", "contractType": "PERPETUAL", "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": sp["tick"], "minPrice": "0.0001", "maxPrice": "1000000"},
                    {"filterType": "LOT_SIZE", "stepSize": sp["step"], "minQty": sp["min_qty"], "maxQty": "100000"},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": sp["step"], "minQty": sp["min_qty"], "maxQty": "50000"},
                    {"filterType": "MIN_NOTIONAL", "notional": sp["min_notional"]}]})
            return 200, {"symbols": syms}
        if path == "/fapi/v1/klines":
            return 200, self.klines(P["symbol"], P["interval"], P.get("limit", 500), P.get("startTime"), P.get("endTime"))
        if path == "/fapi/v1/fundingRate":
            if self.funding_rate is None:
                return 404, {"code": -5000, "msg": f"no route {path}"}
            step = 8 * 3_600_000
            t = max(int(P.get("startTime", self.path_start)), self.path_start)
            t = -(-t // step) * step
            end = min(int(P.get("endTime", now_ms)), now_ms)
            rows = []
            while t <= end and len(rows) < int(P.get("limit", 100)):
                rows.append({"symbol": P["symbol"], "fundingTime": t, "fundingRate": f"{self.funding_rate}",
                             "markPrice": f"{self.price(P['symbol'], t)}"})
                t += step
            return 200, rows
        if path == "/fapi/v1/premiumIndex":
            return 200, [{"symbol": s, "markPrice": f"{self.price(s)}"} for s in self.symbols]
        if path in ("/fapi/v3/account", "/fapi/v2/account"):
            up = self.upnl()
            avail = self.wallet - self.margin_used() + min(0.0, up)
            return 200, {"totalWalletBalance": f"{self.wallet}", "totalUnrealizedProfit": f"{up}",
                         "totalMarginBalance": f"{self.wallet + up}", "availableBalance": f"{avail}",
                         "assets": [{"asset": "USDT", "walletBalance": f"{self.wallet}"}], "positions": []}
        if path in ("/fapi/v3/positionRisk", "/fapi/v2/positionRisk"):
            rows = []
            for s, p in self.positions.items():
                if P.get("symbol") and P["symbol"] != s:
                    continue
                m = self.price(s)
                rows.append({"symbol": s, "positionSide": "BOTH", "positionAmt": f"{p['amt']}",
                             "entryPrice": f"{p['entry']}", "markPrice": f"{m}",
                             "unRealizedProfit": f"{(m - p['entry']) * p['amt']}", "liquidationPrice": "0"})
            return 200, rows
        if path == "/fapi/v1/positionSide/dual":
            if method == "GET":
                return 200, {"dualSidePosition": self.dual}
            want = P["dualSidePosition"] == "true"
            if want == self.dual:
                return self.err(-4059, "No need to change position side.")
            self.dual = want
            return 200, {"code": 200, "msg": "success"}
        if path == "/fapi/v1/multiAssetsMargin":
            if method == "GET":
                return 200, {"multiAssetsMargin": self.multi}
            self.multi = P["multiAssetsMargin"] == "true"
            return 200, {"code": 200, "msg": "success"}
        if path == "/fapi/v1/leverage":
            self.leverage[P["symbol"]] = int(P["leverage"])
            return 200, {"leverage": int(P["leverage"]), "symbol": P["symbol"], "maxNotionalValue": "1000000"}
        if path == "/fapi/v1/marginType":
            if self.margin_type[P["symbol"]] == P["marginType"]:
                return self.err(-4046, "No need to change margin type.")
            if P["symbol"] in self.positions:
                return self.err(-4048, "Margin type cannot be changed if there exists position.")
            self.margin_type[P["symbol"]] = P["marginType"]
            return 200, {"code": 200, "msg": "success"}
        if path == "/fapi/v1/order":
            return self.order(method, P, now_ms)
        if path == "/fapi/v1/algoOrder":
            return self.algo(method, P, now_ms)
        if path == "/fapi/v1/openAlgoOrders":
            return 200, [dict(a) for a in self.algos.values()
                         if a["algoStatus"] == "NEW" and (not P.get("symbol") or a["symbol"] == P["symbol"])]
        if path == "/fapi/v1/userTrades":
            st = int(P.get("startTime", 0))
            en = int(P.get("endTime", 10 ** 15))
            if P.get("startTime") and P.get("endTime") and en - st > 7 * 86_400_000:
                return self.err(-4166, "The time between startTime and endTime cannot be longer than 7 days.")
            return 200, [t for t in self.trades if t["symbol"] == P["symbol"] and st <= t["time"] <= en][:int(P.get("limit", 500))]
        if path == "/fapi/v1/income":
            st = int(P.get("startTime", 0))
            en = int(P.get("endTime", 10 ** 15))
            return 200, [i for i in self.income if (not P.get("symbol") or i["symbol"] == P["symbol"])
                         and (not P.get("incomeType") or i["incomeType"] == P["incomeType"]) and st <= i["time"] <= en]
        return 404, {"code": -5000, "msg": f"no route {path}"}

    def order(self, method, P, now_ms):
        if method == "GET":
            if self.hide_order_lookups > 0:
                self.hide_order_lookups -= 1
                return self.err(-2013, "Order does not exist.")
            o = self.orders.get(P.get("orderId")) or next(
                (x for x in self.orders.values() if x["clientOrderId"] == P.get("origClientOrderId")), None)
            return (200, o) if o else self.err(-2013, "Order does not exist.")
        if method != "POST":
            return self.err(-1000, "unsupported")
        sym, side, typ = P["symbol"], P["side"], P["type"]
        if typ in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT", "TRAILING_STOP_MARKET"):
            return self.err(-4120, "Order type not supported for this endpoint. Please use the Algo Order API endpoints instead.")
        if typ != "MARKET":
            return self.err(-1116, "Invalid orderType.")
        sp = SPECS[sym]
        qty = float(P["quantity"])
        if not _is_multiple(P["quantity"], sp["step"]):
            return self.err(-1111, "Precision is over the maximum defined for this asset.")
        reduce_only = P.get("reduceOnly") == "true"
        pos = self.positions.get(sym)
        price = self.price(sym) * (1.0002 if side == "BUY" else 0.9998)
        if reduce_only:
            if not pos or (pos["amt"] > 0) == (side == "BUY"):
                return self.err(-2022, "ReduceOnly Order is rejected.")
            qty = min(qty, abs(pos["amt"]))
        else:
            if qty * price < float(sp["min_notional"]):
                return self.err(-4164, "Order's notional must be no smaller than min notional.")
            avail = self.wallet - self.margin_used() + min(0.0, self.upnl())
            if qty * price / self.leverage[sym] > avail:
                return self.err(-2019, "Margin is insufficient.")
        oid = self._fill(sym, side, qty, price, now_ms, reduce_only)
        rec = {"orderId": oid, "clientOrderId": P.get("newClientOrderId", f"auto{oid}"), "symbol": sym,
               "status": "FILLED", "avgPrice": f"{price}", "executedQty": f"{qty}", "side": side, "type": "MARKET",
               "reduceOnly": reduce_only}
        self.orders[oid] = rec
        if self.drop_order_response > 0:
            self.drop_order_response -= 1
            return 503, {"code": -1001, "msg": "Internal error; unable to process your request."}
        return 200, rec

    def algo(self, method, P, now_ms):
        if method == "GET":
            a = self.algos.get(int(P["algoId"])) if P.get("algoId") else None
            return (200, a) if a else self.err(-2013, "Order does not exist.")
        if method == "DELETE":
            aid = int(P["algoId"])
            a = self.algos.get(aid)
            if not a or a["algoStatus"] != "NEW":
                return self.err(-2011, "Unknown order sent.")
            a["algoStatus"] = "CANCELED"
            return 200, {"algoId": aid, "clientAlgoId": a["clientAlgoId"], "code": "200", "msg": "success"}
        if self.reject_algo:
            return self.err(-1000, "An unknown error occurred while processing the request.")
        if P.get("algoType") != "CONDITIONAL":
            return self.err(-1116, "Invalid algoType.")
        sym, side, typ = P["symbol"], P["side"], P["type"]
        if typ not in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT", "TRAILING_STOP_MARKET"):
            return self.err(-1116, "Invalid orderType.")
        sp = SPECS[sym]
        if "triggerPrice" not in P:
            return self.err(-1102, "Mandatory parameter 'triggerPrice' was not sent.")
        if not _is_multiple(P["triggerPrice"], sp["tick"]):
            return self.err(-1111, "Precision is over the maximum defined for this asset.")
        close_pos = P.get("closePosition") == "true"
        if close_pos and (P.get("quantity") or P.get("reduceOnly") == "true"):
            return self.err(-1106, "Parameter 'quantity'/'reduceOnly' sent when not required.")
        if not close_pos and not _is_multiple(P["quantity"], sp["step"]):
            return self.err(-1111, "Precision is over the maximum defined for this asset.")
        trig = float(P["triggerPrice"])
        m = self.price(sym)
        if typ == "STOP_MARKET" and ((side == "SELL" and m <= trig) or (side == "BUY" and m >= trig)):
            return self.err(-2021, "Order would immediately trigger.")
        if P.get("workingType", "CONTRACT_PRICE") not in ("MARK_PRICE", "CONTRACT_PRICE"):
            return self.err(-1116, "Invalid workingType.")
        self.seq += 1
        a = {"algoId": self.seq, "clientAlgoId": P.get("clientAlgoId", f"auto{self.seq}"), "algoType": "CONDITIONAL",
             "orderType": typ, "symbol": sym, "side": side, "positionSide": "BOTH", "quantity": P.get("quantity", "0"),
             "algoStatus": "NEW", "triggerPrice": P["triggerPrice"], "price": "0", "workingType": P.get("workingType"),
             "closePosition": close_pos, "priceProtect": P.get("priceProtect") == "true",
             "reduceOnly": P.get("reduceOnly") == "true", "createTime": now_ms, "updateTime": now_ms, "triggerTime": 0}
        self.algos[self.seq] = a
        return 200, dict(a)

    # ------------------------------------------------------------ test helpers
    def open_algos(self, sym=None):
        return [a for a in self.algos.values() if a["algoStatus"] == "NEW" and (sym is None or a["symbol"] == sym)]


def make_api(fake, clock):
    """BinanceFutures that talks to the fake in-process (signing and parsing are the real code paths)."""
    from binance_api import BinanceFutures

    class FakeAPI(BinanceFutures):
        def _send(self, method, url, headers):
            u = urllib.parse.urlsplit(url)
            status, body = fake.handle_raw(method, u.path, u.query, headers)
            return status, json.dumps(body).encode(), {}

    return FakeAPI(fake.key, fake.secret, base_url="https://fake.local", clock=clock, sleep=lambda s: None)


def make_spot_api(fake, clock):
    from binance_spot import BinanceSpot

    class FakeSpotAPI(BinanceSpot):
        def _send(self, method, url, headers):
            u = urllib.parse.urlsplit(url)
            status, body = fake.handle_raw(method, u.path, u.query, headers)
            if status == 599:  # simulate a lost response (request processed, answer never arrived)
                raise TimeoutError("simulated timeout")
            return status, json.dumps(body).encode(), {}

    return FakeSpotAPI(fake.key, fake.secret, base_url="https://fake-spot.local", clock=clock, sleep=lambda s: None)


def serve_http(fake, port=0):
    """Expose the fake over real HTTP on localhost. Returns (server, base_url)."""

    class H(BaseHTTPRequestHandler):
        def _do(self):
            u = urllib.parse.urlsplit(self.path)
            status, body = fake.handle_raw(self.command, u.path, u.query, dict(self.headers))
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_DELETE = _do

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"
