"""Cooling-off for settings (v2.8): a change that raises the risk only takes effect after cool_off_hours.

Changes that lower the risk apply at once. The owner asked for this to keep impulsive decisions (bigger bets after a
loss, more leverage, switching guards off) away from the money. Every risky change can be cancelled until it is due.
"""
from __future__ import annotations

# key: +1 when a LARGER value is riskier, -1 when a SMALLER value is riskier (booleans: True > False)
RISK_DIR = {
    "risk_per_trade_pct": +1,
    "max_open_positions": +1,
    "max_same_direction": +1,
    "max_entries_per_day": +1,
    "daily_loss_limit_pct": +1,       # a looser daily limit
    "max_leverage": +1,
    "equity_floor_pct": -1,           # a lower floor
    "loss_streak_limit": +1,
    "loss_streak_pause_hours": -1,
    "cooldown_after_loss_hours": -1,
    "pyramid_max_adds": +1,
    "pyramid_step_r": -1,             # adds come sooner
    "pyramid_risk_frac": +1,
    "allow_long": +1,
    "allow_short": +1,
    "adx_min": -1,                    # weaker trends let in
    "alloc_spot_pct": -1,             # more money on the leveraged side
    "spot_vol_target": +1,
    "spot_btc_gate": -1,              # gate switched off
    "spot_btc_gate_min": -1,
    "cool_off_hours": -1,             # the waiting time itself
}
REBASE = "__rebase__"  # /rebase that lowers the equity floor


def riskier(key, old, new):
    """True when changing `key` from `old` to `new` raises the risk (so it has to wait)."""
    if key == "funding_max_pct":  # 0 switches the filter off: the loosest value of all
        o, n = float(old or 0), float(new or 0)
        if n == o:
            return False
        return n == 0 or (o > 0 and n > o)
    if key == "symbols":  # a new coin to trade; dropping coins is never riskier
        return bool(set(new or []) - set(old or []))
    d = RISK_DIR.get(key)
    if d is None or old is None or new is None:
        return False
    try:
        o, n = float(old), float(new)
    except (TypeError, ValueError):
        return False
    return (n - o) * d > 1e-12


def show(val):
    if isinstance(val, bool):
        return "켜기" if val else "끄기"
    if isinstance(val, list):
        return ",".join(v[:-4] if str(v).endswith("USDT") else str(v) for v in val)
    if isinstance(val, float) and val == int(val):
        return f"{int(val)}"
    return f"{val}"
