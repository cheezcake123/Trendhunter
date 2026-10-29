"""Indicators vs. independent pandas implementations (pandas only needed for this test)."""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import indicators as I  # noqa: E402

try:
    import pandas as pd
except ImportError:  # the bot itself does not need pandas
    pd = None


def series(n=600, seed=1):
    rnd = random.Random(seed)
    c, h, l = [], [], []
    p = 100.0
    for _ in range(n):
        p *= 1 + rnd.gauss(0.0003, 0.01)
        hi = p * (1 + abs(rnd.gauss(0, 0.004)))
        lo = p * (1 - abs(rnd.gauss(0, 0.004)))
        c.append(p)
        h.append(hi)
        l.append(lo)
    return h, l, c


@unittest.skipIf(pd is None, "pandas not installed")
class IndicatorTests(unittest.TestCase):
    def setUp(self):
        self.h, self.l, self.c = series()
        self.df = pd.DataFrame({"h": self.h, "l": self.l, "c": self.c})

    def close_tail(self, mine, ref, tail=200, tol=1e-6):
        for a, b in zip(mine[-tail:], list(ref)[-tail:]):
            self.assertAlmostEqual(a, b, delta=tol * max(1.0, abs(b)))

    def test_sma_exact(self):
        self.close_tail(I.sma(self.c, 20), self.df.c.rolling(20).mean(), tail=500, tol=1e-9)

    def test_ema_converges(self):
        self.close_tail(I.ema(self.c, 20), self.df.c.ewm(span=20, adjust=False).mean())

    def test_atr_wilder(self):
        pc = self.df.c.shift(1)
        tr = pd.concat([self.df.h - self.df.l, (self.df.h - pc).abs(), (self.df.l - pc).abs()], axis=1).max(axis=1)
        tr.iloc[0] = self.df.h.iloc[0] - self.df.l.iloc[0]
        self.close_tail(I.atr(self.h, self.l, self.c, 14), tr.ewm(alpha=1 / 14, adjust=False).mean())

    def test_rsi_wilder(self):
        d = self.df.c.diff()
        up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
        dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        ref = 100 - 100 / (1 + up / dn)
        self.close_tail(I.rsi(self.c, 14), ref, tol=1e-5)

    def test_adx_wilder(self):
        h, l, c = self.df.h, self.df.l, self.df.c
        up = h.diff()
        dn = -l.diff()
        pdm = ((up > dn) & (up > 0)) * up
        mdm = ((dn > up) & (dn > 0)) * dn
        pc = c.shift(1)
        tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        a = 1 / 14
        atr = tr.ewm(alpha=a, adjust=False).mean()
        pdi = 100 * pdm.ewm(alpha=a, adjust=False).mean() / atr
        mdi = 100 * mdm.ewm(alpha=a, adjust=False).mean() / atr
        dx = 100 * (pdi - mdi).abs() / (pdi + mdi)
        ref = dx.ewm(alpha=a, adjust=False).mean()
        self.close_tail(I.adx(self.h, self.l, self.c, 14), ref, tail=150, tol=1e-4)

    def test_donchian_excludes_current_bar(self):
        hi, lo = I.donchian(self.h, self.l, 20)
        ref_hi = self.df.h.rolling(20).max().shift(1)
        ref_lo = self.df.l.rolling(20).min().shift(1)
        self.close_tail(hi, ref_hi, tail=500, tol=1e-12)
        self.close_tail(lo, ref_lo, tail=500, tol=1e-12)
        self.assertIsNone(hi[19])
        self.assertEqual(hi[20], max(self.h[0:20]))


if __name__ == "__main__":
    unittest.main()
