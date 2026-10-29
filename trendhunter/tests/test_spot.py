"""Spot sleeve + allocation tests: pure rules, then the real Trader against the fake exchange (paper and live)."""
import csv
import io
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

import backtest_mix  # noqa: E402
import bot as B  # noqa: E402
import spot_strategy as SS  # noqa: E402
from fake_binance import FakeExchange, SimClock, make_api, make_spot_api  # noqa: E402
from test_live_flow import make_trader  # noqa: E402

logging.basicConfig(level=logging.WARNING)
DAY = 86400
START = 1_760_000_000 // DAY * DAY + 3600 * 2 + 600  # 02:10 UTC: the daily spot job is due right away


def series_from(closes, qv=1e8):
    t = [i * 86_400_000 for i in range(len(closes))]
    return {"t": t, "c": list(closes), "qv": [qv] * len(closes)}


class SpotRules(unittest.TestCase):
    P = {"spot_lookbacks": [20, 60, 120, 250], "spot_vol_target": 0.6, "spot_vol_days": 60, "spot_max_weight": 1.0,
         "spot_max_gross": 1.0, "spot_min_history_days": 200}

    def test_momentum_fraction(self):
        up = [100 * 1.002 ** i for i in range(300)]
        self.assertEqual(SS.momentum_fraction(up, [20, 60, 120, 250]), 1.0)
        down = [100 * 0.998 ** i for i in range(300)]
        self.assertEqual(SS.momentum_fraction(down, [20, 60, 120, 250]), 0.0)
        short_hist = up[:100]  # only the 20 and 60 day lookbacks exist
        self.assertEqual(SS.momentum_fraction(short_hist, [20, 60, 120, 250]), 0.5)

    def test_vol_floor_and_none(self):
        flat = [100.0] * 100
        self.assertEqual(SS.ann_vol(flat, 60), 0.15)
        self.assertIsNone(SS.ann_vol(flat[:30], 60))

    def test_universe_by_volume(self):
        s = {"A": series_from([1] * 400, 5e8), "B": series_from([1] * 400, 9e8), "C": series_from([1] * 300, 9e9),
             "D": series_from([1] * 400, 1e8)}
        self.assertEqual(SS.select_universe(s, ["A", "B", "C", "D"], n=2, min_days=365), ["B", "A"])

    def test_weights_scale_with_vol_and_cap(self):
        import math
        calm = [100 * 1.002 ** i * (1 + 0.004 * math.sin(i)) for i in range(300)]
        wild = [100 * 1.002 ** i * (1 + 0.04 * math.sin(i)) for i in range(300)]
        w, d = SS.target_weights({"C": series_from(calm), "W": series_from(wild)}, ["C", "W"], self.P)
        self.assertGreater(w["C"], w["W"], "lower volatility must get the larger weight")
        self.assertLessEqual(sum(w.values()), 1.0 + 1e-9)
        w2, _ = SS.target_weights({"C": series_from(calm)}, ["C"], dict(self.P, spot_vol_target=5.0))
        self.assertLessEqual(w2["C"], 1.0 + 1e-9, "spot never levers")

    def test_plan_trades_band_dust_and_order(self):
        plan = SS.plan_trades({"A": 30.0, "B": 3.0, "C": 50.0}, {"A": 0.32, "B": 0.0, "C": 0.0, "D": 0.2}, 100.0, 0.05, 5.0)
        syms = [p[0] for p in plan]
        self.assertNotIn("A", syms, "a 2% change is inside the 5% band")
        self.assertNotIn("B", syms, "dust below min notional is left alone")
        self.assertEqual(syms, ["C", "D"], "sells come before buys")
        self.assertTrue(plan[0][2], "C is a full exit")
        self.assertAlmostEqual(plan[1][1], 20.0)

    def test_simulate_invests_in_uptrend_and_exits_in_downtrend(self):
        import math
        trend = [100 * 1.003 ** i for i in range(500)]
        trend += [trend[-1] * 0.99 ** i for i in range(1, 300)]
        noisy = [x * (1 + 0.03 * math.sin(i * 1.7)) for i, x in enumerate(trend)]  # ~55% annualised vol
        s = {"X": series_from(noisy, 1e9)}
        p = dict(self.P, spot_universe_size=1, spot_universe_min_days=365)
        curve = SS.simulate(s, p, 380 * 86_400_000, start_equity=10_000)
        peak = max(e for _, e in curve)
        self.assertGreater(peak, 10_000 * 1.1, "rides the uptrend")
        hold = noisy[-1] / noisy[380]
        self.assertGreater(curve[-1][1] / 10_000, hold * 3, "keeps most of the money while the price falls ~95%")
        w, _ = SS.target_weights({"X": series_from(noisy)}, ["X"], p)
        self.assertEqual(w["X"], 0.0, "ends the crash in cash")


