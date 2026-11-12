"""v2.4: pyramiding (add-ons to locked winners), the tight-margin leverage rule, the candle retry,
the liquidation warning and the one-time update note.

The money rules checked here:
- an add-on never goes to a position whose stop is still below its average entry
- an add-on with an unknown outcome is never sent twice
- after an add-on the position still has exactly ONE exchange stop, for the whole quantity
- paper mode never touches the real account
"""
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
import bot as B  # noqa: E402
import risk as R  # noqa: E402
import strategy as S  # noqa: E402
from binance_api import NetworkError  # noqa: E402
from fake_binance import FakeExchange, SimClock, make_api  # noqa: E402
import test_live_flow as TLF  # noqa: E402  (module import: its test classes must not be collected twice)
from test_live_flow import START, fake_signal, make_trader  # noqa: E402

logging.basicConfig(level=logging.ERROR)
with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
    CFG = json.load(fh)
PYR = dict(CFG, pyramid_max_adds=2, pyramid_step_r=1.5, pyramid_risk_frac=1.0, risk_per_trade_pct=2.0)
F = {"tick": "0.01", "step": "0.001", "min_qty": 0.001, "max_qty": 100000, "min_notional": 5}
TF = 14400


def long_pos(**kw):
    p = {"side": S.LONG, "qty": 10.0, "entry": 100.0, "stop": 101.0, "lev": 10, "r_dist": 2.0,
         "last_add_px": 100.0, "adds": 0}
    p.update(kw)
    return p


class LeverageRule(unittest.TestCase):
    def test_tight_margin_keeps_size_and_cuts_the_money_at_stake(self):
        new = R.size_position(1000, 1000, 100.0, 2.0, CFG, F)
        old = R.size_position(1000, 1000, 100.0, 2.0, dict(CFG, liq_buffer_mult=0), F)
        self.assertEqual(new["qty"], old["qty"])
        self.assertEqual(new["risk_amt"], old["risk_amt"])
        self.assertGreater(new["lev"], old["lev"])
        self.assertLess(new["margin"], old["margin"] * 0.6, "isolated margin = the most a gap through the stop can cost")
        self.assertTrue(R.liquidation_buffer_ok(new["lev"], 2.0, buf=2.0))

    def test_wide_stop_lowers_the_leverage(self):
        sz = R.size_position(1000, 1000, 100.0, 6.0, dict(CFG, max_leverage=20), F)  # 6% stop
        self.assertTrue(sz["ok"])
        self.assertEqual(sz["lev"], 7)  # 1 / (2 x 6% + 1%)
        self.assertTrue(R.liquidation_buffer_ok(sz["lev"], 6.0, buf=2.0))
        self.assertFalse(R.liquidation_buffer_ok(sz["lev"] + 1, 6.0, buf=2.0))

    def test_missing_key_means_the_old_rule(self):
        cfg = {k: v for k, v in CFG.items() if k != "liq_buffer_mult"}
        self.assertEqual(R.size_position(100, 100, 100.0, 2.0, cfg, F)["lev"], 5)


