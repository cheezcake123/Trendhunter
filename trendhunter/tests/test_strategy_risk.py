"""Unit tests for signals, stops, sizing and guards."""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import risk as R  # noqa: E402
import strategy as S  # noqa: E402

with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
    CFG = json.load(fh)


def candles(prices, start=1_700_000_000_000, tf=3_600_000, wick=0.003):
    rows = []
    prev = prices[0]
    for i, p in enumerate(prices):
        o = prev
        hi = max(o, p) * (1 + wick)
        lo = min(o, p) * (1 - wick)
        rows.append([start + i * tf, o, hi, lo, p, 1.0, start + (i + 1) * tf - 1])
        prev = p
    return S.candles_from_klines(rows)


class SignalTests(unittest.TestCase):
    def test_regime_alignment(self):
        up = [100 * (1.004 ** i) for i in range(400)]
        r4 = S.prepare_4h(candles(up, tf=14_400_000), CFG)
        self.assertEqual(r4["regime"][-1], S.LONG)
        down = [100 * (0.996 ** i) for i in range(400)]
        self.assertEqual(S.prepare_4h(candles(down, tf=14_400_000), CFG)["regime"][-1], S.SHORT)
        # up-trend that rolls over: fast MA crosses below mid while slower MAs still point up -> NEUTRAL phase
        v = up[:300] + [up[299] * (0.995 ** k) for k in range(1, 60)]
        reg = S.prepare_4h(candles(v, tf=14_400_000), CFG)["regime"]
        self.assertEqual(reg[299], S.LONG)
        self.assertIn(S.NEUTRAL, reg[300:])

    def test_breakout_long_and_filters(self):
        import math
        prices = [100 * (1.0015 ** i) + 1.5 * math.sin(i * 0.5) for i in range(400)]
        ind = S.prepare_1h(candles(prices, wick=0.001), CFG)
        longs = [i for i in range(250, 400) if S.evaluate(ind, i, S.LONG, CFG)["side"] == S.LONG]
        self.assertTrue(longs, "an up-trend with pullbacks must produce long breakouts")
        for i in longs:
            self.assertGreater(ind["c"][i], ind["dch"][i])
            self.assertIsNone(S.evaluate(ind, i, S.NEUTRAL, CFG)["side"])
            self.assertIsNone(S.evaluate(ind, i, S.SHORT, CFG)["side"])
        # a huge spike candle is rejected (anti-chase)
        spike = prices[:longs[0]] + [prices[longs[0] - 1] * 1.08]
        ind2 = S.prepare_1h(candles(spike, wick=0.001), CFG)
        self.assertIn(S.evaluate(ind2, len(spike) - 1, S.LONG, CFG)["code"], ("CHASE", "SPIKE", "OVERHEATED"))
        # mirror image produces shorts in a SHORT regime
        mirror = [10000.0 / x for x in prices]
        ind3 = S.prepare_1h(candles(mirror, wick=0.001), CFG)
        both = dict(CFG, allow_short=True)
        shorts = [i for i in range(250, 400) if S.evaluate(ind3, i, S.SHORT, both)["side"] == S.SHORT]
        self.assertTrue(shorts)
        # default config is long-only: the same short setups are switched off
        self.assertEqual(S.evaluate(ind3, shorts[0], S.SHORT, CFG)["code"], "DIRECTION_OFF")

    def test_regime_at_uses_closed_candle(self):
        r4 = {"tc": [10, 20, 30], "regime": [S.SHORT, S.NEUTRAL, S.LONG]}
        self.assertEqual(S.regime_at(r4, 25), S.NEUTRAL)
        self.assertEqual(S.regime_at(r4, 30), S.LONG)
        self.assertEqual(S.regime_at(r4, 5), S.NEUTRAL)

    def test_stops_and_trailing(self):
        CFG_ = dict(CFG, trail_atr_mult=3.0, max_stop_pct=6.0)
        stop, dist = S.initial_stop(S.LONG, 100.0, 1.0, CFG_)
        self.assertAlmostEqual(stop, 98.0)
        stop, dist = S.initial_stop(S.LONG, 100.0, 0.1, CFG_)  # min stop 0.8%
        self.assertAlmostEqual(dist, 0.8)
        stop, dist = S.initial_stop(S.SHORT, 100.0, 10.0, CFG_)  # max stop 6%
        self.assertAlmostEqual(stop, 106.0)
        # trailing never loosens; breakeven lock after 1.5R
        s = S.trail_stop(S.LONG, 100, 2.0, 98.0, 101.0, 1.0, CFG_)
        self.assertEqual(s, 98.0)
        s = S.trail_stop(S.LONG, 100, 2.0, 98.0, 103.0, 1.0, CFG_)  # mfe 1.5R -> lock entry+0.2R
        self.assertAlmostEqual(s, 100.4)
        s = S.trail_stop(S.LONG, 100, 2.0, 100.4, 110.0, 1.0, CFG_)  # chandelier 110-3
        self.assertAlmostEqual(s, 107.0)
        s = S.trail_stop(S.SHORT, 100, 2.0, 102.0, 90.0, 1.0, CFG_)
        self.assertAlmostEqual(s, 93.0)

    def test_time_stop(self):
        h = 3_600_000
        p = dict(CFG, time_stop_hours=30)
        self.assertFalse(S.time_stop_due(0, 10 * h, S.LONG, 100, 100.5, 2, p))
        self.assertTrue(S.time_stop_due(0, 31 * h, S.LONG, 100, 100.5, 2, p))
        self.assertFalse(S.time_stop_due(0, 31 * h, S.LONG, 100, 103.0, 2, p))
        self.assertFalse(S.time_stop_due(0, 999 * h, S.LONG, 100, 100.0, 2, dict(CFG, time_stop_hours=0)))