class SpotFlows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, mode="live", days=8, spot_balance=0.0, over=None, **fk):
        self.clock = SimClock(START)
        self.fake = FakeExchange(self.clock, history_days=430, test_days=days + 1, spot_balance=spot_balance, **fk)
        tr, tg, store = make_trader(self.fake, self.clock, self.tmp, mode=mode, over=over, spot=True)
        return tr, tg, store

    def run_days(self, tr, days, step=600, check=None):
        for _ in range(int(days * DAY / step)):
            self.clock.advance(step)
            tr.step()
            if check:
                check()

    def test_paper_split_and_rebalance(self):
        tr, tg, store = self.make(mode="paper")
        tr.startup()
        self.assertTrue(tr.spot_ready)
        tr.step()
        moves = store.read_all("transfers.jsonl")
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["to"], "spot")
        self.assertAlmostEqual(moves[0]["amount"], 50.0, delta=0.6)
        trades = store.read_all("spot_trades.jsonl")
        self.assertTrue(trades, "uptrend -> the spot sleeve buys")
        self.assertTrue(all(t["mode"] == "paper" for t in trades))
        tot, sv, fv = tr.total_equity()
        self.assertAlmostEqual(tot, 100.0, delta=1.5)
        self.assertEqual(self.fake.spot_orders, {}, "paper mode must never touch the real spot account")
        self.assertEqual(self.fake.transfers, [])
        self.run_days(tr, 2)
        self.assertEqual(len(store.read_all("transfers.jsonl")), 1, "inside the band -> no churn")
        tr.cmd_spot([])
        self.assertTrue(any(t.startswith("🪙 현물") for t in tg.texts()))
        tr.cmd_export([])
        name, data, cap = tg.docs[-1]
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
        self.assertEqual(rows[0][0], "구분")
        self.assertTrue(any(r[0] == "현물체결" for r in rows[1:]))
        self.assertTrue(any(r[0] == "지갑이체" for r in rows[1:]))

    def test_live_split_trades_and_accounting(self):
        tr, tg, store = self.make(mode="live", days=10)
        tr.startup()
        self.assertTrue(tr.spot_ready)
        w0 = self.fake.wallet

        def check():
            for a, v in self.fake.spot_bal.items():
                self.assertGreaterEqual(v, -1e-9, f"negative spot balance {a}")
            for sym, p in self.fake.positions.items():
                self.assertEqual(len(self.fake.open_algos(sym)), 1, f"{sym} must keep exactly one stop")

        self.run_days(tr, 1, check=check)
        tin = [t for t in self.fake.transfers if t["type"] == "UMFUTURE_MAIN"]
        self.assertTrue(tin, "first daily run moves half of the futures USDT to spot")
        self.assertAlmostEqual(tin[0]["amount"], 50.0, delta=1.0)
        self.assertGreater(self.fake.spot_value() - self.fake.spot_bal.get("USDT", 0.0), 5.0, "coins were bought")
        L = tr.ledger
        self.assertLess(L["start_equity"], 60.0, "futures floor baseline follows the transfer")
        self.assertGreater(L["start_equity"], 40.0)
        self.run_days(tr, 8, check=check)
        tot, sv, fv = tr.total_equity()
        real = self.fake.spot_value() + self.fake.wallet + self.fake.upnl()
        self.assertAlmostEqual(tot, real, delta=real * 0.01 + 0.05, msg="bot view of total equity matches the exchange")
        # futures accounting still exact after transfers
        tr.act_closeall()
        net_out = sum(t["amount"] for t in self.fake.transfers if t["type"] == "UMFUTURE_MAIN") - \
            sum(t["amount"] for t in self.fake.transfers if t["type"] == "MAIN_UMFUTURE")
        booked = sum(j["pnl"] for j in store.read_all("journal.jsonl"))
        self.assertAlmostEqual(self.fake.wallet - w0 + net_out, booked, delta=0.01)
        # every recorded spot fill exists on the exchange (no phantom / duplicate orders)
        recs = store.read_all("spot_trades.jsonl")
        self.assertEqual(len(recs), len(self.fake.spot_orders))

    def test_transfer_permission_missing(self):
        tr, tg, store = self.make(mode="live")
        self.fake.restrictions["permitsUniversalTransfer"] = False
        tr.startup()
        self.run_days(tr, 1)
        self.assertTrue(any("자동 이체 실패" in t for t in tg.texts()))
        self.assertEqual(self.fake.transfers, [])
        self.assertEqual(self.fake.spot_orders, {}, "nothing to buy with an empty spot wallet")
        self.assertTrue(tr.spot_ready, "the sleeve stays alive and retries tomorrow")

    def test_spot_money_flows_to_futures(self):
        tr, tg, store = self.make(mode="live", balance=0.0, spot_balance=100.0)
        tr.startup()
        self.run_days(tr, 1)
        tout = [t for t in self.fake.transfers if t["type"] == "MAIN_UMFUTURE"]
        self.assertTrue(tout)
        self.assertAlmostEqual(sum(t["amount"] for t in tout), 50.0, delta=1.5)
        self.assertGreater(tr.ledger["start_equity"], 0.0, "futures baseline set from the first deposit")

    def test_lost_order_response_is_not_duplicated(self):
        tr, tg, store = self.make(mode="live")
        self.fake.spot_drop_response = 1
        tr.startup()
        self.run_days(tr, 1)
        recs = store.read_all("spot_trades.jsonl")
        self.assertTrue(recs)
        self.assertEqual(len(recs), len(self.fake.spot_orders), "the lost order was looked up, not re-sent")

    def test_transfer_timeout_is_verified(self):
        tr, tg, store = self.make(mode="live")
        self.fake.transfer_drop_response = 1
        tr.startup()
        self.run_days(tr, 1)
        self.assertEqual(len(self.fake.transfers), 1, "no second transfer after the lost response")
        self.assertEqual(len(store.read_all("transfers.jsonl")), 1, "the verified transfer is booked")
        self.assertLess(tr.ledger["start_equity"], 60.0, "and the futures baseline follows it")

    def test_delisted_candidate_is_skipped(self):
        tr, tg, store = self.make(mode="paper", over={"spot_candidates": ["AAAUSDT", "ZZZUSDT", "BBBUSDT", "BTCUSDT"]})
        tr.startup()
        self.assertTrue(tr.spot_ready)
        self.assertNotIn("ZZZUSDT", tr.spot.filters)
        self.assertTrue(any("바이낸스에 없는 종목" in t for t in tg.texts()))

    def test_alloc_command_moves_money(self):
        tr, tg, store = self.make(mode="paper")
        tr.startup()
        tr.step()
        tr.act_alloc(80)
        self.clock.advance(60)
        tr.step()
        tot, sv, fv = tr.total_equity()
        self.assertAlmostEqual(sv / tot, 0.8, delta=0.03)
        self.assertEqual(json.load(open(os.path.join(self.tmp, "settings.json")))["alloc_spot_pct"], 80.0)

    def test_preset_and_mode_switch(self):
        tr, tg, store = self.make(mode="paper")
        tr.startup()
        tr.step()
        tr.act_preset("공격")
        self.assertEqual(tr.cfg["risk_per_trade_pct"], 4.0)
        self.assertEqual(tr.cfg["alloc_spot_pct"], 40.0)
        tr.act_go_live()
        self.assertEqual(tr.state["mode"], "live")
        self.assertEqual(tr.spot.ex.name, "live")
        self.assertIsNone(tr.state["spot"]["last_day"])
        self.clock.advance(60)
        tr.step()
        self.assertTrue(self.fake.transfers, "live account gets split right after /live")

    def test_rebase_resets_total_baseline(self):
        tr, tg, store = self.make(mode="paper")
        tr.startup()
        self.run_days(tr, 1)
        tr.spot.ex.adjust_usdt(1000.0)  # e.g. a 2028 deposit landing in the spot wallet
        tr.cmd_rebase([])
        tot, sv, fv = tr.total_equity()
        self.assertAlmostEqual(tr.state["total"]["base"], tot, delta=0.01)
        self.assertAlmostEqual(tr.state["spot"]["net_in"], sv, delta=0.01)
        self.assertTrue(any("총자산 기준점" in t for t in tg.texts()))

    def test_backtest_mix_runs(self):
        tr, tg, store = self.make(mode="paper")
        cfg = dict(tr.cfg)
        text, out = backtest_mix.report(cfg, 45, fut_api=make_api(self.fake, self.clock),
                                        spot_api=make_spot_api(self.fake, self.clock),
                                        cache_dir=os.path.join(self.tmp, "cache"))
        self.assertIn("백테스트 v2", text)
        self.assertIn(0.5, out["mixes"])
        self.assertGreater(len(out["mixes"][0.5]), 30)


if __name__ == "__main__":
    unittest.main()