class AddonRules(unittest.TestCase):
    def test_due_only_for_a_locked_winner_that_moved_on(self):
        self.assertTrue(S.addon_due(long_pos(), 103.0, PYR))          # 100 + 1.5 x 2
        self.assertFalse(S.addon_due(long_pos(), 102.9, PYR))
        self.assertFalse(S.addon_due(long_pos(stop=99.9), 110.0, PYR), "stop below the average entry: not locked")
        self.assertFalse(S.addon_due(long_pos(adds=2), 110.0, PYR))
        self.assertFalse(S.addon_due(long_pos(adopted=True), 110.0, PYR))
        self.assertFalse(S.addon_due(long_pos(), 110.0, dict(PYR, pyramid_max_adds=0)))
        self.assertFalse(S.addon_due(long_pos(), 110.0, {k: v for k, v in PYR.items() if k != "pyramid_max_adds"}))
        self.assertTrue(S.addon_due(long_pos(last_add_px=None), 103.0, PYR), "older positions fall back to the entry")
        self.assertFalse(S.addon_due(long_pos(last_add_px=104.0), 106.9, PYR), "measured from the LAST add")

    def test_due_short_mirror(self):
        sh = long_pos(side=S.SHORT, stop=99.5)
        self.assertTrue(S.addon_due(sh, 97.0, PYR))
        self.assertFalse(S.addon_due(sh, 97.1, PYR))
        self.assertFalse(S.addon_due(dict(sh, stop=100.5), 90.0, PYR))

    def test_size_risks_the_target_to_the_current_stop(self):
        sz = R.size_addon(1000, 1000, long_pos(), 106.0, PYR, F)  # 2% of 1000 over a $5 gap
        self.assertTrue(sz["ok"])
        self.assertAlmostEqual(sz["qty"], 4.0)
        self.assertAlmostEqual(sz["risk_amt"], 20.0)
        self.assertAlmostEqual(sz["margin"], 4.0 * 106.0 / 10)
        half = R.size_addon(1000, 1000, long_pos(), 106.0, dict(PYR, pyramid_risk_frac=0.5), F)
        self.assertAlmostEqual(half["qty"], 2.0)

    def test_never_bigger_than_a_fresh_entry(self):
        sz = R.size_addon(1000, 1000, long_pos(), 101.5, PYR, F)  # stop only $0.5 away
        self.assertAlmostEqual(sz["qty"], 10.0)       # sized on the first unit's $2 stop, not on $0.5
        self.assertAlmostEqual(sz["risk_amt"], 5.0)   # so the real risk is smaller than the target

    def test_margin_budget_covers_the_whole_pyramid(self):
        sz = R.size_addon(1000, 1000, long_pos(qty=28.0), 106.0, PYR, F)  # 280 of the 300 budget already posted
        self.assertTrue(sz["capped"])
        self.assertLessEqual(28.0 * 100 / 10 + sz["margin"], 300.0 + 1e-6)
        poor = R.size_addon(1000, 10, long_pos(), 106.0, PYR, F)
        self.assertLessEqual(poor["margin"], 9.5 + 1e-6)
        self.assertEqual(R.size_addon(1000, 1000, long_pos(qty=30.0), 106.0, PYR, F)["code"], "NO_MARGIN")

    def test_liquidation_stays_beyond_the_stop(self):
        pos = long_pos(stop=100.0)
        sz = R.size_addon(10000, 10000, pos, 130.0, PYR, F)  # a wide trail far above the average entry
        self.assertTrue(sz["ok"] and sz["capped"])
        avg = (pos["qty"] * pos["entry"] + sz["qty"] * 130.0) / (pos["qty"] + sz["qty"])
        allowed = (1.0 / pos["lev"] - R.MMR) / 2.0
        self.assertLessEqual((avg - pos["stop"]) / avg, allowed + 1e-9)
        self.assertEqual(R.size_addon(1000, 1000, long_pos(stop=90.0), 106.0, PYR, F)["code"], "LIQ_TOO_CLOSE")

    def test_refusals(self):
        self.assertEqual(R.size_addon(1000, 1000, long_pos(), 101.0, PYR, F)["code"], "PAST_STOP")
        self.assertEqual(R.size_addon(10, 10, long_pos(qty=0.1), 106.0, PYR, F)["code"], "SIZE_TOO_SMALL")
        self.assertEqual(R.size_addon(1000, 1000, long_pos(lev=None), 106.0, PYR, F)["code"], "BAD_INPUT")

    def test_size_short_mirror(self):
        sz = R.size_addon(1000, 1000, long_pos(side=S.SHORT, stop=99.0), 94.0, PYR, F)
        self.assertAlmostEqual(sz["qty"], 4.0)
        self.assertAlmostEqual(sz["risk_amt"], 20.0)

    def test_account_guards_apply_to_adds(self):
        now = 1_700_000_000
        L = R.new_ledger(100.0, now)
        self.assertEqual(R.can_add(L, now, 100, CFG), (True, "OK"))
        self.assertEqual(R.can_add(L, now, 40, CFG)[1], "EQUITY_FLOOR")
        self.assertEqual(R.can_add(L, now, 100, CFG, paused=True)[1], "PAUSED")
        self.assertEqual(R.can_add(L, now, 100, CFG, blocked=True)[1], "BLOCKED")
        for sym in "XYZ":
            R.on_exit(L, sym, -3.0, now, CFG)
        self.assertEqual(R.can_add(L, now + 60, 91, CFG)[1], "DAILY_LIMIT")


