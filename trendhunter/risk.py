"""Position sizing, exchange rounding and the entry guards (shared by live bot and backtester)."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

# ---------------------------------------------------------------- rounding

def _d(x):
    return x if isinstance(x, Decimal) else Decimal(str(x))


def floor_step(x, step):
    step = _d(step)
    if step <= 0:
        return float(x)
    return float((_d(x) / step).to_integral_value(rounding=ROUND_FLOOR) * step)


def ceil_step(x, step):
    step = _d(step)
    if step <= 0:
        return float(x)
    return float((_d(x) / step).to_integral_value(rounding=ROUND_CEILING) * step)


def round_step(x, step):
    step = _d(step)
    if step <= 0:
        return float(x)
    return float((_d(x) / step).to_integral_value(rounding=ROUND_HALF_UP) * step)


def fmt_step(x, step):
    """String for the API: x snapped to step, with exactly the step's decimals, no exponent."""
    step = _d(step).normalize()
    exp = -step.as_tuple().exponent if step.as_tuple().exponent < 0 else 0
    q = Decimal(1).scaleb(-exp)
    return format(_d(x).quantize(q, rounding=ROUND_HALF_UP), "f")


DEFAULT_FILTER = {"tick": 0.0001, "step": 0.001, "min_qty": 0.001, "max_qty": 1e9, "min_notional": 5.0}

# ---------------------------------------------------------------- sizing

def size_position(equity, available, entry, stop_dist, p, filt=None):
    """Risk-based sizing.

    qty = (equity x risk%) / stop distance. Margin per trade is capped at
    max_margin_per_trade_pct of equity; leverage is the smallest whole number
    that fits the position into that margin, capped at max_leverage
    (if it does not fit, the position is shrunk instead).
    """
    f = dict(DEFAULT_FILTER)
    if filt:
        f.update(filt)
    out = {"ok": False, "code": None, "qty": 0.0, "lev": 1, "notional": 0.0,
           "margin": 0.0, "risk_amt": 0.0, "target_risk": 0.0, "capped": False}
    if equity <= 0 or entry <= 0 or stop_dist <= 0:
        out["code"] = "BAD_INPUT"
        return out
    target_risk = equity * float(p["risk_per_trade_pct"]) / 100.0
    out["target_risk"] = target_risk
    margin_cap = min(equity * float(p["max_margin_per_trade_pct"]) / 100.0, max(available, 0.0) * 0.95)
    if margin_cap <= 0:
        out["code"] = "NO_MARGIN"
        return out
    max_lev = max(1, int(p["max_leverage"]))
    # keep the isolated-margin liquidation price well beyond the stop
    stop_pct = stop_dist / entry * 100.0
    liq_cap = int(1.0 / (stop_pct * 1.3 / 100.0 + 0.01))
    max_lev = max(1, min(max_lev, liq_cap))
    qty = target_risk / stop_dist
    notional = qty * entry
    lev = max(1, math.ceil(notional / margin_cap - 1e-9))
    if lev > max_lev:
        lev = max_lev
        notional = margin_cap * lev
        qty = notional / entry
        out["capped"] = True
    qty = floor_step(qty, f["step"])
    min_qty_needed = max(float(f["min_qty"]), float(f["min_notional"]) * 1.01 / entry)
    if qty < min_qty_needed:
        bumped = ceil_step(min_qty_needed, f["step"])
        # accept a bump only if the risk stays within 1.5x of target and margin still fits
        if bumped * stop_dist <= target_risk * 1.5 and bumped * entry <= margin_cap * max_lev:
            qty = bumped
            lev = max(lev, max(1, math.ceil(bumped * entry / margin_cap - 1e-9)))
        else:
            out["code"] = "SIZE_TOO_SMALL"
            return out
    if qty > float(f["max_qty"]):
        qty = floor_step(float(f["max_qty"]), f["step"])
    notional = qty * entry
    lev = max(lev, math.ceil(notional / margin_cap - 1e-9))
    if lev > max_lev:
        out["code"] = "NO_MARGIN"
        return out
    out.update(ok=True, code="OK", qty=qty, lev=int(lev), notional=notional,
               margin=notional / lev, risk_amt=qty * stop_dist)
    return out


