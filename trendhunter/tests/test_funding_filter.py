"""v2.6: funding filter — no new entry on a crowded side (strategy.funding_blocks).

The rule lives in strategy.py; the backtest and the live bot both call it with the funding rows settled by the
signal candle's close. A funding history that cannot be read never blocks an entry."""
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import backtest  # noqa: E402
import backtest_mix  # noqa: E402
import bot as B  # noqa: E402
import strategy as S  # noqa: E402
from fake_binance import FakeExchange, SimClock, make_api  # noqa: E402
from test_live_flow import START, make_trader  # noqa: E402

H = 3_600_000
with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
    CFG = json.load(fh)


class Rule(unittest.TestCase):
    def test_average_per_eight_hours(self):
        t = 1_000 * 8 * H
        times = [t - k * 8 * H for k in range(12, -1, -1)]  # every 8 h up to and including t
        rates = [0.0001] * len(times)
        rates[-1] = 0.0010
        avg = S.funding_avg_8h(times, rates, t)
        # rows in (t - 72 h, t]: nine rows, the newest one at t itself
        self.assertAlmostEqual(avg, (8 * 0.0001 + 0.0010) / 9)
        self.assertAlmostEqual(S.funding_avg_8h(times, rates, t - 1), 0.0001, msg="a row after t is not known yet")
        self.assertIsNone(S.funding_avg_8h(times, rates, times[0] - 1))
        self.assertIsNone(S.funding_avg_8h([], [], t))

    def test_shorter_funding_intervals_count_per_eight_hours(self):
        t = 1_000 * 8 * H
        times = [t - k * 4 * H for k in range(17, -1, -1)]  # every 4 h: 18 rows in 72 h
        avg = S.funding_avg_8h(times, [0.0002] * len(times), t)
        self.assertAlmostEqual(avg, 0.0004, msg="0.02 % every 4 h is 0.04 % per 8 h")

    def test_which_side_is_crowded(self):
        p = {"funding_max_pct": 0.04}
        self.assertTrue(S.funding_blocks(S.LONG, 0.00041, p))
        self.assertFalse(S.funding_blocks(S.LONG, 0.0004, p))
        self.assertFalse(S.funding_blocks(S.LONG, -0.01, p))
        self.assertTrue(S.funding_blocks(S.SHORT, -0.00041, p))
        self.assertFalse(S.funding_blocks(S.SHORT, 0.01, p))
        self.assertFalse(S.funding_blocks(S.LONG, None, p), "an unknown rate never blocks")
        self.assertFalse(S.funding_blocks(S.LONG, 0.01, {"funding_max_pct": 0}), "0 switches it off")
        self.assertFalse(S.funding_blocks(S.LONG, 0.01, {}))


class Backtest(unittest.TestCase):
    def run_bt(self, rate, **over):
        clock = SimClock(START)
        fake = FakeExchange(clock, direction=1, history_days=420, test_days=1)
        fake.funding_rate = rate
        cfg = dict(CFG, symbols=list(fake.symbols), **over)
        data, start = backtest.load_data(make_api(fake, clock), cfg, 120, None, int(START * 1000))
        return backtest.simulate(cfg, backtest.precompute(cfg, data), start, 3.0, 100.0), cfg

    def test_crowded_longs_are_not_entered(self):
        calm, _ = self.run_bt(0.0001)
        self.assertTrue(calm["trades"], "the fake uptrend gives long trades")
        self.assertNotIn("FUNDING", calm["skips"])
        hot, cfg = self.run_bt(0.001)  # 0.1 % every 8 h
        self.assertEqual(hot["trades"], [])
        self.assertGreater(hot["skips"]["FUNDING"], 0)
        self.assertIn("건너뛴 신호", backtest.funding_note(hot, cfg))
        off, _ = self.run_bt(0.001, funding_max_pct=0.0)
        self.assertTrue(off["trades"], "switched off: traded as before")
        self.assertEqual(backtest.funding_note(off, dict(cfg, funding_max_pct=0.0)), "")

    def test_without_a_funding_history_nothing_is_filtered(self):
        res, cfg = self.run_bt(None)  # the fake answers 404: the backtest charges a flat guess
        self.assertEqual(res["funding_mode"], "flat")
        self.assertTrue(res["trades"])
        self.assertEqual(backtest.funding_note(res, cfg), "")

    def test_reports_show_the_filter(self):
        hot, cfg = self.run_bt(0.001)
        text = backtest.format_report(cfg, 120, 0, int(START * 1000), {3.0: hot}, 3.0, 3)
        self.assertIn("펀딩비 과열 필터", text)


