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


def _wavy(base, growth, days, amp=0.01):
    import math
    return [base * growth ** i * (1 + amp * math.sin(i * 1.3)) for i in range(days)]


class BtcGate(unittest.TestCase):
    """v2.2: BTC's own momentum gates the alts (spot sleeve only)."""
    P = dict(SpotRules.P, spot_btc_gate=True, spot_btc_gate_min=0.5)
    UNI = ["BTCUSDT", "AAAUSDT", "BBBUSDT"]

    def btc_down(self):
        # long decline, short bounce: only the 20-day lookback is up -> BTC signal 1/4
        return [100 * 0.996 ** i for i in range(280)] + [100 * 0.996 ** 279 * 1.004 ** i for i in range(1, 26)]

    def series(self, btc):
        return {"BTCUSDT": series_from(btc), "AAAUSDT": series_from(_wavy(10, 1.003, len(btc))),
                "BBBUSDT": series_from(_wavy(5, 1.002, len(btc), 0.02))}

    def test_btc_downtrend_zeroes_alts_and_keeps_btc(self):
        s = self.series(self.btc_down())
        g = SS.btc_gate(s, self.P)
        self.assertEqual(g["frac"], 0.25)
        self.assertFalse(g["open"])
        gated, _ = SS.target_weights(s, self.UNI, self.P)
        free, _ = SS.target_weights(s, self.UNI, dict(self.P, spot_btc_gate=False))
        self.assertGreater(free["AAAUSDT"], 0.0)
        self.assertGreater(free["BBBUSDT"], 0.0)
        self.assertEqual(gated["AAAUSDT"], 0.0)
        self.assertEqual(gated["BBBUSDT"], 0.0)
        self.assertGreater(gated["BTCUSDT"], 0.0)
        self.assertEqual(gated["BTCUSDT"], free["BTCUSDT"], "BTC keeps exactly its ungated weight")

    def test_btc_uptrend_changes_nothing(self):
        s = self.series(_wavy(100, 1.003, 305))
        self.assertTrue(SS.btc_gate(s, self.P)["open"])
        self.assertEqual(SS.target_weights(s, self.UNI, self.P),
                         SS.target_weights(s, self.UNI, dict(self.P, spot_btc_gate=False)))

    def test_gate_off_is_the_old_behaviour(self):
        s = self.series(self.btc_down())
        off = dict(self.P, spot_btc_gate=False)
        self.assertIsNone(SS.btc_gate(s, off))
        # the pre-gate rules, reproduced by hiding BTC's name from the gate
        old = {("XBTUSDT" if k == "BTCUSDT" else k): v for k, v in s.items()}
        w_old, d_old = SS.target_weights(old, ["XBTUSDT", "AAAUSDT", "BBBUSDT"], self.P)
        w_off, d_off = SS.target_weights(s, self.UNI, off)
        self.assertEqual(w_off, {("BTCUSDT" if k == "XBTUSDT" else k): v for k, v in w_old.items()})
        self.assertEqual(d_off, {("BTCUSDT" if k == "XBTUSDT" else k): v for k, v in d_old.items()})

    def test_missing_or_short_btc_history_does_not_gate(self):
        s = self.series(self.btc_down())
        short = dict(s, BTCUSDT=series_from(self.btc_down()[-250:]))  # 250 days: the 250-day lookback is missing
        self.assertIsNone(SS.btc_gate(short, self.P))
        w, _ = SS.target_weights(short, self.UNI, self.P)
        self.assertGreater(w["AAAUSDT"], 0.0)
        no_btc = {k: v for k, v in s.items() if k != "BTCUSDT"}
        w2, _ = SS.target_weights(no_btc, self.UNI, self.P)
        self.assertGreater(w2["AAAUSDT"], 0.0)

    def test_backtest_uses_the_same_gate(self):
        s = self.series(self.btc_down() + [100 * 0.996 ** 279 * 1.004 ** 25 * 0.998 ** i for i in range(1, 60)])
        p = dict(self.P, spot_universe_size=3, spot_universe_min_days=300)
        start = 300 * 86_400_000
        on = SS.simulate(s, p, start, start_equity=10_000)
        off = SS.simulate(s, dict(p, spot_btc_gate=False), start, start_equity=10_000)
        self.assertNotEqual([e for _, e in on], [e for _, e in off])
        self.assertGreater(off[-1][1], on[-1][1], "the alts kept rising, so blocking them cost money here")


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
        gate = tr.spot.gate_line()
        g = tr.state["spot"]["btc_gate"]["info"]
        self.assertEqual(gate, f"BTC 신호 {int(round(g['frac'] * 4))}/4 · 알트 {'허용' if g['open'] else '차단'}")
        self.assertIn(gate, tg.texts()[-1])
        self.assertIn(gate, tr.brief_text("morning", self.clock()))
        self.assertNotIn("BTC 신호", tr.brief_text("evening", self.clock()))
        tr.handle_update({"message": {"chat": {"id": 42}, "text": "/set spot_btc_gate false"}})
        self.assertFalse(tr.cfg["spot_btc_gate"])
        self.assertEqual(tr.spot.gate_line(), "BTC 게이트 꺼짐 · 알트 허용")
        tr.handle_update({"message": {"chat": {"id": 42}, "text": "/set spot_btc_gate_min 1.5"}})
        self.assertEqual(tr.cfg["spot_btc_gate_min"], 0.5, "outside 0~1 is refused")
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