def liquidation_buffer_ok(lev, stop_pct, mmr=0.01):
    """Rough isolated-margin check: liquidation must sit well beyond the stop."""
    liq_pct = (1.0 / lev - mmr) * 100.0
    return liq_pct > stop_pct * 1.3


# ---------------------------------------------------------------- ledger & guards

def trading_day(ts):
    """Trading day changes at 00:00 UTC = 09:00 KST."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def new_ledger(equity, now):
    return {
        "day": trading_day(now), "day_start_equity": equity, "day_realized": 0.0,
        "day_entries": 0, "day_limit_hit": False, "loss_streak": 0, "pause_until": 0.0,
        "cooldown": {}, "start_equity": equity, "peak_equity": equity,
    }


def roll_day(L, now, equity):
    d = trading_day(now)
    if L.get("day") != d:
        L.update(day=d, day_start_equity=equity, day_realized=0.0, day_entries=0, day_limit_hit=False)
        return True
    return False


def on_entry(L, now):
    L["day_entries"] = int(L.get("day_entries", 0)) + 1


def on_exit(L, sym, pnl, now, p):
    """Book a closed trade. Returns a list of guard events that fired."""
    events = []
    L["day_realized"] = float(L.get("day_realized", 0.0)) + pnl
    if pnl < 0:
        L["loss_streak"] = int(L.get("loss_streak", 0)) + 1
        L.setdefault("cooldown", {})[sym] = now + float(p["cooldown_after_loss_hours"]) * 3600
        if L["loss_streak"] >= int(p["loss_streak_limit"]):
            L["pause_until"] = now + float(p["loss_streak_pause_hours"]) * 3600
            L["loss_streak"] = 0
            events.append("STREAK_PAUSE")
    else:
        L["loss_streak"] = 0
    limit = float(L.get("day_start_equity", 0.0)) * float(p["daily_loss_limit_pct"]) / 100.0
    if limit > 0 and L["day_realized"] <= -limit and not L.get("day_limit_hit"):
        L["day_limit_hit"] = True
        events.append("DAILY_LIMIT")
    return events


GUARD_KO = {
    "OK": "통과",
    "BLOCKED": "계정 설정 문제로 매매 차단",
    "PAUSED": "사용자가 일시정지함",
    "EQUITY_FLOOR": "자본 바닥선 도달 — 신규 진입 중단",
    "DAILY_LIMIT": "일일 손실 한도 도달",
    "STREAK_PAUSE": "연속 손실로 휴식 중",
    "MAX_ENTRIES": "하루 진입 횟수 한도",
    "IN_POSITION": "이미 포지션 보유",
    "MAX_POSITIONS": "동시 보유 한도",
    "SAME_DIRECTION": "같은 방향 포지션 한도",
    "COOLDOWN": "손절 직후 재진입 금지 시간",
    "SIZE_TOO_SMALL": "주문 최소 수량 미달",
    "NO_MARGIN": "증거금 부족",
    "BAD_INPUT": "입력값 오류",
    "LIQ_TOO_CLOSE": "청산가가 손절가에 너무 가까움",
}


def can_enter(L, positions, sym, side, now, equity, p, paused=False, blocked=False):
    if blocked:
        return False, "BLOCKED"
    if paused:
        return False, "PAUSED"
    floor = float(L.get("start_equity", 0.0)) * float(p["equity_floor_pct"]) / 100.0
    if equity < floor:
        return False, "EQUITY_FLOOR"
    if L.get("day_limit_hit"):
        return False, "DAILY_LIMIT"
    if now < float(L.get("pause_until", 0.0)):
        return False, "STREAK_PAUSE"
    if int(L.get("day_entries", 0)) >= int(p["max_entries_per_day"]):
        return False, "MAX_ENTRIES"
    if sym in positions:
        return False, "IN_POSITION"
    if len(positions) >= int(p["max_open_positions"]):
        return False, "MAX_POSITIONS"
    same = sum(1 for x in positions.values() if x.get("side") == side)
    if same >= int(p["max_same_direction"]):
        return False, "SAME_DIRECTION"
    if now < float(L.get("cooldown", {}).get(sym, 0.0)):
        return False, "COOLDOWN"
    return True, "OK"