class BacktestAdds(unittest.TestCase):
    def setUp(self):
        clock = SimClock(START)
        fake = FakeExchange(clock, direction=1, history_days=420, test_days=1)
        self.cfg = dict(CFG, symbols=list(fake.symbols), pyramid_max_adds=2)
        data, self.start = backtest.load_data(make_api(fake, clock), self.cfg, 250, None, int(START * 1000))
        self.pre = backtest.precompute(self.cfg, data)

    def test_adds_are_booked_consistently(self):
        on = backtest.simulate(self.cfg, self.pre, self.start, 2.0, 1000.0)
        self.assertGreater(sum(t["adds"] for t in on["trades"]), 0, "an up-trend must produce add-ons")
        self.assertLessEqual(max(t["adds"] for t in on["trades"]), 2)
        self.assertAlmostEqual(on["final"] - 1000.0, sum(t["pnl"] for t in on["trades"]), delta=1e-6)
        self.assertGreater(min(e for _, e in on["curve"]), 0)

    def test_off_is_the_old_behaviour_and_leverage_does_not_change_pnl(self):
        off = backtest.simulate(dict(self.cfg, pyramid_max_adds=0), self.pre, self.start, 2.0, 1000.0)
        old = backtest.simulate(dict(self.cfg, pyramid_max_adds=0, liq_buffer_mult=0), self.pre, self.start, 2.0, 1000.0)
        self.assertEqual(sum(t["adds"] for t in off["trades"]), 0)
        self.assertEqual(len(off["trades"]), len(old["trades"]))
        self.assertAlmostEqual(off["final"], old["final"], delta=1e-6)
        self.assertTrue(all(a["lev"] >= b["lev"] for a, b in zip(off["trades"], old["trades"])))

    def test_report_says_when_pyramiding_is_on(self):
        res = {2.0: backtest.simulate(self.cfg, self.pre, self.start, 2.0, 100.0)}
        text = backtest.format_report(dict(self.cfg, risk_per_trade_pct=2.0), 250, self.start, int(START * 1000), res, 2.0, 3)
        self.assertIn("불타기 최대 2회", text)
        self.assertNotIn("불타기", backtest.format_report(dict(self.cfg, pyramid_max_adds=0), 250, self.start,
                                                        int(START * 1000), res, 2.0, 3))


