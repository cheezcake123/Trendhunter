"""Monthly report: last month's result, a buy-and-hold BTC yardstick and a futures health check.

Pure functions (standard library only) so the text can be tested without an exchange.
Months follow the bot's trading day, which turns at 00:00 UTC = 09:00 KST.
"""
from __future__ import annotations

from datetime import datetime, timezone

DAY_MS = 86_400_000


def month_start(ts):
    """Start (unix seconds) of the UTC month that contains ts."""
    d = datetime.fromtimestamp(ts, timezone.utc)
    return datetime(d.year, d.month, 1, tzinfo=timezone.utc).timestamp()


def month_label(ts):
    d = datetime.fromtimestamp(ts, timezone.utc)
    return f"{d.year}년 {d.month}월"


def prev_month(ts):
    """(start, end, label) of the month before the one that contains ts."""
    end = month_start(ts)
    start = month_start(end - 1)
    return start, end, month_label(start)


def _day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _day_ms(day):
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def _usd(x, signed=True):
    if not signed:
        return f"${x:,.2f}"
    sign = "+" if x > 0.004 else ("-" if x < -0.004 else "")
    return f"{sign}${abs(x):,.2f}"


def futures_stats(trades):
    """Count, P&L, win rate, profit factor (None = no losing trade) and average R of journal rows."""
    n = len(trades)
    if not n:
        return None
    pnl = sum(float(t["pnl"]) for t in trades)
    gw = sum(float(t["pnl"]) for t in trades if t["pnl"] > 0)
    gl = -sum(float(t["pnl"]) for t in trades if t["pnl"] < 0)
    return {"n": n, "pnl": pnl, "win": sum(1 for t in trades if t["pnl"] > 0) / n * 100,
            "pf": gw / gl if gl > 0 else None, "avg_r": sum(float(t.get("r") or 0) for t in trades) / n,
            "fee": sum(float(t.get("fee") or 0) for t in trades),
            "funding": sum(float(t.get("funding") or 0) for t in trades)}


def equity_window(hist, start, end):
    """First and last daily total-equity rows inside [start, end]. hist rows: [utc_day, spot, futures] and,
    since v2.5, a 4th value: the unit price of the account (total equity with deposits/withdrawals taken out)."""
    d0, d1 = _day(start), _day(end)
    rows = [h for h in hist if d0 <= h[0] <= d1]
    if len(rows) < 2:
        return None
    return rows[0], rows[-1]


def btc_change(rows, day0, day1):
    """BTC change between the opens of two UTC days, from daily kline rows; None if a day is missing."""
    opens = {int(k[0]): float(k[1]) for k in rows or []}
    a, b = opens.get(_day_ms(day0)), opens.get(_day_ms(day1))
    if not a or not b:
        return None
    return (b / a - 1) * 100


def health_line(recent):
    """Plain-language check of the latest futures trades (recent = newest journal rows of this mode)."""
    st = futures_stats(recent)
    if not st or st["n"] < 10:
        return None
    if st["pf"] is None:
        return f"🩺 선물 점검: 최근 {st['n']}건 손실 거래 없음 — 정상"
    if st["pf"] >= 1.0:
        return f"🩺 선물 점검: 최근 {st['n']}건 수익팩터 {st['pf']:.2f} — 정상 범위"
    if st["n"] < 20:
        return f"🩺 선물 점검: 최근 {st['n']}건 수익팩터 {st['pf']:.2f} — 아직 표본이 적어 판단을 미룹니다"
    return (f"🩺 선물 점검: 최근 {st['n']}건 수익팩터 {st['pf']:.2f} (1 미만 = 손실 구간). "
            "추세추종은 이런 구간을 자주 겪습니다. 위험을 낮추려면 /preset 안정 또는 /risk 2")


def window_flows(flows, day0, day1):
    """Net outside money (deposits - withdrawals) booked after the day0 row and up to the day1 row.
    A day's row is written a few minutes after 00:00 UTC, so the cut is 10 minutes into each day."""
    lo, hi = _day_ms(day0) / 1000 + 600, _day_ms(day1) / 1000 + 600
    return sum(float(f.get("net") or 0.0) for f in flows or [] if lo < float(f.get("ts") or 0) <= hi)


def monthly_text(label, mode_ko, hist, start, end, trades, recent, spot_fills, btc_rows=None, flows=None):
    lines = [f"📅 월간 리포트 — {label} (매일 09시 KST 기준) | {mode_ko}"]
    win = equity_window(hist, start, end)
    mixed = True  # True while the total-equity change may still contain deposits / withdrawals
    if win:
        r0, r1 = win
        d0, d1 = r0[0], r1[0]
        t0, t1 = r0[1] + r0[2], r1[1] + r1[2]
        nav0 = r0[3] if len(r0) > 3 else None
        nav1 = r1[3] if len(r1) > 3 else None
        since = "" if d0 == _day(start) else f" [{d0[5:].replace('-', '/')}부터]"
        if nav0 and nav1:
            mixed = False
            net = window_flows(flows, d0, d1)
            lines.append(f"총자산 {_usd(t0, False)} → {_usd(t1, False)} | 수익률 {(nav1 / nav0 - 1) * 100:+.1f}%{since}")
            if abs(net) >= 0.005:
                lines.append(f"  (이 기간 순입금 {_usd(net)} — 수익률은 입금·출금을 빼고 계산했습니다)")
        else:
            chg = f" ({(t1 / t0 - 1) * 100:+.1f}%)" if t0 > 0 else ""
            lines.append(f"총자산 {_usd(t0, False)} → {_usd(t1, False)}{chg}{since}")
        b = btc_change(btc_rows, d0, d1)
        if b is not None:
            lines.append(f"같은 기간 BTC 보유: {b:+.1f}%")
    else:
        lines.append("총자산 기록이 아직 부족합니다(하루 1번 기록).")
    st = futures_stats(trades)
    if st:
        pf = "-" if st["pf"] is None else f"{st['pf']:.2f}"
        lines.append(f"[선물] 거래 {st['n']}건 · 승률 {st['win']:.0f}% · 실현손익 {_usd(st['pnl'])} · "
                     f"평균 {st['avg_r']:+.2f}R · 수익팩터 {pf}")
        lines.append(f"  (수수료 {_usd(st['fee'], False)} · 펀딩 {_usd(st['funding'])} 포함)")
    else:
        lines.append("[선물] 청산된 거래 없음")
    lines.append(f"[현물] 리밸런싱 체결 {spot_fills}건")
    h = health_line(recent)
    if h:
        lines.append(h)
    if mixed:
        lines.append("※ 총자산 변화에는 입금·출금도 섞일 수 있습니다. 선물 실현손익은 입출금과 무관한 매매 결과입니다.")
    return "\n".join(lines)
