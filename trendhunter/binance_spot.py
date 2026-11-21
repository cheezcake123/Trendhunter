"""Minimal Binance Spot + wallet (SAPI) REST client. Shares signing/retry code with BinanceFutures.

Only what the spot sleeve needs: prices, daily klines, balances, MARKET orders, order lookup,
trade history, universal transfer between the spot and USDⓈ-M futures wallets, API key restrictions,
Simple Earn flexible products (idle USDT earns interest), read-only wallet views for /address and /balance
(deposit networks, deposit address and history, every wallet's total) and small balances -> BNB (/dust).
No withdrawal endpoint, on purpose.
"""
from __future__ import annotations

import json

from binance_api import BinanceError, BinanceFutures, NetworkError  # noqa: F401  (re-exported)

SPOT_BASE = "https://api.binance.com"


def _sym_list(symbols):
    return json.dumps(list(symbols), separators=(",", ":"))


class BinanceSpot(BinanceFutures):
    def __init__(self, key="", secret="", base_url=SPOT_BASE, **kw):
        super().__init__(key, secret, base_url=base_url, **kw)

    # ------------------------------------------------------------ public
    def server_time(self):
        return int(self._request("GET", "/api/v3/time")["serverTime"])

    def exchange_info(self, symbols=None):
        params = {"symbols": _sym_list(symbols)} if symbols else None
        return self._request("GET", "/api/v3/exchangeInfo", params)

    def klines(self, symbol, interval, limit=500, start_time=None, end_time=None):
        return self._request("GET", "/api/v3/klines", {
            "symbol": symbol, "interval": interval, "limit": int(limit),
            "startTime": start_time, "endTime": end_time})

    def prices(self, symbols=None):
        params = {"symbols": _sym_list(symbols)} if symbols else None
        rows = self._request("GET", "/api/v3/ticker/price", params)
        if isinstance(rows, dict):
            rows = [rows]
        return {r["symbol"]: float(r["price"]) for r in rows}

    def mark_prices(self, symbols=None):  # same name as the futures client, for shared helpers
        return self.prices(symbols)

    # ------------------------------------------------------------ account
    def account(self):
        return self._request("GET", "/api/v3/account", {"omitZeroBalances": True}, signed=True)

    def balances(self):
        out = {}
        for b in self.account().get("balances", []):
            free, locked = float(b.get("free") or 0), float(b.get("locked") or 0)
            if free or locked:
                out[b["asset"]] = {"free": free, "locked": locked}
        return out

    def api_restrictions(self):
        return self._request("GET", "/sapi/v1/account/apiRestrictions", signed=True)

    # ------------------------------------------------------------ orders
    def new_order(self, **params):
        return self._request("POST", "/api/v3/order", params, signed=True)

    def get_order(self, symbol, order_id=None, client_id=None, retries=None):
        return self._request("GET", "/api/v3/order", {
            "symbol": symbol, "orderId": order_id, "origClientOrderId": client_id}, signed=True, retries=retries)

    def my_trades(self, symbol, start_time=None, end_time=None, limit=1000):
        return self._request("GET", "/api/v3/myTrades", {
            "symbol": symbol, "startTime": start_time, "endTime": end_time, "limit": limit}, signed=True)

    # ------------------------------------------------------------ wallet
    def transfer_history(self, kind, start_ms=None, size=20, retries=None):
        """Recent universal transfers of one type: [{asset, amount, type, status, tranId, timestamp}]."""
        r = self._request("GET", "/sapi/v1/asset/transfer",
                          {"type": kind, "startTime": start_ms, "size": int(size)}, signed=True, retries=retries)
        return (r or {}).get("rows") or []

    def transfer(self, kind, asset, amount):
        """kind: MAIN_UMFUTURE (spot -> USDⓈ-M futures) or UMFUTURE_MAIN (futures -> spot)."""
        return self._request("POST", "/sapi/v1/asset/transfer",
                             {"type": kind, "asset": asset, "amount": amount}, signed=True)

    # ------------------------------------------------------------ Simple Earn (flexible)
    # Binance docs (Simple Earn): list/position are USER_DATA (IP weight 150 each); subscribe/redeem are TRADE
    # (IP weight 1, at most one call per 3 s per account) and need the key's spot trading permission.
    def earn_flexible_list(self, asset="USDT", retries=None):
        """Flexible products: rows of {asset, productId, minPurchaseAmount, canPurchase, isSoldOut, status,
        latestAnnualPercentageRate, ...}."""
        r = self._request("GET", "/sapi/v1/simple-earn/flexible/list", {"asset": asset, "size": 100},
                          signed=True, retries=retries)
        return (r or {}).get("rows") or []

    def earn_flexible_position(self, asset="USDT", retries=None):
        """Holdings: rows of {asset, productId, totalAmount, canRedeem, latestAnnualPercentageRate,
        cumulativeTotalRewards, ...}."""
        r = self._request("GET", "/sapi/v1/simple-earn/flexible/position", {"asset": asset, "size": 100},
                          signed=True, retries=retries)
        return (r or {}).get("rows") or []

    def earn_subscribe(self, product_id, amount):
        """Move `amount` from the SPOT wallet into the product. autoSubscribe=false: otherwise Binance keeps
        sweeping the spot wallet the bot trades from into Earn. Returns {purchaseId, success}."""
        return self._request("POST", "/sapi/v1/simple-earn/flexible/subscribe", {
            "productId": product_id, "amount": amount, "autoSubscribe": False, "sourceAccount": "SPOT"}, signed=True)

    def earn_redeem(self, product_id, amount=None, redeem_all=False):
        """Back to the SPOT wallet: `amount`, or everything. Returns {redeemId, success}."""
        params = {"productId": product_id, "destAccount": "SPOT"}
        if redeem_all:
            params["redeemAll"] = True
        else:
            params["amount"] = amount
        return self._request("POST", "/sapi/v1/simple-earn/flexible/redeem", params, signed=True)

    def earn_locked_position(self, retries=None):
        """Locked Simple Earn holdings: rows of {asset, amount, ...}. Read only (shown by /balance)."""
        r = self._request("GET", "/sapi/v1/simple-earn/locked/position", {"size": 100}, signed=True, retries=retries)
        return (r or {}).get("rows") or []

    # ------------------------------------------------------------ deposits and wallets (read only)
    # Binance docs (Wallet > Capital / Asset): all USER_DATA, readable with the key's read permission. There is no
    # withdrawal here on purpose: the bot never sends coins out of the account.
    def coin_networks(self, coin="USDT"):
        """The networks Binance takes `coin` on: rows of {network, name, depositEnable, depositDesc, minConfirm,
        unLockConfirm, busy, ...} (GET /sapi/v1/capital/config/getall, IP weight 10)."""
        for c in self._request("GET", "/sapi/v1/capital/config/getall", signed=True) or []:
            if c.get("coin") == coin:
                return c.get("networkList") or []
        return []

    def deposit_address(self, coin, network):
        """{address, coin, tag, url} of this account for `coin` on `network` (IP weight 10)."""
        return self._request("GET", "/sapi/v1/capital/deposit/address", {"coin": coin, "network": network},
                             signed=True)

    def deposit_history(self, coin="USDT", start_ms=None, limit=50, retries=None):
        """Recent deposits, newest first: [{id, amount, coin, network, status, txId, insertTime, completeTime,
        confirmTimes, unlockConfirm, walletType}]. status 0 pending, 6 credited (not yet withdrawable),
        1 success, 7 wrong deposit, 8 waiting for the user's confirmation, 2 rejected (IP weight 1)."""
        return self._request("GET", "/sapi/v1/capital/deposit/hisrec", {
            "coin": coin, "startTime": start_ms, "limit": int(limit)}, signed=True, retries=retries) or []

    def wallet_balance(self, quote="USDT"):
        """Every wallet's total: [{walletName, balance, activate}] valued in `quote` (IP weight 60)."""
        return self._request("GET", "/sapi/v1/asset/wallet/balance", {"quoteAsset": quote}, signed=True) or []

    def funding_assets(self):
        """Funding wallet: [{asset, free, locked, freeze, withdrawing, btcValuation}] (POST, USER_DATA)."""
        return self._request("POST", "/sapi/v1/asset/get-funding-asset", {}, signed=True) or []

    # ------------------------------------------------------------ small balances -> BNB (v2.9)
    def dust_assets(self):
        """What Binance would turn into BNB: {details: [{asset, amountFree, toBTC, toBNB, toBNBOffExchange,
        exchange}], totalTransferBNB, dribbletPercentage} (POST /sapi/v1/asset/dust-btc, IP weight 1)."""
        return self._request("POST", "/sapi/v1/asset/dust-btc", {"accountType": "SPOT"}, signed=True) or {}

    def dust(self, assets):
        """Turn these small balances into BNB (needs the trading permission). Returns {totalServiceCharge,
        totalTransfered, transferResult: [{fromAsset, amount, transferedAmount, serviceChargeAmount, tranId}]}.
        Never retried: a lost answer is reported, not sent again."""
        return self._request("POST", "/sapi/v1/asset/dust", {"asset": ",".join(assets), "accountType": "SPOT"},
                             signed=True, retries=0)