class LiveAdds(unittest.TestCase):
    SYM = "AAAUSDT"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.clock = SimClock(START)
        self.fake = FakeExchange(self.clock, direction=1, test_days=3, history_days=260)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def started(self, mode="live", over=None):
        tr, tg, store = make_trader(self.fake, self.clock, self.tmp, mode=mode,
                                    over=dict({"pyramid_max_adds": 2}, **(over or {})))
        tr.startup()
        tr.marks = tr.api.mark_prices()
        return tr, tg, store

    def winner(self, tr, lock=True):
        """Open a position and turn it into a winner that is due for an add: as if it had been bought 10% lower
        and the stop had trailed to 5% under the price (or, lock=False, were still under the entry)."""
        sym = self.SYM
        self.assertTrue(tr.enter(sym, fake_signal(tr, sym), tr.broker.equity()))
        pos = tr.positions[sym]
        m = tr.marks[sym]
        pos.update(last_add_px=m * 0.90, r_dist=m * 0.01, extreme=m)
        if lock:
            pos["entry"] = m * 0.90
            self.assertTrue(tr.replace_stop(sym, pos, round(m * 0.95, 2), note=False))
        self.assertEqual(S.is_locked(pos["side"], pos["entry"], pos["stop"]), lock)
        return pos

    def orders(self):
        return [o for o in self.fake.orders.values() if o["symbol"] == self.SYM]

    def assert_one_full_stop(self, tr):
        algos = self.fake.open_algos(self.SYM)
        self.assertEqual(len(algos), 1, "exactly one exchange stop")
        self.assertAlmostEqual(float(algos[0]["quantity"]), tr.positions[self.SYM]["qty"], places=9)
        self.assertAlmostEqual(float(algos[0]["quantity"]), abs(self.fake.positions[self.SYM]["amt"]), places=9)
        self.assertAlmostEqual(float(algos[0]["triggerPrice"]), tr.positions[self.SYM]["stop"], places=6)

    def test_add_grows_the_position_and_resizes_the_stop(self):
        tr, tg, store = self.started()
        pos = self.winner(tr)
        q0, stop0 = pos["qty"], pos["stop"]
        tr.try_addons(self.clock())
        self.assertEqual(pos["adds"], 1)
        self.assertGreater(pos["qty"], q0)
        self.assertEqual(pos["stop"], stop0, "an add never moves the stop")
        self.assert_one_full_stop(tr)
        self.assertEqual(len(self.orders()), 2)
        self.assertTrue(any(t.startswith("➕ 불타기 1/2") for t in tg.texts()))
        ev = [e for e in store.read_all("events.jsonl") if e["kind"] == "ADD"]
        self.assertEqual(len(ev), 1)
        # the add risks about risk% of equity down to the stop (lot rounding only makes it smaller)
        add_risk = ev[0]["qty"] * (ev[0]["entry"] - stop0)
        self.assertLessEqual(add_risk, 100 * tr.cfg["risk_per_trade_pct"] / 100 * 1.05)
        # one add per candle, however often the check runs
        pos["last_add_px"] = pos["entry"] * 0.5
        tr.try_addons(self.clock())
        self.assertEqual(len(self.orders()), 2)
        # and the journal knows about the add when the position is closed
        tr.act_closeall()
        self.assertEqual(self.fake.positions, {})
        self.assertEqual(self.fake.open_algos(), [])
        j = store.read_all("journal.jsonl")[-1]
        self.assertEqual(j["adds"], 1)

    def test_no_add_without_a_locked_stop(self):
        tr, tg, store = self.started()
        self.winner(tr, lock=False)
        tr.try_addons(self.clock())
        self.assertEqual(len(self.orders()), 1)

    def test_no_add_when_switched_off_paused_or_legacy_position(self):
        tr, tg, store = self.started(over={"pyramid_max_adds": 0})
        pos = self.winner(tr)
        tr.try_addons(self.clock())
        self.assertEqual(len(self.orders()), 1)
        tr.cfg["pyramid_max_adds"] = 2
        tr.state["paused"] = True
        tr.try_addons(self.clock())
        self.assertEqual(len(self.orders()), 1)
        self.assertTrue(any(e.get("add") and e.get("code") == "PAUSED" for e in store.read_all("events.jsonl")))
        tr.state["paused"] = False
        pos["lev"] = None  # adopted / unknown leverage: never pyramided
        tr.try_addons(self.clock())
        self.assertEqual(len(self.orders()), 1)

    def test_add_with_unknown_outcome_is_never_sent_twice(self):
        tr, tg, store = self.started()
        pos = self.winner(tr)
        q0 = pos["qty"]
        self.fake.drop_order_response = 1
        self.fake.hide_order_lookups = 50  # the order exists but stays invisible for the whole polling window
        tr.try_addons(self.clock())
        self.assertEqual(len(self.orders()), 2, "the add reached the exchange exactly once")
        self.assertEqual(pos["qty"], q0, "the bot does not know yet")
        self.assertTrue(any("추가 진입 주문 실패" in t for t in tg.texts()))
        tr.try_addons(self.clock())
        self.assertEqual(len(self.orders()), 2, "an add with an unknown outcome was re-sent")
        self.fake.hide_order_lookups = 0
        self.clock.advance(30)
        tr.step()  # reconcile: the exchange shows the bigger position
        self.assertGreater(pos["qty"], q0)
        self.assertEqual(pos["adds"], 1)
        self.assertAlmostEqual(pos["entry"], self.fake.positions[self.SYM]["entry"])
        self.assert_one_full_stop(tr)
        tr.try_addons(self.clock())
        self.assertEqual(len(self.orders()), 2)

    def test_paper_add_never_touches_the_real_account(self):
        tr, tg, store = self.started(mode="paper")
        pos = self.winner(tr)
        q0 = pos["qty"]
        tr.try_addons(self.clock())
        self.assertEqual(pos["adds"], 1)
        self.assertGreater(pos["qty"], q0)
        self.assertEqual(self.fake.positions, {})
        self.assertEqual(self.fake.orders, {})
        self.assertEqual(self.fake.open_algos(), [])
        st = tr.broker.st
        self.assertAlmostEqual(st["positions"][self.SYM]["amt"], pos["qty"])
        stops = [s for s in st["stops"].values() if s["sym"] == self.SYM]
        self.assertEqual(len(stops), 1)
        self.assertAlmostEqual(stops[0]["qty"], pos["qty"])

    def test_liquidation_before_stop_is_reported(self):
        tr, tg, store = self.started()
        self.assertTrue(tr.enter(self.SYM, fake_signal(tr, self.SYM), tr.broker.equity()))
        stop = tr.positions[self.SYM]["stop"]
        real = tr.broker.positions
        tr.broker.positions = lambda: {k: dict(v, liq=stop * 1.001) for k, v in real().items()}
        self.clock.advance(20)
        tr.step()
        self.assertTrue(any("청산가" in t and "먼저 닿는" in t for t in tg.texts()))

    def test_update_note_is_sent_once(self):
        tr, tg, store = self.started()
        self.assertFalse(any(t.startswith("🆕") for t in tg.texts()), "a fresh install needs no update note")
        st = store.load_state()
        st.pop("seen_version")  # as written by v2.3, which did not know this key
        store.save_state(st)
        tr2, tg2, _ = make_trader(self.fake, self.clock, self.tmp)
        tr2.startup()
        self.assertEqual(sum(t.startswith("🆕 v2.4") for t in tg2.texts()), 1)
        tr3, tg3, _ = make_trader(self.fake, self.clock, self.tmp)
        tr3.startup()
        self.assertFalse(any(t.startswith("🆕") for t in tg3.texts()))

    def test_presets_and_settings(self):
        tr, tg, store = self.started(over={"pyramid_max_adds": 0})
        tr.cmd_preset([])
        self.assertIn("불타기 2회", tg.texts()[-1])
        for text in ("/set pyramid_max_adds 3", "/set pyramid_step_r 2", "/set pyramid_risk_frac 0.5",
                     "/set liq_buffer_mult 3", "/settings"):
            tr.handle_update({"message": {"chat": {"id": 42}, "text": text}})
            self.assertNotIn("오류", tg.texts()[-1], text)
        self.assertEqual((tr.cfg["pyramid_max_adds"], tr.cfg["pyramid_step_r"], tr.cfg["pyramid_risk_frac"],
                          tr.cfg["liq_buffer_mult"]), (3, 2.0, 0.5, 3.0))
        tr.handle_update({"message": {"chat": {"id": 42}, "text": "/set pyramid_max_adds 9"}})
        self.assertIn("값 오류", tg.texts()[-1])

    def test_config_check_validates_the_new_keys(self):
        tmp = tempfile.mkdtemp()
        try:
            self.assertEqual(B.check(tmp), 0)
            for bad in ({"pyramid_max_adds": 4}, {"pyramid_max_adds": 1.5}, {"pyramid_max_adds": True},
                        {"pyramid_step_r": 0}, {"pyramid_risk_frac": 2}, {"liq_buffer_mult": -1},
                        {"liq_buffer_mult": "2"}):
                with open(os.path.join(tmp, "settings.json"), "w", encoding="utf-8") as fh:
                    json.dump(bad, fh)
                with self.assertRaises(SystemExit, msg=str(bad)):
                    B.check(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class CandleRetry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.clock = SimClock(START)
        self.fake = FakeExchange(self.clock, direction=1, test_days=3, history_days=260)
        self.tr, self.tg, self.store = make_trader(self.fake, self.clock, self.tmp)
        self.tr.startup()
        self.clock.advance((int(self.clock() // TF) + 1) * TF - self.clock() + 10)  # 10 s after a 4h close
        self.real = self.tr.api.klines
        self.seen = []
        entries = self.tr.try_entries
        self.tr.try_entries = lambda now, only=None: (self.seen.append(only), entries(now, only))[1]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def break_klines(self, how):
        def klines(sym, interval, **kw):
            if sym != "AAAUSDT":
                return self.real(sym, interval, **kw)
            if how == "down":
                raise NetworkError("simulated outage")
            rows = self.real(sym, interval, **kw)
            return rows[:-2] + rows[-1:]  # the candle that just closed is not there yet
        self.tr.api.klines = klines

    def test_failed_klines_are_retried_inside_the_entry_window(self):
        tr = self.tr
        self.break_klines("down")
        tr.step()
        self.assertEqual(self.seen, [None])
        self.assertEqual(tr.stale_symbols(), ["AAAUSDT"])
        self.assertIsNotNone(tr.candle_retry)
        tr.api.klines = self.real
        self.clock.advance(20)
        tr.step()
        self.assertEqual(len(self.seen), 1, "retries are 30 s apart")
        self.clock.advance(20)
        tr.step()
        self.assertEqual(self.seen[-1], {"AAAUSDT"}, "only the symbol that was missing gets a second look")
        self.assertTrue(tr.data["AAAUSDT"]["fresh"])
        self.clock.advance(20)
        tr.step()
        self.assertIsNone(tr.candle_retry)
        self.assertEqual(len(self.seen), 2)

    def test_late_candle_is_not_mistaken_for_the_new_one(self):
        tr = self.tr
        self.break_klines("late")
        tr.step()
        self.assertFalse(tr.data["AAAUSDT"]["fresh"], "the previous candle must not be judged as if it had just closed")
        self.assertTrue(tr.data["BBBUSDT"]["fresh"])
        tr.api.klines = self.real
        self.clock.advance(40)
        tr.step()
        self.assertTrue(tr.data["AAAUSDT"]["fresh"])

    def test_gives_up_after_the_window_and_says_so(self):
        tr = self.tr
        self.break_klines("down")
        for _ in range(12):
            tr.step()
            self.clock.advance(60)
        self.assertIsNone(tr.candle_retry)
        self.assertTrue(any("캔들 데이터를 받지 못해" in t for t in self.tg.texts()))
        n = len(self.seen)
        self.clock.advance(60)
        tr.step()
        self.assertEqual(len(self.seen), n, "no entries outside the 10-minute window")


class PyramidScenario(TLF.LiveScenario):
    """The 18-day live scenario with pyramiding on: every step still has one full-size stop per position,
    stops only tighten, and the journal matches the exchange wallet to the cent."""

    def check(self, tr, fake, last_stops):
        super().check(tr, fake, last_stops)
        for sym, p in fake.positions.items():  # the bot's average entry is the exchange's, add after add
            self.assertAlmostEqual(tr.positions[sym]["entry"], p["entry"], delta=p["entry"] * 1e-9)
            self.assertLessEqual(tr.positions[sym].get("adds", 0), 2)

    def test_uptrend_longs(self):
        tr, tg, store, fake, journal = self.run_scenario(+1, over={"pyramid_max_adds": 2})
        adds = [e for e in store.read_all("events.jsonl") if e["kind"] == "ADD"]
        self.assertGreaterEqual(len(adds), 1, "expected add-ons in a strong up-trend")
        self.assertEqual(sum(j.get("adds", 0) for j in journal), len(adds))
        self.assertLessEqual(max(j.get("adds", 0) for j in journal), 2)
        print(f"\n[uptrend+pyramid] trades={len(journal)} adds={len(adds)} pnl={sum(j['pnl'] for j in journal):+.2f}")

    def test_downtrend_shorts(self):
        tr, tg, store, fake, journal = self.run_scenario(-1, over={"allow_short": True, "pyramid_max_adds": 2})
        self.assertTrue(any(j["side"] == S.SHORT for j in journal))
        print(f"\n[downtrend+pyramid] trades={len(journal)} adds={sum(j.get('adds', 0) for j in journal)} "
              f"pnl={sum(j['pnl'] for j in journal):+.2f}")


if __name__ == "__main__":
    unittest.main()