class SizingTests(unittest.TestCase):
    F = {"tick": "0.01", "step": "0.001", "min_qty": 0.001, "max_qty": 1000, "min_notional": 5}

    def test_risk_amount(self):
        sz = R.size_position(100, 100, 100.0, 2.0, CFG, self.F)
        self.assertTrue(sz["ok"])
        self.assertAlmostEqual(sz["risk_amt"], 3.0, places=2)
        self.assertLessEqual(sz["margin"], 30.0 + 1e-9)
        self.assertEqual(sz["lev"], 5)

    def test_tight_stop_caps_by_margin_and_leverage(self):
        sz = R.size_position(100, 100, 100.0, 0.2, CFG, self.F)  # wants $1500 notional
        self.assertTrue(sz["ok"])
        self.assertTrue(sz["capped"])
        self.assertLessEqual(sz["lev"], CFG["max_leverage"])
        self.assertLess(sz["risk_amt"], 3.0)

    def test_too_small(self):
        f = dict(self.F, step="1", min_qty=1, min_notional=100)
        sz = R.size_position(100, 100, 100.0, 50.0, CFG, f)
        self.assertFalse(sz["ok"])
        self.assertEqual(sz["code"], "SIZE_TOO_SMALL")

    def test_bump_to_min_qty_within_tolerance(self):
        f = dict(self.F, step="0.001", min_qty=0.001, min_notional=100)
        sz = R.size_position(100, 100, 100000.0, 1500.0, CFG, f)  # BTC-like: wants 0.002
        self.assertTrue(sz["ok"])
        self.assertAlmostEqual(sz["qty"], 0.002)

    def test_liquidation_cap(self):
        sz = R.size_position(1000, 1000, 100.0, 6.0, dict(CFG, max_leverage=20), self.F)
        self.assertTrue(sz["ok"])
        self.assertLessEqual(sz["lev"], 11)

    def test_rounding_helpers(self):
        self.assertEqual(R.fmt_step(0.123456, "0.001"), "0.123")
        self.assertEqual(R.fmt_step(12.0, "1"), "12")
        self.assertEqual(R.floor_step(0.0019, "0.001"), 0.001)
        self.assertEqual(R.ceil_step(0.0011, "0.001"), 0.002)
        self.assertEqual(R.fmt_step(65000.04, "0.10"), "65000.0")


class GuardTests(unittest.TestCase):
    def test_guards(self):
        now = 1_700_000_000
        L = R.new_ledger(100.0, now)
        pos = {}
        self.assertEqual(R.can_enter(L, pos, "BTCUSDT", S.LONG, now, 100, CFG), (True, "OK"))
        self.assertEqual(R.can_enter(L, pos, "BTCUSDT", S.LONG, now, 40, CFG)[1], "EQUITY_FLOOR")
        self.assertEqual(R.can_enter(L, pos, "BTCUSDT", S.LONG, now, 100, CFG, paused=True)[1], "PAUSED")
        pos = {"A": {"side": S.LONG}, "B": {"side": S.LONG}}
        self.assertEqual(R.can_enter(L, pos, "C", S.LONG, now, 100, CFG)[1], "SAME_DIRECTION")
        self.assertEqual(R.can_enter(L, pos, "C", S.SHORT, now, 100, CFG), (True, "OK"))
        pos["C"] = {"side": S.SHORT}
        self.assertEqual(R.can_enter(L, pos, "D", S.SHORT, now, 100, CFG)[1], "MAX_POSITIONS")
        # loss -> cooldown on that symbol, streak pause after 3, daily limit at -8%
        ev = R.on_exit(L, "X", -3.0, now, CFG)
        self.assertEqual(ev, [])
        self.assertEqual(R.can_enter(L, {}, "X", S.LONG, now + 60, 100, CFG)[1], "COOLDOWN")
        R.on_exit(L, "Y", -3.0, now, CFG)
        ev = R.on_exit(L, "Z", -3.0, now, CFG)
        self.assertIn("STREAK_PAUSE", ev)
        self.assertIn("DAILY_LIMIT", ev)
        self.assertEqual(R.can_enter(L, {}, "Q", S.LONG, now + 60, 91, CFG)[1], "DAILY_LIMIT")
        # next trading day resets the daily limit but the streak pause (8h) may still apply
        R.roll_day(L, now + 86400, 91)
        self.assertEqual(R.can_enter(L, {}, "Q", S.LONG, now + 86400, 91, CFG), (True, "OK"))

    def test_entries_per_day(self):
        now = 1_700_000_000
        L = R.new_ledger(100.0, now)
        for _ in range(CFG["max_entries_per_day"]):
            R.on_entry(L, now)
        self.assertEqual(R.can_enter(L, {}, "A", S.LONG, now, 100, CFG)[1], "MAX_ENTRIES")


if __name__ == "__main__":
    unittest.main()
