"""Minimal Binance USD-M Futures REST client (standard library only).

Conditional orders (STOP_MARKET etc.) use the Algo Order service
(POST /fapi/v1/algoOrder) — Binance moved them there in Dec 2025 and the
old /fapi/v1/order path rejects them with -4120.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "https://fapi.binance.com"


class BinanceError(Exception):
    def __init__(self, status, code, msg, path=""):
        super().__init__(f"[{status}] {code}: {msg} ({path})")
        self.status = status
        self.code = code
        self.msg = msg
        self.path = path


class NetworkError(Exception):
    """Timeout / connection problem: the request may or may not have reached Binance."""


def _bool(v):
    return "true" if v else "false"


class BinanceFutures:
    def __init__(self, key="", secret="", base_url=DEFAULT_BASE, timeout=10, recv_window=6000,
                 clock=time.time, sleep=time.sleep, log=None):
        self.key = key or ""
        self.secret = (secret or "").encode()
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.recv_window = recv_window
        self.clock = clock
        self.sleep = sleep
        self.log = log
        self.time_offset_ms = 0.0

    # ------------------------------------------------------------ transport
    def _send(self, method, url, headers):
        """One HTTP round trip. Returns (status, body_bytes, headers). Network problems raise."""
        data = b"" if method in ("POST", "PUT") else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers or {})

    def _request(self, method, path, params=None, signed=False, retries=None, _resynced=False):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        for k, v in list(params.items()):
            if isinstance(v, bool):
                params[k] = _bool(v)
        if retries is None:
            retries = 3 if method == "GET" else 0
        attempt = 0
        while True:
            q = dict(params)
            if signed:
                q["timestamp"] = int(self.clock() * 1000 + self.time_offset_ms)
                q["recvWindow"] = self.recv_window
            qs = urllib.parse.urlencode(q)
            if signed:
                sig = hmac.new(self.secret, qs.encode(), hashlib.sha256).hexdigest()
                qs = f"{qs}&signature={sig}"
            url = self.base + path + ("?" + qs if qs else "")
            headers = {"User-Agent": "TrendHunter/1.0"}
            if self.key:
                headers["X-MBX-APIKEY"] = self.key
            try:
                status, body, rh = self._send(method, url, headers)
            except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError, OSError) as e:
                if attempt < retries:
                    attempt += 1
                    self.sleep(1.5 * attempt)
                    continue
                raise NetworkError(f"{method} {path}: {e}") from e
            if 200 <= status < 300:
                return json.loads(body) if body else {}
            try:
                j = json.loads(body)
                code, msg = j.get("code"), j.get("msg")
            except Exception:
                code, msg = None, (body or b"")[:200].decode(errors="replace")
            if code == -1021 and signed and not _resynced:  # timestamp outside recvWindow
                self.sync_time()
                return self._request(method, path, params, signed, retries, _resynced=True)
            if (status in (429, 418) or status >= 500) and attempt < retries:
                attempt += 1
                try:
                    wait = float((rh or {}).get("Retry-After") or 2 * attempt)
                except ValueError:
                    wait = 2 * attempt
                self.sleep(min(wait, 30))
                continue
            raise BinanceError(status, code, msg, path)

    # ------------------------------------------------------------ public
    def server_time(self):
        return int(self._request("GET", "/fapi/v1/time")["serverTime"])

    def sync_time(self):
        t0 = self.clock() * 1000
        st = self.server_time()
        t1 = self.clock() * 1000
        self.time_offset_ms = st - (t0 + t1) / 2.0
        return self.time_offset_ms

    def exchange_info(self):
        return self._request("GET", "/fapi/v1/exchangeInfo")

    def klines(self, symbol, interval, limit=500, start_time=None, end_time=None):
        return self._request("GET", "/fapi/v1/klines", {
            "symbol": symbol, "interval": interval, "limit": int(limit),
            "startTime": start_time, "endTime": end_time})

    def mark_prices(self, symbols=None):
        """Mark prices. For a handful of symbols ask one by one (a few hundred bytes instead of the whole
        ~100 KB market list every loop); otherwise fetch the full list."""
        if symbols and len(symbols) <= 8:
            out = {}
            for sym in symbols:
                r = self._request("GET", "/fapi/v1/premiumIndex", {"symbol": sym})
                for row in (r if isinstance(r, list) else [r]):
                    if row.get("symbol") == sym:
                        out[sym] = float(row["markPrice"])
            return out
        rows = self._request("GET", "/fapi/v1/premiumIndex")
        if isinstance(rows, dict):
            rows = [rows]
        out = {r["symbol"]: float(r["markPrice"]) for r in rows}
        if symbols:
            out = {s: out[s] for s in symbols if s in out}
        return out

    # ------------------------------------------------------------ account
    def _v3_or_v2(self, name, params=None):
        try:
            return self._request("GET", f"/fapi/v3/{name}", params, signed=True)
        except BinanceError as e:
            if e.status == 404:
                return self._request("GET", f"/fapi/v2/{name}", params, signed=True)
            raise

    def account(self):
        return self._v3_or_v2("account")

    def position_risk(self, symbol=None):
        return self._v3_or_v2("positionRisk", {"symbol": symbol})

    def get_position_mode(self):
        return bool(self._request("GET", "/fapi/v1/positionSide/dual", signed=True)["dualSidePosition"])

    def set_position_mode(self, dual):
        return self._request("POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": bool(dual)}, signed=True)

    def get_multi_assets(self):
        return bool(self._request("GET", "/fapi/v1/multiAssetsMargin", signed=True)["multiAssetsMargin"])

    def set_multi_assets(self, on):
        return self._request("POST", "/fapi/v1/multiAssetsMargin", {"multiAssetsMargin": bool(on)}, signed=True)

    def set_leverage(self, symbol, leverage):
        return self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)}, signed=True)

    def set_margin_type(self, symbol, margin_type="ISOLATED"):
        try:
            return self._request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type}, signed=True)
        except BinanceError as e:
            if e.code == -4046:  # No need to change margin type
                return {"code": -4046, "msg": "already"}
            raise

    # ------------------------------------------------------------ orders
    def new_order(self, **params):
        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def get_order(self, symbol, order_id=None, client_id=None, retries=None):
        return self._request("GET", "/fapi/v1/order", {
            "symbol": symbol, "orderId": order_id, "origClientOrderId": client_id}, signed=True, retries=retries)

    def cancel_order(self, symbol, order_id=None, client_id=None):
        return self._request("DELETE", "/fapi/v1/order", {
            "symbol": symbol, "orderId": order_id, "origClientOrderId": client_id}, signed=True)

    def new_algo_order(self, **params):
        params.setdefault("algoType", "CONDITIONAL")
        return self._request("POST", "/fapi/v1/algoOrder", params, signed=True)

    def get_algo_order(self, algo_id=None, client_algo_id=None):
        return self._request("GET", "/fapi/v1/algoOrder", {
            "algoId": algo_id, "clientAlgoId": client_algo_id}, signed=True)

    def cancel_algo_order(self, algo_id=None, client_algo_id=None):
        return self._request("DELETE", "/fapi/v1/algoOrder", {
            "algoId": algo_id, "clientAlgoId": client_algo_id}, signed=True)

    def open_algo_orders(self, symbol=None):
        rows = self._request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True)
        if isinstance(rows, dict):  # defensive: some wrappers return {"orders": [...]}
            rows = rows.get("orders") or rows.get("rows") or []
        return rows

    def user_trades(self, symbol, start_time=None, end_time=None, limit=1000):
        return self._request("GET", "/fapi/v1/userTrades", {
            "symbol": symbol, "startTime": start_time, "endTime": end_time, "limit": limit}, signed=True)

    def income(self, symbol=None, income_type=None, start_time=None, end_time=None, limit=1000):
        return self._request("GET", "/fapi/v1/income", {
            "symbol": symbol, "incomeType": income_type, "startTime": start_time,
            "endTime": end_time, "limit": limit}, signed=True)


def parse_filters(exinfo, symbols=None):
    """exchangeInfo -> {symbol: {tick, step, min_qty, max_qty, min_notional, status, contract}}"""
    out = {}
    for s in exinfo.get("symbols", []):
        sym = s.get("symbol")
        if symbols and sym not in symbols:
            continue
        f = {x.get("filterType"): x for x in s.get("filters", [])}
        price_f = f.get("PRICE_FILTER", {})
        lot = f.get("LOT_SIZE", {})
        mlot = f.get("MARKET_LOT_SIZE", {})
        step = mlot.get("stepSize") if mlot and float(mlot.get("stepSize", 0) or 0) > 0 else lot.get("stepSize", "0.001")
        min_qty = max(float(lot.get("minQty", 0) or 0), float(mlot.get("minQty", 0) or 0))
        max_qty_vals = [float(x.get("maxQty")) for x in (lot, mlot) if x.get("maxQty")]
        mn = f.get("MIN_NOTIONAL", {})
        out[sym] = {
            "tick": price_f.get("tickSize", "0.0001"),
            "step": step,
            "min_qty": min_qty or float(step),
            "max_qty": min(max_qty_vals) if max_qty_vals else 1e12,
            "min_notional": float(mn.get("notional") or mn.get("minNotional") or 5.0),
            "status": s.get("status"),
            "contract": s.get("contractType"),
        }
    return out