class Live(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        logging.disable(logging.NOTSET)

    def run_days(self, rate, days=6, over=None, spot=False):
        self.clock = SimClock(START)
        self.fake = FakeExchange(self.clock, history_days=430, test_days=days + 1)
        self.fake.funding_rate = rate
        tr, tg, store = make_trader(self.fake, self.clock, self.tmp, over=over, spot=spot)
        tr.startup()
        for _ in range(days * 144):
            self.clock.advance(600)
            tr.step()
        return tr, tg, store

    def test_calm_funding_trades_as_before(self):
        tr, tg, store = self.run_days(0.0001)
        self.assertTrue(self.fake.trades, "entries happen")
        self.assertFalse(any("과열" in t for t in tg.texts()))

    def test_crowded_longs_are_not_entered_and_the_user_is_told_once_a_day(self):
        tr, tg, store = self.run_days(0.001)
        self.assertEqual(self.fake.trades, [], "no entry while longs are crowded")
        told = [t for t in tg.texts() if "과열이라 진입하지 않습니다" in t]
        self.assertTrue(told)
        per_coin = {}
        for t in told:
            coin = t.split()[1]
            per_coin[coin] = per_coin.get(coin, 0) + 1
        self.assertLessEqual(max(per_coin.values()), 6 + 1, "at most once a day per coin")
        self.assertTrue(any(e.get("code") == "FUNDING" for e in store.read_all("events.jsonl")))

    def test_notice_at_most_once_a_day_per_coin(self):
        tr, tg, store = self.run_days(0.001, days=1)
        sym = self.fake.symbols[0]

        def told():
            return len([t for t in tg.texts() if "과열이라" in t and t.split()[1] == sym[:-4]])

        self.clock.advance(86400)  # a day after anything the run itself said
        tc0 = int(self.clock() * 1000) // (4 * H) * 4 * H - 1
        tr.data[sym] = {"ind": {"tc": [tc0 + k * 4 * H for k in range(8)]}, "sig": {}}
        n0 = told()
        self.assertFalse(tr.funding_ok(sym, {"side": S.LONG, "i": 0}))
        self.assertEqual(told(), n0 + 1)
        self.clock.advance(4 * 3600)
        self.assertFalse(tr.funding_ok(sym, {"side": S.LONG, "i": 1}))
        self.assertEqual(told(), n0 + 1, "the same coin again within a day: no new notice")
        self.clock.advance(86400)
        self.assertFalse(tr.funding_ok(sym, {"side": S.LONG, "i": 7}))
        self.assertEqual(told(), n0 + 2)

    def test_unreadable_funding_history_never_blocks(self):
        tr, tg, store = self.run_days(None)  # the funding endpoint answers 404
        self.assertTrue(self.fake.trades, "the filter steps aside: the bot trades as before")

    def test_switched_off(self):
        tr, tg, store = self.run_days(0.001, over={"funding_max_pct": 0.0})
        self.assertTrue(self.fake.trades)
        self.assertFalse(any("과열" in t for t in tg.texts()))

    def test_one_funding_lookup_per_signal_candle(self):
        calls = []
        tr, tg, store = self.run_days(0.001, days=1)
        real = tr.api.funding_rates

        def spy(*a, **kw):
            calls.append(a)
            return real(*a, **kw)

        tr.api.funding_rates = spy
        for _ in range(3 * 144):
            self.clock.advance(600)
            tr.step()
        sigs = {(sym, d["ind"]["tc"][d["sig"]["i"]]) for sym, d in tr.data.items() if d["sig"].get("side")}
        self.assertLessEqual(len(calls), 3 * 6 * len(self.fake.symbols), "bounded: once per symbol and candle")
        self.assertTrue(calls or not sigs)

    def test_setting_and_config_check(self):
        tr, tg, store = self.run_days(0.0001, days=1)
        tr.handle_update({"message": {"chat": {"id": 42}, "text": "/set funding_max_pct 0.05"}})
        self.assertEqual(tr.cfg["funding_max_pct"], 0.05)
        tr.handle_update({"message": {"chat": {"id": 42}, "text": "/set funding_max_pct 5"}})
        self.assertEqual(tr.cfg["funding_max_pct"], 0.05, "out of range is refused")
        tr.handle_update({"message": {"chat": {"id": 42}, "text": "/settings"}})
        self.assertIn("funding_max_pct", tg.texts()[-1])
        tmp = tempfile.mkdtemp()
        try:
            self.assertEqual(B.check(tmp), 0)
            with open(os.path.join(tmp, "settings.json"), "w", encoding="utf-8") as fh:
                json.dump({"funding_max_pct": 3}, fh)
            with self.assertRaises(SystemExit):
                B.check(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
