"""Pure-Python technical indicators (no numpy/pandas needed).

All functions take plain lists and return lists of the same length.
Positions where an indicator is not yet defined hold None.
"""
from __future__ import annotations


def sma(values, n):
    out = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    s = sum(values[:n])
    out[n - 1] = s / n
    for i in range(n, len(values)):
        s += values[i] - values[i - n]
        out[i] = s / n
    return out


def ema(values, n):
    """EMA seeded with the SMA of the first n values (TA-Lib style)."""
    out = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    k = 2.0 / (n + 1)
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def moving_average(values, n, kind="EMA"):
    return sma(values, n) if str(kind).upper() == "SMA" else ema(values, n)


def true_range(high, low, close):
    tr = [None] * len(close)
    for i in range(len(close)):
        if i == 0:
            tr[i] = high[i] - low[i]
        else:
            pc = close[i - 1]
            tr[i] = max(high[i] - low[i], abs(high[i] - pc), abs(low[i] - pc))
    return tr


def wilder(values, n, start=0):
    """Wilder smoothing (RMA). Seeds with the mean of values[start:start+n]."""
    out = [None] * len(values)
    if n <= 0 or len(values) < start + n:
        return out
    prev = sum(values[start:start + n]) / n
    out[start + n - 1] = prev
    for i in range(start + n, len(values)):
        prev = (prev * (n - 1) + values[i]) / n
        out[i] = prev
    return out


def atr(high, low, close, n=14):
    return wilder(true_range(high, low, close), n)


def rsi(close, n=14):
    out = [None] * len(close)
    if len(close) <= n:
        return out
    gains = [0.0] * len(close)
    losses = [0.0] * len(close)
    for i in range(1, len(close)):
        d = close[i] - close[i - 1]
        gains[i] = d if d > 0 else 0.0
        losses[i] = -d if d < 0 else 0.0
    ag = sum(gains[1:n + 1]) / n
    al = sum(losses[1:n + 1]) / n
    out[n] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    for i in range(n + 1, len(close)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    return out


def adx(high, low, close, n=14):
    """Wilder's ADX. First value appears at index 2n-1."""
    size = len(close)
    out = [None] * size
    if size < 2 * n:
        return out
    pdm = [0.0] * size
    mdm = [0.0] * size
    tr = true_range(high, low, close)
    for i in range(1, size):
        up = high[i] - high[i - 1]
        down = low[i - 1] - low[i]
        pdm[i] = up if (up > down and up > 0) else 0.0
        mdm[i] = down if (down > up and down > 0) else 0.0
    # Wilder running sums seeded over bars 1..n
    s_tr = sum(tr[1:n + 1])
    s_p = sum(pdm[1:n + 1])
    s_m = sum(mdm[1:n + 1])
    dx = [None] * size

    def _dx(st, sp, sm):
        if st <= 0:
            return 0.0
        pdi = 100.0 * sp / st
        mdi = 100.0 * sm / st
        tot = pdi + mdi
        return 0.0 if tot == 0 else 100.0 * abs(pdi - mdi) / tot

    dx[n] = _dx(s_tr, s_p, s_m)
    for i in range(n + 1, size):
        s_tr = s_tr - s_tr / n + tr[i]
        s_p = s_p - s_p / n + pdm[i]
        s_m = s_m - s_m / n + mdm[i]
        dx[i] = _dx(s_tr, s_p, s_m)
    first = 2 * n - 1
    prev = sum(dx[n:first + 1]) / n
    out[first] = prev
    for i in range(first + 1, size):
        prev = (prev * (n - 1) + dx[i]) / n
        out[i] = prev
    return out


def donchian(high, low, n=20):
    """Highest high / lowest low of the PREVIOUS n bars (bar i itself excluded)."""
    size = len(high)
    hi = [None] * size
    lo = [None] * size
    for i in range(n, size):
        hi[i] = max(high[i - n:i])
        lo[i] = min(low[i - n:i])
    return hi, lo
