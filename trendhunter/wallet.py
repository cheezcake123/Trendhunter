"""Wallet views for Telegram (v2.8): USDT deposit addresses, deposit progress and every wallet's balance.

Meant for running the bot without the Binance app. Everything here only READS the account. The one thing that
moves money is Trader.act_funding_to_spot (funding wallet -> spot wallet of the same account). There is no
withdrawal anywhere: coins never leave the account through the bot.
"""
from __future__ import annotations

import html

from binance_api import BinanceError, NetworkError

COIN = "USDT"
# network code (config deposit_networks) -> (label, codes Binance may list it under, network name prefixes)
NETWORKS = {
    "TRX": ("트론 TRC20", ("TRX",), ("tron",)),
    "KAIA": ("카이아 Kaia", ("KAIA", "KLAY"), ("kaia", "klaytn")),
    "APT": ("앱토스 Aptos", ("APT", "APTOS"), ("aptos",)),
    "ETH": ("이더리움 ERC20", ("ETH",), ("ethereum",)),
    "BSC": ("BNB 스마트체인 BEP20", ("BSC",), ("bnb smart chain",)),
    "SOL": ("솔라나", ("SOL",), ("solana",)),
    "TON": ("톤 TON", ("TON",), ("the open network",)),
    "ARBITRUM": ("아비트럼", ("ARBITRUM",), ("arbitrum",)),
    "MATIC": ("폴리곤", ("MATIC", "POL"), ("polygon",)),
    "AVAXC": ("아발란체 C체인", ("AVAXC",), ("avax c-chain",)),
}
DEP_DONE = (1, 6)  # credited: usable for trading (6: not withdrawable yet)
DEP_KO = {0: "오는 중", 1: "입금 완료", 6: "입금 완료", 2: "거절됨", 7: "잘못된 입금", 8: "확인 필요"}
CORE_WALLETS = ("spot", "funding", "futures", "earn")
OTHER_KO = {"cross margin": "교차 마진", "isolated margin": "격리 마진", "coin-m futures": "코인-M 선물",
            "options": "옵션", "trading bots": "트레이딩 봇", "copy trading": "카피 트레이딩"}


def net_label(code):
    return NETWORKS.get(code, (code,))[0]


def match_network(rows, code):
    """The networkList row for one of our codes ('TRX', 'KAIA', ...), or None when Binance does not list it."""
    _, codes, names = NETWORKS.get(code, (code, (code,), ()))
    for r in rows:
        if str(r.get("network") or "").upper() in codes:
            return r
    for r in rows:
        name = str(r.get("name") or "").strip().lower()
        if names and name.startswith(names):
            return r
    return None


def deposit_view(api, codes, coin=COIN):
    """One entry per wanted network: {code, label, network, ok, address, tag, why, confirm}.
    ok False with `why` when Binance does not take USDT there right now (maintenance) or does not list it."""
    rows = api.coin_networks(coin)
    out = []
    for code in codes:
        e = {"code": code, "label": net_label(code), "network": None, "ok": False, "address": "", "tag": "",
             "why": "", "confirm": None}
        r = match_network(rows, code)
        if r is None:
            e["why"] = "바이낸스가 이 네트워크로 USDT를 받지 않습니다"
        elif not r.get("depositEnable"):
            e["network"] = r.get("network")
            e["why"] = "지금 입금 중단(점검 중)" + (f" — {r['depositDesc']}" if r.get("depositDesc") else "")
        else:
            e["network"] = r.get("network")
            e["confirm"] = r.get("minConfirm")
            try:
                a = api.deposit_address(coin, r["network"])
                e["address"] = str(a.get("address") or "")
                e["tag"] = str(a.get("tag") or "")
                e["ok"] = bool(e["address"])
                if not e["ok"]:
                    e["why"] = "주소를 받지 못했습니다"
                elif r.get("busy"):
                    e["why"] = "네트워크 혼잡: 늦게 도착할 수 있습니다"
            except (BinanceError, NetworkError) as err:
                e["why"] = f"주소 조회 실패({err})"
        out.append(e)
    return out


