"""Backtester on the fake exchange's data + updater/rollback round trip."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import backtest  # noqa: E402
import updater  # noqa: E402
from fake_binance import FakeExchange, SimClock, make_api  # noqa: E402
from test_live_flow import START  # noqa: E402

with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
    CFG = json.load(fh)


class BacktestTests(unittest.TestCase):
    def test_report_on_fake_history(self):
        clock = SimClock(START)
        fake = FakeExchange(clock, direction=1, history_days=420, test_days=1)
        api = make_api(fake, clock)
        cfg = dict(CFG, symbols=list(fake.symbols))
        tmp = tempfile.mkdtemp()
        try:
            text, results = backtest.report(cfg, 60, risks=[1.5], cache_dir=tmp, api=api, now_ms=int(START * 1000))
            self.assertIn("백테스트", text)
            self.assertIn("1회 위험별 비교", text)
            self.assertEqual(sorted(results), [1.0, 1.5, 2.0, 3.0, 5.0])
            for r, res in results.items():
                eq = [e for _, e in res["curve"]]
                self.assertTrue(eq)
                self.assertGreater(min(eq), 0)
                for t in res["trades"]:
                    self.assertGreater(t["qty"], 0)
            # higher risk -> bigger position sizes
            def avg_qty(r):
                ts = results[r]["trades"]
                return sum(t["qty"] * t["entry"] for t in ts) / max(1, len(ts))
            self.assertGreater(avg_qty(5.0), avg_qty(1.0))
            # cache is reused on the second run (no new kline downloads)
            n_before = len([c for c in fake.calls if c[1] == "/fapi/v1/klines"])
            backtest.report(cfg, 60, cache_dir=tmp, api=api, now_ms=int(START * 1000))
            n_after = len([c for c in fake.calls if c[1] == "/fapi/v1/klines"])
            self.assertLessEqual(n_after - n_before, 2 * len(fake.symbols))
            print("\n" + text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_simulate_pnl_consistency(self):
        clock = SimClock(START)
        fake = FakeExchange(clock, direction=-1, history_days=420, test_days=1)
        api = make_api(fake, clock)
        cfg = dict(CFG, symbols=list(fake.symbols))
        data, start = backtest.load_data(api, cfg, 90, None, int(START * 1000))
        pre = backtest.precompute(cfg, data)
        res = backtest.simulate(cfg, pre, start, 3.0, 100.0)
        booked = sum(t["pnl"] for t in res["trades"])
        self.assertAlmostEqual(res["final"] - 100.0, booked, delta=1e-6)


class FundingTests(unittest.TestCase):
    """v2.1: the backtest charges the real funding history (longs pay positive rates) instead of a guess."""

    def run_bt(self, rate, cache_dir=None):
        clock = SimClock(START)
        fake = FakeExchange(clock, direction=1, history_days=420, test_days=1)
        fake.funding_rate = rate
        api = make_api(fake, clock)
        cfg = dict(CFG, symbols=list(fake.symbols))
        data, start = backtest.load_data(api, cfg, 120, cache_dir, int(START * 1000))
        pre = backtest.precompute(cfg, data)
        return backtest.simulate(cfg, pre, start, 3.0, 100.0), data, fake

    def test_real_funding_history_is_charged_by_side(self):
        free, data, _ = self.run_bt(0.0)
        self.assertEqual(free["funding_mode"], "real")
        ft, fr = data["BTCUSDT"]["funding"]
        self.assertTrue(ft and all(b - a == 8 * 3_600_000 for a, b in zip(ft, ft[1:])))
        self.assertTrue(free["trades"], "the fake uptrend should produce long trades")
        paid, _, _ = self.run_bt(0.002)  # 0.2% every 8h: longs pay a lot
        self.assertLess(paid["final"], free["final"] - 1e-6)
        got, _, _ = self.run_bt(-0.002)  # negative funding: longs are paid
        self.assertGreater(got["final"], free["final"] + 1e-6)
        for res in (free, paid, got):  # cash accounting still adds up trade by trade
            self.assertAlmostEqual(res["final"] - 100.0, sum(t["pnl"] for t in res["trades"]), delta=1e-6)

    def test_missing_funding_endpoint_falls_back_to_flat(self):
        res, data, _ = self.run_bt(None)  # the fake answers 404
        self.assertEqual(res["funding_mode"], "flat")
        self.assertIsNone(data["BTCUSDT"]["funding"])
        text = backtest.format_report(CFG, 120, 0, int(START * 1000), {3.0: res}, 3.0, 3)
        self.assertIn("펀딩비(추정치)", text)

    def test_funding_history_cache_is_topped_up(self):
        tmp = tempfile.mkdtemp()
        try:
            _, _, fake = self.run_bt(0.0001, cache_dir=tmp)
            first = len([c for c in fake.calls if c[1] == "/fapi/v1/fundingRate"])
            self.assertTrue(os.path.exists(os.path.join(tmp, "BTCUSDT_funding.json")))
            res, _, fake2 = self.run_bt(0.0001, cache_dir=tmp)
            again = len([c for c in fake2.calls if c[1] == "/fapi/v1/fundingRate"])
            self.assertGreater(first, 0)
            self.assertLessEqual(again, len(fake2.symbols))  # one small top-up request per symbol
            self.assertEqual(res["funding_mode"], "real")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ConfigCheckTests(unittest.TestCase):
    def test_check_rejects_a_bad_monthly_report_value(self):
        import bot
        tmp = tempfile.mkdtemp()
        try:
            self.assertEqual(bot.check(tmp), 0)
            with open(os.path.join(tmp, "settings.json"), "w", encoding="utf-8") as fh:
                json.dump({"monthly_report": "yes"}, fh)
            with self.assertRaises(SystemExit):
                bot.check(tmp)
            for bad in ({"spot_btc_gate": "on"}, {"spot_btc_gate_min": 1.5}, {"spot_btc_gate_min": "0.5"}):
                with open(os.path.join(tmp, "settings.json"), "w", encoding="utf-8") as fh:
                    json.dump(bad, fh)
                with self.assertRaises(SystemExit, msg=str(bad)):
                    bot.check(tmp)
            with open(os.path.join(tmp, "settings.json"), "w", encoding="utf-8") as fh:
                json.dump({"spot_btc_gate": False, "spot_btc_gate_min": 0.75}, fh)
            self.assertEqual(bot.check(tmp), 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class UpdaterTests(unittest.TestCase):
    def test_update_and_rollback(self):
        tmp = tempfile.mkdtemp()
        try:
            app = os.path.join(tmp, "app")
            data = os.path.join(tmp, "data")
            os.makedirs(data)
            shutil.copytree(ROOT, app, ignore=shutil.ignore_patterns("__pycache__", "tests", "*.zip"))
            # build a "new release" zip with a bumped version inside a top folder, like the real one
            new_src = os.path.join(tmp, "release", "trendhunter")
            shutil.copytree(app, new_src)
            cfgp = os.path.join(new_src, "config.json")
            c = json.load(open(cfgp, encoding="utf-8"))
            c["version"] = "9.9.9"
            json.dump(c, open(cfgp, "w", encoding="utf-8"))
            zpath = os.path.join(tmp, "trendhunter.zip")
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
                for base, _, files in os.walk(os.path.join(tmp, "release")):
                    for f in files:
                        full = os.path.join(base, f)
                        z.write(full, os.path.relpath(full, os.path.join(tmp, "release")))
            old, new = updater.update("file://" + zpath, app, data)
            self.assertEqual((old, new), (CFG["version"], "9.9.9"))
            self.assertEqual(json.load(open(os.path.join(app, "config.json")))["version"], "9.9.9")
            self.assertEqual(updater.rollback(app, data), CFG["version"])
            # a broken release is refused and nothing changes
            bad = os.path.join(tmp, "bad.zip")
            with zipfile.ZipFile(bad, "w") as z:
                z.writestr("trendhunter/bot.py", "def broken(:\n")
            with self.assertRaises(updater.UpdateError):
                updater.update("file://" + bad, app, data)
            self.assertEqual(json.load(open(os.path.join(app, "config.json")))["version"], CFG["version"])
            r = subprocess.run([sys.executable, os.path.join(app, "bot.py"), "--check", "--data", data],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