def parse_spot_filters(exinfo, symbols=None):
    """exchangeInfo -> {symbol: {tick, step, min_qty, max_qty, min_notional, status, base, quote}}"""
    out = {}
    for s in exinfo.get("symbols", []):
        sym = s.get("symbol")
        if symbols and sym not in symbols:
            continue
        f = {x.get("filterType"): x for x in s.get("filters", [])}
        lot = f.get("LOT_SIZE", {})
        mlot = f.get("MARKET_LOT_SIZE", {})
        step = lot.get("stepSize", "0.00001")
        min_qty = max(float(lot.get("minQty", 0) or 0), float(mlot.get("minQty", 0) or 0))
        maxes = [float(x.get("maxQty")) for x in (lot, mlot) if float(x.get("maxQty", 0) or 0) > 0]
        nf = f.get("NOTIONAL") or {}
        mn = f.get("MIN_NOTIONAL") or {}
        min_notional = float(nf.get("minNotional") or mn.get("minNotional") or 5.0)
        out[sym] = {
            "tick": f.get("PRICE_FILTER", {}).get("tickSize", "0.00000001"),
            "step": step,
            "min_qty": min_qty or float(step),
            "max_qty": min(maxes) if maxes else 1e12,
            "min_notional": min_notional,
            "status": s.get("status"),
            "base": s.get("baseAsset") or (sym[:-4] if sym.endswith("USDT") else sym),
            "quote": s.get("quoteAsset") or "USDT",
        }
    return out
