"""Regression tests for the independent review of the v2 spot sleeve (one test per finding)."""
import logging
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import selftest  # noqa: E402
import spot_strategy as SS  # noqa: E402
import strategy as S  # noqa: E402
from fake_binance import FakeExchange, SimClock, make_api, make_spot_api  # noqa: E402
from test_live_flow import fake_signal, make_trader  # noqa: E402
from test_spot import START  # noqa: E402

logging.basicConfig(level=logging.ERROR)
DAY = 86400


class ReviewFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, mode="live", days=6, **fk):
        self.clock = SimClock(START)
        self.fake = FakeExchange(self.clock, history_days=430, test_days=days + 1, **fk)
        return make_trader(self.fake, self.clock, self.tmp, mode=mode, spot=True)

    def steps(self, tr, n, dt=600):
        for _ in range(n):
            self.clock.advance(dt)
            tr.step()

    # H1 ---------------------------------------------------------------------------------------------
    def test_paper_never_uses_live_backend_after_toggling(self):
        tr, tg, store = self.make(mode="live")
        tr.startup()
        tr.step()
        live_orders = len(self.fake.spot_orders)
        live_transfers = len(self.fake.transfers)
        tr.cmd_set(["spot_enabled", "false"])
        self.assertIsNone(tr.spot)
        tr.act_closeall()
        tr.act_go_paper()
        tr.cmd_set(["spot_enabled", "true"])
        self.steps(tr, 6 * 24)
        self.assertEqual(len(self.fake.spot_orders), live_orders, "paper mode sent a real spot order")
        self.assertEqual(len(self.fake.transfers), live_transfers, "paper mode moved real money")
        self.assertEqual(tr.spot.ex.name, "paper")
        tot, sv, fv = tr.total_equity()
        self.assertAlmostEqual(tot, 100.0, delta=8.0, msg="paper wallets must not drain")

    def test_mismatched_backend_is_rebuilt(self):
        tr, tg, store = self.make(mode="paper")
        tr.startup()
        tr.step()
        tr.spot.make_ex("live")  # simulate any path that leaves the wrong back-end behind
        n = len(self.fake.spot_orders)
        self.steps(tr, 3)
        self.assertEqual(tr.spot.ex.name, "paper")
        self.assertEqual(len(self.fake.spot_orders), n)

    # H2 ---------------------------------------------------------------------------------------------
    def test_transfer_5xx_after_execution_is_booked(self):
        tr, tg, store = self.make(mode="live")
        self.fake.transfer_5xx_after = 1
        tr.startup()
        self.steps(tr, 3)
        self.assertEqual(len(self.fake.transfers), 1)
        self.assertEqual(len(store.read_all("transfers.jsonl")), 1)
        L = tr.ledger
        self.assertAlmostEqual(L["start_equity"], self.fake.wallet, delta=1.0, msg="floor baseline follows the move")
        ok, code = __import__("risk").can_enter(L, {}, "AAAUSDT", S.LONG, self.clock(), self.fake.wallet - 0.5, tr.cfg)
        self.assertNotEqual(code, "EQUITY_FLOOR")

    def test_empty_futures_wallet_gets_a_real_baseline(self):
        tr, tg, store = self.make(mode="live", balance=0.0, spot_balance=100.0)
        self.fake.transfer_5xx_after = 1
        tr.startup()
        self.steps(tr, 3)
        L = tr.ledger
        self.assertGreater(L["start_equity"], 40.0)
        self.assertGreater(L["day_start_equity"], 40.0, "daily loss limit must not be zero")

    def test_transfer_check_uses_server_time(self):
        import bot as B
        from store import Store
        self.clock = SimClock(START)
        self.fake = FakeExchange(self.clock, history_days=430, test_days=4)
        skewed = lambda: self.clock() + 10.0  # this server's clock runs 10 s ahead of Binance
        store = Store(self.tmp)
        store.save_state({"mode": "live"})
        cfg = B.load_config(B.APP_DIR, store)
        cfg.update(symbols=list(self.fake.symbols), spot_enabled=True, spot_candidates=list(self.fake.symbols))
        from test_live_flow import FakeTG
        tr = B.Trader(cfg, store, make_api(self.fake, skewed), FakeTG(), logging.getLogger("t"), clock=skewed,
                      sleep=lambda s: None, public_api_factory=lambda: make_api(self.fake, skewed),
                      spot_api=make_spot_api(self.fake, skewed), spot_public_factory=lambda: make_spot_api(self.fake, skewed))
        self.fake.transfer_5xx_after = 1
        tr.startup()
        self.steps(tr, 3)
        self.assertEqual(len(self.fake.transfers), 1)
        self.assertEqual(len(store.read_all("transfers.jsonl")), 1, "executed transfer must be booked despite clock skew")
        self.assertAlmostEqual(tr.ledger["start_equity"], self.fake.wallet, delta=1.0)

    def test_history_down_wallet_check_books_transfer(self):
        tr, tg, store = self.make(mode="live")
        self.fake.transfer_5xx_after = 1
        self.fake.transfer_history_fail = True
        tr.startup()
        self.steps(tr, 3)
        self.assertEqual(len(store.read_all("transfers.jsonl")), 1)
        self.assertIsNone(tr.state.get("pending_transfer"))
        self.assertAlmostEqual(tr.ledger["start_equity"], self.fake.wallet, delta=1.0)

    def test_paused_coin_still_counts_as_money(self):
        tr, tg, store = self.make(mode="live", days=4)
        tr.startup()
        self.steps(tr, 3)
        n_transfers = len(self.fake.transfers)
        held = [s_ for s_ in ("AAAUSDT", "BBBUSDT") if self.fake.spot_bal.get(s_[:-4], 0) > 0]
        self.assertTrue(held)
        self.fake.spot_status[held[0]] = "BREAK"
        self.steps(tr, 6 * 24)  # next daily run with that coin paused
        tot, sv, fv = tr.total_equity()
        real = self.fake.spot_value()
        self.assertAlmostEqual(sv, real, delta=real * 0.02 + 0.05, msg="a paused coin must stay in the valuation")
        self.assertEqual(len(self.fake.transfers), n_transfers, "no money moved because of a paused coin")

    # H3 ---------------------------------------------------------------------------------------------
    def test_half_built_sleeve_is_never_ready(self):
        tr, tg, store = self.make(mode="paper")
        self.fake.spot_exinfo_fail = True
        tr.startup()
        self.assertFalse(tr.spot_ready)
        tr.act_go_live()
        self.assertFalse(tr.spot_ready, "no filters -> not ready, even after a mode switch")
        self.steps(tr, 2)
        self.assertEqual(self.fake.transfers, [], "nothing moves while the sleeve is not ready")
        self.fake.spot_exinfo_fail = False
        self.steps(tr, 12)  # retry after a minute / ten minutes
        self.assertTrue(tr.spot_ready)
        self.assertTrue(tr.spot.filters)
        tot, sv, fv = tr.total_equity()
        self.assertAlmostEqual(sv / tot, 0.5, delta=0.05)

    # M1 ---------------------------------------------------------------------------------------------
    def test_spot_lost_response_with_slow_lookup_is_not_resent(self):
        tr, tg, store = self.make(mode="live")
        self.fake.spot_drop_response = 1
        self.fake.spot_hide_lookups = 3  # the order shows up only on the 4th lookup
        tr.startup()
        self.steps(tr, 2)
        self.assertEqual(len(store.read_all("spot_trades.jsonl")), len(self.fake.spot_orders))

    def test_futures_entry_with_unknown_outcome_is_not_doubled(self):
        tr, tg, store = self.make(mode="live")
        tr.cfg["spot_enabled"] = False
        tr.startup()
        sym = "AAAUSDT"
        self.fake.drop_order_response = 1
        self.fake.hide_order_lookups = 50  # never visible within the bot's polling window
        sig = fake_signal(tr, sym)
        ok = tr.enter(sym, sig, tr.broker.equity())
        self.assertFalse(ok)
        self.assertEqual(len([o for o in self.fake.orders.values() if o["symbol"] == sym]), 1, "entry was re-sent")
        self.fake.hide_order_lookups = 0
        self.clock.advance(30)
        tr.step()  # reconcile finds the position and protects it
        self.assertIn(sym, tr.positions)
        self.assertEqual(len(self.fake.open_algos(sym)), 1)
        self.assertAlmostEqual(abs(self.fake.positions[sym]["amt"]), tr.positions[sym]["qty"])

    # M2 ---------------------------------------------------------------------------------------------
    def test_spot_selftest_keeps_users_coins_and_returns_funds(self):
        import fake_binance
        old_step = fake_binance.SPECS["BBBUSDT"]["step"]
        fake_binance.SPECS["BBBUSDT"]["step"] = "0.1"  # like DOGE/TRX on the real exchange (1 step << 5 USDT)
        self.addCleanup(lambda: fake_binance.SPECS["BBBUSDT"].__setitem__("step", old_step))
        tr, tg, store = self.make(mode="live")
        self.fake.spot_bal["AAA"] = 2.0  # the user's own coins
        sapi, fapi = make_spot_api(self.fake, self.clock), make_api(self.fake, self.clock)
        sapi.sync_time()
        w0 = self.fake.wallet
        out = selftest.spot_roundtrip(sapi, fapi, tr.cfg, candidates=("AAAUSDT", "BBBUSDT"))
        self.assertTrue(out["ok"], out["text"])
        self.assertEqual(self.fake.spot_bal["AAA"], 2.0, "the user's AAA was touched")
        self.assertIn("BBBUSDT", {o["symbol"] for o in self.fake.spot_orders.values()})
        left = self.fake.spot_bal.get("BBB", 0.0)
        self.assertLess(left, 0.1 + 1e-9, "only a sub-step remainder may stay behind")
        self.assertAlmostEqual(self.fake.wallet + left * self.fake.price("BBBUSDT"), w0, delta=0.1,
                               msg="test money went back to futures (minus fees and the remainder)")

    # L3 / L4 ----------------------------------------------------------------------------------------
    def test_failed_transfer_keeps_forced_resplit(self):
        tr, tg, store = self.make(mode="live")
        self.fake.restrictions["permitsUniversalTransfer"] = False
        tr.startup()
        tr.act_alloc(60)
        self.steps(tr, 2)
        self.assertTrue(tr.state.get("force_alloc"))

    def test_dust_position_can_open(self):
        plan = SS.plan_trades({"A": 4.0}, {"A": 0.06}, 100.0, 0.05, 5.25)
        self.assertEqual(plan, [("A", 5.25, False)])


if __name__ == "__main__":
    unittest.main()