def deposit_text(view, recent, coin=COIN):
    """Telegram HTML: addresses in <code> (tap to copy), what is under maintenance, the last few deposits."""
    esc = html.escape
    lines = [f"📥 <b>{coin} 입금 주소</b> (바이낸스 현물 지갑으로 들어옵니다)"]
    first = next((e for e in view if e["ok"]), None)
    for e in view:
        lines.append("")
        if e["ok"]:
            star = " ⭐ 추천" if e is first else ""
            lines.append(f"<b>{esc(e['label'])}</b>{star}")
            lines.append(f"<code>{esc(e['address'])}</code>")
            if e["tag"]:
                lines.append(f"메모(태그): <code>{esc(e['tag'])}</code> — 꼭 함께 넣으세요")
            if e["why"]:
                lines.append(f"⚠️ {esc(e['why'])}")
        else:
            lines.append(f"<b>{esc(e['label'])}</b> — ❌ {esc(e['why'])}")
    if not first:
        lines += ["", "⚠️ 지금은 고른 네트워크 모두 입금을 받지 않습니다. 잠시 뒤 다시 /address 해 주세요."]
    lines += ["",
              "주소를 누르면 복사됩니다. 빗썸 출금 화면에서 <b>같은 네트워크</b>를 고르세요. 네트워크가 다르면 돈을 "
              "잃을 수 있습니다.",
              "처음 보내는 주소면 소액(예: 10 USDT)으로 먼저 보내 보세요. 트래블룰 때문에 받는 사람 이름(영문)이 "
              "바이낸스 계정과 같아야 합니다.",
              "오는 중·도착하면 이 대화로 알려 드립니다(48시간 동안)."]
    if recent:
        lines += ["", "최근 입금:"]
        for r in recent:
            lines.append("· " + esc(deposit_line(r)))
    return "\n".join(lines)


def plain(text):
    """Telegram HTML -> plain text (when a formatted message could not be sent)."""
    import re
    return html.unescape(re.sub(r"</?(b|code)>", "", text))


def deposit_line(r):
    from datetime import datetime, timedelta, timezone
    t = datetime.fromtimestamp(int(r.get("insertTime") or 0) / 1000, timezone(timedelta(hours=9)))
    st = int(r.get("status") if r.get("status") is not None else -1)
    conf = f" ({r['confirmTimes']})" if st == 0 and r.get("confirmTimes") else ""
    return (f"{t:%m/%d %H:%M} {float(r.get('amount') or 0):,.2f} {r.get('coin', COIN)} "
            f"({net_label(str(r.get('network') or ''))}) — {DEP_KO.get(st, f'상태 {st}')}{conf}")


def watch_pass(W, rows, start_ms):
    """Deposits whose status changed since the last look: [(row, kind)], kind 'coming' / 'done' / 'problem'.
    The first look takes what is already credited as known (no notice for old deposits)."""
    seen = W.setdefault("seen", {})
    first = not W.get("looked")
    W["looked"] = True
    out = []
    for r in sorted(rows, key=lambda x: int(x.get("insertTime") or 0)):
        key = str(r.get("id") or r.get("txId") or "")
        if not key:
            continue
        st = int(r.get("status") if r.get("status") is not None else -1)
        prev = seen.get(key)
        seen[key] = st
        if prev == st or (prev in DEP_DONE and st in DEP_DONE):
            continue
        if first and st in DEP_DONE and int(r.get("insertTime") or 0) < start_ms:
            continue
        if st in DEP_DONE:
            out.append((r, "done"))
        elif st == 0:
            if prev is None:
                out.append((r, "coming"))
        else:
            out.append((r, "problem"))
    if len(seen) > 200:
        W["seen"] = dict(list(seen.items())[-100:])
    return out