class SpotEarn(unittest.TestCase):
    """v2.3: idle spot USDT waits in Simple Earn (flexible); it stays sleeve money and never blocks trading."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, mode="live", days=6, earn=True):
        self.clock = SimClock(START)
        self.fake = FakeExchange(self.clock, history_days=430, test_days=days + 1)
        return make_trader(self.fake, self.clock, self.tmp, mode=mode, over={"spot_earn": earn}, spot=True)

    def run_days(self, tr, days, step=600):
        for _ in range(int(days * DAY / step)):
            self.clock.advance(step)
            tr.step()

    def earn_calls(self, method=None):
        return [c for c in self.fake.calls if "simple-earn" in c[1] and (method is None or c[0] == method)]

    def total_matches_exchange(self, tr):
        tr.spot.value(refresh=True)
        tot, sv, fv = tr.total_equity()
        real = self.fake.spot_value() + self.fake.wallet + self.fake.upnl()
        self.assertAlmostEqual(tot, real, delta=real * 0.01 + 0.05, msg="parked USDT must stay in the valuation")

    def test_parks_leftover_and_redeems_before_a_transfer(self):
        tr, tg, store = self.make()
        tr.startup()
        self.run_days(tr, 1)
        subs = [p for k, p in self.fake.earn_ops if k == "subscribe"]
        self.assertTrue(subs, "leftover USDT is parked after the daily run")
        self.assertEqual(subs[0]["autoSubscribe"], "false", "Binance must not sweep the spot wallet on its own")
        self.assertEqual(subs[0]["sourceAccount"], "SPOT")
        self.assertGreater(self.fake.earn_usdt, 1.0)
        self.assertLessEqual(self.fake.spot_bal["USDT"], 2.0 + 0.011, "2 USDT stay in the spot wallet")
        self.assertAlmostEqual(tr.spot.value(refresh=True)["earn"], self.fake.earn_usdt, places=6)
        self.total_matches_exchange(tr)
        # the user moves the target to 20 % spot: the sleeve must send money to futures, so Earn pays it out first
        tr._save_setting("alloc_spot_pct", 20.0)
        tr.force_rebalance()
        n_out = len([t for t in self.fake.transfers if t["type"] == "MAIN_UMFUTURE"])
        self.clock.advance(60)
        tr.step()
        self.assertTrue([k for k, _ in self.fake.earn_ops if k == "redeem"], "redeemed before the transfer")
        self.assertGreater(len([t for t in self.fake.transfers if t["type"] == "MAIN_UMFUTURE"]), n_out)
        self.total_matches_exchange(tr)
        tot, sv, fv = tr.total_equity()
        self.assertAlmostEqual(sv / tot, 0.20, delta=0.05)
        tr.cmd_spot([])
        self.assertIn("이자 예치(유연 USDT) $", tg.texts()[-1])
        self.assertIn("누적 이자", tg.texts()[-1])
        tr.cmd_export([])
        rows = list(csv.reader(io.StringIO(tg.docs[-1][1].decode("utf-8-sig"))))
        kinds = {r[0] for r in rows[1:]}
        self.assertIn("이자예치", kinds)
        self.assertIn("이자환매", kinds)

    def test_failed_redemption_never_stops_trading(self):
        tr, tg, store = self.make()
        tr.startup()
        self.run_days(tr, 1)
        self.assertGreater(self.fake.earn_usdt, 1.0)
        self.fake.earn_fail_redeem = True
        n_trades = len(store.read_all("spot_trades.jsonl"))
        tr._save_setting("alloc_spot_pct", 20.0)  # needs cash for sells -> transfer; Earn will refuse to pay out
        tr.force_rebalance()
        self.clock.advance(60)
        tr.step()
        st = tr.state["spot"]
        self.assertEqual(st["fails"], 0, "the daily spot job itself still succeeded")
        self.assertEqual(st["last_day"], B.utc_day(self.clock()))
        self.assertGreater(len(store.read_all("spot_trades.jsonl")), n_trades, "sells still went through")
        self.assertTrue(any("이자 예치 환매 실패" in t for t in tg.texts()))
        self.total_matches_exchange(tr)
        # no more deposit/redemption attempts for the rest of the day; only the hourly balance read for valuation
        n_moves, n_reads = len(self.earn_calls("POST")), len(self.earn_calls("GET"))
        tr.force_rebalance()
        self.run_days(tr, 0.3)
        self.assertEqual(len(self.earn_calls("POST")), n_moves, "Earn rests until tomorrow after a failure")
        self.assertLessEqual(len(self.earn_calls("GET")) - n_reads, 8, "balance reads stay rare")
        for sym in self.fake.positions:
            self.assertEqual(len(self.fake.open_algos(sym)), 1)
        # tomorrow it works again
        self.fake.earn_fail_redeem = False
        self.run_days(tr, 1)
        self.assertTrue([k for k, _ in self.fake.earn_ops if k == "redeem"])

    def test_unreadable_earn_balance_keeps_the_last_known_amount(self):
        tr, tg, store = self.make()
        tr.startup()
        self.run_days(tr, 1)
        parked = self.fake.earn_usdt
        self.fake.earn_fail_read = True
        v = tr.spot.value(refresh=True)
        tr.spot.earn_value(refresh=True)
        self.assertAlmostEqual(tr.spot.value(refresh=True)["earn"], parked, places=6)
        self.assertGreater(v["equity"], parked)

    def test_paper_mode_never_calls_earn_and_accrues_interest(self):
        tr, tg, store = self.make(mode="paper", days=8)
        tr.startup()
        self.run_days(tr, 7)
        self.assertEqual(self.earn_calls(), [], "paper mode must never call Binance Earn")
        self.assertEqual(self.fake.earn_usdt, 0.0)
        ps = tr.spot.ex.st
        self.assertGreater(ps["earn"], 1.0, "virtual Earn balance")
        self.assertGreater(ps["earn_rewards"], 0.0)
        self.assertLess(ps["earn_rewards"], ps["earn"] * 0.03 * 8 / 365 * 1.5)
        v = tr.spot.value(refresh=True)
        self.assertGreater(v["earn"], 1.0)
        self.assertAlmostEqual(v["equity"], v["usdt"] + v["earn"] + sum(h["val"] for h in v["holdings"].values()),
                               places=6)
        self.assertTrue(all(r["mode"] == "paper" for r in store.read_all("earn.jsonl")))

    def test_switched_off_does_nothing(self):
        tr, tg, store = self.make(earn=False)
        tr.startup()
        self.run_days(tr, 2)
        self.assertEqual(self.earn_calls(), [])
        self.assertEqual(store.read_all("earn.jsonl"), [])
        self.assertIn("이자 예치 꺼짐", tr.spot.earn_line())

    def test_selftest_checks_earn_access(self):
        import selftest
        self.make()
        sapi = make_spot_api(self.fake, self.clock)
        self.fake.earn_usdt = 12.5
        res = selftest.run_spot_checks(sapi, {"spot_earn": True, "spot_candidates": list(self.fake.symbols)})
        row = next(r for r in res if r["name"].startswith("Simple Earn"))
        self.assertTrue(row["ok"])
        self.assertFalse(row.get("blocking", True), "Earn trouble must never block /live")
        wallet = next(r for r in res if r["name"] == "현물 지갑")
        self.assertGreaterEqual(wallet["spot_equity"], 12.5, "parked USDT counts as spot money")
        self.fake.earn_fail_read = True
        res = selftest.run_spot_checks(sapi, {"spot_earn": True, "spot_candidates": list(self.fake.symbols)})
        self.assertFalse(next(r for r in res if r["name"].startswith("Simple Earn"))["ok"])
        off = selftest.run_spot_checks(sapi, {"spot_earn": False, "spot_candidates": list(self.fake.symbols)})
        self.assertIsNone(next(r for r in off if r["name"].startswith("Simple Earn"))["ok"])

    def test_backtest_pays_interest_on_idle_cash(self):
        down = [100 * 0.997 ** i for i in range(700)]  # the sleeve sits in USDT the whole time
        s = {"X": series_from(down, 1e9)}
        p = dict(SpotRules.P, spot_universe_size=1, spot_universe_min_days=365, spot_cash_yield=0.05)
        start = 380 * 86_400_000
        off = SS.simulate(s, p, start, start_equity=10_000)
        on = SS.simulate(s, dict(p, spot_earn=True), start, start_equity=10_000)
        self.assertAlmostEqual(off[-1][1], 10_000, delta=1e-6)
        days = len(on)
        self.assertAlmostEqual(on[-1][1], 2 + 9_998 * (1 + 0.05 / 365) ** days, delta=0.5)


if __name__ == "__main__":
    unittest.main()