def watch_text(r, kind):
    amt = f"{float(r.get('amount') or 0):,.2f} {r.get('coin', COIN)}"
    net = net_label(str(r.get("network") or ""))
    if kind == "coming":
        conf = f" (블록 확인 {r['confirmTimes']})" if r.get("confirmTimes") else ""
        return f"📥 {amt} 입금이 오고 있습니다 — {net}{conf}. 도착하면 다시 알려 드립니다."
    if kind == "done":
        where = "펀딩 지갑" if int(r.get("walletType") or 0) == 1 else "현물 지갑"
        text = f"✅ {amt} 입금 완료 — {net}, {where}."
        if where == "펀딩 지갑":
            text += " 봇은 현물·선물 지갑만 씁니다. /balance 에서 현물로 옮길 수 있습니다."
        else:
            text += " 봇이 수익률 계산에서 이 돈을 빼고, 다음 09:05 KST 점검 때 현물·선물로 나눕니다."
        return text
    st = int(r.get("status") if r.get("status") is not None else -1)
    if st == 8:
        return (f"⚠️ {amt} 입금({net})을 바이낸스가 확인해 달라고 합니다(트래블룰 등). 휴대폰 브라우저로 "
                "binance.com 에 로그인해 입금 내역에서 확인해 주세요. 앱을 설치할 필요는 없습니다.")
    return f"⚠️ {amt} 입금({net}) 상태: {DEP_KO.get(st, f'상태 {st}')}. binance.com 입금 내역을 확인해 주세요."


# ---------------------------------------------------------------- balances

def _wallet_kind(name):
    n = str(name or "").lower()
    if n == "spot":
        return "spot"
    if n == "funding":
        return "funding"
    if n == "earn":
        return "earn"
    if "futures" in n and "coin" not in n and "usd" in n:
        return "futures"
    return "other"


def _block(items, prices):
    """[(asset, qty)] -> {value, assets: [(asset, qty, usd or None)]}, largest first. Same asset rows are added."""
    q = {}
    for a, x in items:
        if x > 0:
            q[a] = q.get(a, 0.0) + x
    rows = []
    for a, x in q.items():
        p = 1.0 if a == COIN else prices.get(a + COIN)
        rows.append((a, x, x * p if p else None))
    rows.sort(key=lambda r: -(r[2] if r[2] is not None else -1))
    return {"value": sum(r[2] for r in rows if r[2] is not None), "assets": rows}


def balance_view(spot_api, fut_api):
    """Every wallet of the account, valued in USDT. A wallet that cannot be read is listed in `errors` and left out of
    the total. {spot, futures, earn, funding: block or None, others: [(label, usd)], total, errors: [(what, msg)]}"""
    out = {"spot": None, "futures": None, "earn": None, "funding": None, "others": [], "total": 0.0, "errors": []}

    def guard(what, fn):
        try:
            return fn()
        except (BinanceError, NetworkError) as e:
            out["errors"].append((what, str(e)))
            return None

    prices = guard("시세", spot_api.prices) or {}
    bal = guard("현물", spot_api.balances)
    if bal is not None:
        out["spot"] = _block([(a, b["free"] + b["locked"]) for a, b in bal.items()], prices)
    acc = guard("선물", fut_api.account) if fut_api is not None else None
    if acc is not None:
        wallet = float(acc.get("totalWalletBalance") or 0.0)
        upnl = float(acc.get("totalUnrealizedProfit") or 0.0)
        val = float(acc.get("totalMarginBalance") or (wallet + upnl))
        out["futures"] = {"value": val, "wallet": wallet, "upnl": upnl,
                          "n": len([p for p in acc.get("positions") or [] if float(p.get("positionAmt") or 0)])}
    flex = guard("이자 예치", lambda: spot_api.earn_flexible_position(asset=None))
    locked = guard("이자 예치(고정)", spot_api.earn_locked_position)
    if flex is not None or locked is not None:
        items = [(r.get("asset"), float(r.get("totalAmount") or 0)) for r in flex or []]
        items += [(r.get("asset"), float(r.get("amount") or 0)) for r in locked or []]
        out["earn"] = _block(items, prices)
    fund = guard("펀딩", spot_api.funding_assets)
    if fund is not None:
        out["funding"] = _block([(r.get("asset"), float(r.get("free") or 0) + float(r.get("locked") or 0)
                                  + float(r.get("freeze") or 0)) for r in fund], prices)
        out["funding_usdt"] = sum(float(r.get("free") or 0) for r in fund if r.get("asset") == COIN)
    wb = guard("기타 지갑", lambda: spot_api.wallet_balance(COIN))
    for w in wb or []:
        if _wallet_kind(w.get("walletName")) == "other" and float(w.get("balance") or 0) >= 0.01:
            name = str(w.get("walletName"))
            out["others"].append((OTHER_KO.get(name.lower(), name), float(w["balance"])))
    out["total"] = sum(out[k]["value"] for k in CORE_WALLETS if out[k]) + sum(v for _, v in out["others"])
    return out
