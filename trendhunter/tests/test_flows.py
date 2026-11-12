"""v2.5: outside money (deposits, withdrawals, transfers made by hand in the app).

The bot notices it within one valuation refresh and takes it out of every performance baseline, so
return / drawdown / the monthly report measure the trading and the futures guards keep their meaning.
What must NOT count as outside money: the bot's own orders, transfers and Earn moves.
"""
import logging
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import bot as B  # noqa: E402
import report as RP  # noqa: E402
from binance_api import NetworkError  # noqa: E402
from fake_binance import FakeExchange, SimClock  # noqa: E402
from test_live_flow import make_trader  # noqa: E402
from test_spot import START  # noqa: E402  (02:10 UTC: the daily spot job is due right away)

logging.basicConfig(level=logging.ERROR)
DAY = 86400


def say(tr, text):
    tr.handle_update({"message": {"chat": {"id": 42}, "text": text}})


def press_ok(tr, tg):
    _, kw = tg.sent[-1]
    tr.handle_update({"callback_query": {"id": "1", "data": kw["inline"][0][0][1], "message": {"chat": {"id": 42}}}})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, mode="live", days=8, over=None, **fk):
        self.clock = SimClock(START)
        self.fake = FakeExchange(self.clock, history_days=430, test_days=days + 1, **fk)
        return make_trader(self.fake, self.clock, self.tmp, mode=mode, over=over, spot=True)

    def run_for(self, tr, seconds, step=600):
        for _ in range(int(seconds / step)):
            self.clock.advance(step)
            tr.step()

    def settled(self, mode="live", over=None, **fk):
        """A trader after its first day: the wallets are split, coins are bought, nothing unusual happened."""
        tr, tg, store = self.make(mode=mode, over=over, **fk)
        tr.startup()
        self.run_for(tr, DAY)
        self.assertEqual(store.read_all("flows.jsonl"), [], "the first day's own trades and transfers are not flows")
        return tr, tg, store

    @staticmethod
    def ret(tr):
        tot = tr.total_equity()[0]
        return tot / tr.state["total"]["base"]

    def usdt(self):
        return self.fake.spot_bal.get("USDT", 0.0)


class OwnMoves(Base):
    def test_trades_transfers_and_earn_are_never_flows(self):
        tr, tg, store = self.make(over={"spot_earn": True})
        tr.startup()
        self.run_for(tr, 6 * DAY)
        self.assertTrue(store.read_all("spot_trades.jsonl"))
        self.assertTrue(store.read_all("transfers.jsonl"))
        self.assertTrue(store.read_all("earn.jsonl"))
        self.assertEqual(store.read_all("flows.jsonl"), [])
        self.assertFalse(any("입금" in t and "감지" in t or "출금 감지" in t or "직접 옮긴" in t for t in tg.texts()))
        self.assertAlmostEqual(float(tr.state["total"].get("net_flow") or 0.0), 0.0)

    def test_alloc_change_and_preset_are_not_flows(self):
        tr, tg, store = self.settled()
        tr.act_alloc(80.0)
        self.run_for(tr, 3600)
        tr.act_preset("공격")
        self.run_for(tr, DAY)
        self.assertGreaterEqual(len(store.read_all("transfers.jsonl")), 2)
        self.assertEqual(store.read_all("flows.jsonl"), [])

    def test_slow_earn_redemption_is_not_a_deposit(self):
        tr, tg, store = self.settled()
        E = tr.state["spot"]["earn"]
        E["transit"], E["transit_ts"] = 20.0, self.clock()  # redeemed, but it had not landed when the bot stopped waiting
        self.fake.outside_deposit(20.0)                      # ... and now it lands
        self.run_for(tr, 1200)
        self.assertEqual(store.read_all("flows.jsonl"), [])
        self.assertAlmostEqual(E["transit"], 0.0)
        self.fake.outside_deposit(20.0)  # the same amount again is real new money
        self.run_for(tr, 1200)
        self.assertEqual([f["net"] for f in store.read_all("flows.jsonl")], [20.0])


    def test_usdt_moved_between_wallet_and_earn_by_hand_is_not_a_flow(self):
        tr, tg, store = self.make(over={"spot_earn": True})
        tr.startup()
        self.run_for(tr, DAY)
        parked = self.fake.earn_usdt
        self.assertGreater(parked, 3.0, "the daily job parked the idle USDT")
        r0 = self.ret(tr)
        x = round(parked * 0.6, 2)
        self.fake.earn_usdt -= x  # the user redeems in the app ...
        self.fake.outside_deposit(x)
        self.run_for(tr, 1200)
        self.assertEqual(store.read_all("flows.jsonl"), [], "Earn -> wallet is the same money")
        self.assertAlmostEqual(self.ret(tr), r0, delta=0.01)
        self.fake.outside_deposit(-x)  # ... and puts it back
        self.fake.earn_usdt += x
        self.run_for(tr, 1200)
        self.assertEqual(store.read_all("flows.jsonl"), [])
        self.fake.earn_usdt -= x  # redeemed AND withdrawn from the exchange between two looks
        self.run_for(tr, 2 * 3600)  # the wallet shows no change: noticed at the hourly look at Earn
        self.assertEqual([f["net"] for f in store.read_all("flows.jsonl")], [-x])
        self.assertAlmostEqual(self.ret(tr), r0, delta=0.02, msg="a withdrawal out of Earn is not a loss")

    def test_own_earn_usdt_counted_from_now_on_is_a_deposit_not_a_profit(self):
        tr, tg, store = self.settled()
        r0 = self.ret(tr)
        self.fake.earn_usdt = 30.0  # the user's own flexible USDT, so far outside the bot's books
        say(tr, "/set spot_earn true")
        self.run_for(tr, DAY)
        flows = store.read_all("flows.jsonl")
        self.assertEqual(len(flows), 1)
        self.assertAlmostEqual(flows[0]["net"], 30.0, delta=0.05)
        self.assertAlmostEqual(self.ret(tr), r0, delta=0.05)


class Detection(Base):
    def test_deposit_is_noticed_and_taken_out_of_the_return(self):
        tr, tg, store = self.settled()
        r0, tot0 = self.ret(tr), tr.total_equity()[0]
        base0, in0 = tr.state["total"]["base"], tr.state["spot"]["net_in"]
        self.fake.outside_deposit(50.0)
        self.run_for(tr, 1200)
        flows = store.read_all("flows.jsonl")
        self.assertEqual(len(flows), 1)
        self.assertEqual((flows[0]["spot"], flows[0]["fut"], flows[0]["net"], flows[0]["src"]), (50.0, 0.0, 50.0, "auto"))
        self.assertAlmostEqual(tr.total_equity()[0], tot0 + 50.0, delta=tot0 * 0.02)
        self.assertAlmostEqual(self.ret(tr), r0, delta=0.01, msg="a deposit is not a profit")
        self.assertGreater(tr.state["total"]["base"], base0 * 1.3)
        self.assertAlmostEqual(tr.state["total"]["net_flow"], 50.0)
        self.assertAlmostEqual(tr.state["spot"]["net_in"], in0 + 50.0, delta=0.01)
        self.assertGreater(tr.state["total"]["mdd"], -5.0, "no phantom drawdown from the scaled peak")
        self.assertTrue(any(t.startswith("💵 입금 감지 — $50.00") for t in tg.texts()))
        # the next daily check splits the new money to the target and that transfer is not a flow
        n = len(store.read_all("transfers.jsonl"))
        self.run_for(tr, DAY)
        moves = store.read_all("transfers.jsonl")[n:]
        self.assertTrue(moves and moves[0]["to"] == "futures")
        self.assertAlmostEqual(moves[0]["amount"], 25.0, delta=5.0)
        self.assertEqual(len(store.read_all("flows.jsonl")), 1)
        tot, sv, fv = tr.total_equity()
        self.assertAlmostEqual(sv / tot, 0.5, delta=0.06)
        self.assertAlmostEqual(self.ret(tr), r0, delta=0.05, msg="a day of market moves, not +50%")

    def test_withdrawal_and_coin_deposit(self):
        tr, tg, store = self.settled()
        r0 = self.ret(tr)
        out = round(min(self.usdt() * 0.8, 8.0), 2)
        self.assertGreater(out, 1.5)
        self.fake.outside_deposit(-out)
        self.run_for(tr, 1200)
        self.assertEqual([f["net"] for f in store.read_all("flows.jsonl")], [-out])
        self.assertTrue(any(t.startswith("💵 출금 감지") for t in tg.texts()))
        self.assertAlmostEqual(self.ret(tr), r0, delta=0.01, msg="a withdrawal is not a loss")
        px = self.fake.price("AAAUSDT")
        self.fake.outside_deposit(0.1, asset="AAA")  # coins sent in from another exchange
        self.run_for(tr, 1200)
        flows = store.read_all("flows.jsonl")
        self.assertEqual(len(flows), 2)
        self.assertAlmostEqual(flows[1]["net"], 0.1 * px, delta=0.1 * px * 0.03)
        self.assertAlmostEqual(self.ret(tr), r0, delta=0.015)

    def test_money_moved_between_the_wallets_by_hand_nets_to_zero(self):
        tr, tg, store = self.settled()
        base0, in0, start0 = tr.state["total"]["base"], tr.state["spot"]["net_in"], tr.ledger["start_equity"]
        fv0 = tr.state["equity"]
        x = round(min(self.usdt() * 0.8, 10.0), 2)
        self.assertGreater(x, 1.5)
        self.fake.app_transfer(x, to_futures=True)
        self.run_for(tr, 1200)
        flows = store.read_all("flows.jsonl")
        self.assertEqual(len(flows), 1)
        self.assertEqual((flows[0]["spot"], flows[0]["fut"], flows[0]["net"]), (-x, x, 0.0))
        self.assertEqual(tr.state["total"]["base"], base0, "total return is untouched")
        self.assertAlmostEqual(float(tr.state["total"].get("net_flow") or 0.0), 0.0)
        self.assertAlmostEqual(tr.state["spot"]["net_in"], in0 - x, delta=0.01)
        self.assertAlmostEqual(tr.ledger["start_equity"] / start0, (fv0 + x) / fv0, delta=0.02,
                               msg="the futures floor baseline follows the money")
        self.assertTrue(any(t.startswith("↔️ 앱에서 직접 옮긴") for t in tg.texts()))

    def test_outside_money_into_the_futures_wallet(self):
        tr, tg, store = self.settled()
        r0, start0, fv0 = self.ret(tr), tr.ledger["start_equity"], tr.state["equity"]
        tr.ledger["peak_equity"] = fv0  # the futures wallet stands at its peak
        tr.state["total"]["peak"] = tr.total_equity()[0]
        tr.flow_ckpt = tr._peaks()
        self.fake.outside_futures(20.0)
        self.run_for(tr, 1200)
        flows = store.read_all("flows.jsonl")
        self.assertEqual([(f["spot"], f["fut"], f["net"]) for f in flows], [(0.0, 20.0, 20.0)])
        self.assertAlmostEqual(self.ret(tr), r0, delta=0.01)
        self.assertAlmostEqual(tr.ledger["start_equity"] / start0, (fv0 + 20.0) / fv0, delta=0.02)
        fv = tr.state["equity"]
        self.assertGreater((fv - tr.ledger["peak_equity"]) / tr.ledger["peak_equity"], -0.02,
                           "the peak must not be pushed up twice (once by the money, once by the scaling)")
        tot = tr.total_equity()[0]
        self.assertGreater((tot - tr.state["total"]["peak"]) / tr.state["total"]["peak"], -0.02)

    def test_everything_withdrawn_then_fresh_money_starts_a_new_baseline(self):
        tr, tg, store = self.settled()
        tot, sv, fv = tr.total_equity()
        tr.state["equity"], tr.spot.cache["equity"] = 0.0, 0.0  # both wallets emptied
        tr.book_flow(self.clock(), -sv, -fv, "manual")
        self.assertEqual(tr.state["total"]["base"], 0.0, "no -100% 'return' for taking the money out")
        say(tr, "/pnl")
        self.assertNotIn("처리 중 오류", tg.texts()[-1])
        tr.state["equity"] = 80.0
        tr.book_flow(self.clock(), 0.0, 80.0, "manual")
        self.assertEqual(tr.state["total"]["base"], 80.0)
        self.assertEqual(tr.ledger["start_equity"], 80.0)

    def test_small_noise_is_ignored_and_does_not_pile_up(self):
        tr, tg, store = self.settled()
        for _ in range(5):
            self.fake.outside_deposit(0.3)  # bonus interest paid into the wallet, dust ...
            self.run_for(tr, 1200)
        self.assertEqual(store.read_all("flows.jsonl"), [])
        self.assertEqual(tr.state["spot"]["flow_acc"], 0.0)

    def test_switched_off(self):
        tr, tg, store = self.settled(over={"flow_detect": False})
        self.fake.outside_deposit(50.0)
        self.run_for(tr, 3600)
        self.assertEqual(store.read_all("flows.jsonl"), [])

    def test_unreadable_transfer_history_delays_the_booking(self):
        tr, tg, store = self.settled()
        real = tr.api.income

        def down(symbol=None, income_type=None, **kw):
            if income_type == "TRANSFER":
                raise NetworkError("income history unavailable")
            return real(symbol=symbol, income_type=income_type, **kw)

        tr.api.income = down
        self.fake.outside_deposit(50.0)
        self.run_for(tr, 1200)
        self.assertEqual(store.read_all("flows.jsonl"), [], "both wallets are judged together or not at all")
        self.assertAlmostEqual(tr.state["spot"]["flow_acc"], 50.0)
        tr.api.income = real
        self.clock.advance(20)
        tr.step()
        self.assertEqual([f["net"] for f in store.read_all("flows.jsonl")], [50.0])

    def test_waits_while_a_transfer_of_the_bot_is_unconfirmed(self):
        tr, tg, store = self.settled()
        self.fake.outside_deposit(50.0)
        tr.state["pending_transfer"] = {"amount": 5.0}
        self.assertIsNone(tr.flow_pass(self.clock(), force=True))
        self.assertEqual(store.read_all("flows.jsonl"), [])
        tr.state["pending_transfer"] = None
        self.assertEqual(tr.flow_pass(self.clock(), force=True)["net"], 50.0)

    def test_deposit_while_the_bot_was_down(self):
        tr, tg, store = self.settled()
        r0 = self.ret(tr)
        tr.save()
        self.fake.outside_deposit(40.0)
        tr2, tg2, store2 = make_trader(self.fake, self.clock, self.tmp, spot=True)
        tr2.startup()
        self.clock.advance(20)
        tr2.step()
        self.assertEqual([f["net"] for f in store2.read_all("flows.jsonl")], [40.0])
        self.assertAlmostEqual(self.ret(tr2), r0, delta=0.01)

    def test_restart_right_after_an_own_transfer_is_not_a_flow(self):
        tr, tg, store = self.settled()
        tr.act_alloc(70.0)  # the next step moves money; pretend the process dies before it looks at the wallet again
        self.clock.advance(20)
        real = tr.record_daily
        tr.record_daily = lambda now: None
        try:
            tr.step()
        finally:
            tr.record_daily = real
        self.assertTrue(store.read_all("transfers.jsonl")[-1]["why"])
        tr.save()
        tr2, tg2, store2 = make_trader(self.fake, self.clock, self.tmp, spot=True)
        tr2.startup()
        self.run_for(tr2, 3600)
        self.assertEqual(store2.read_all("flows.jsonl"), [])


class Commands(Base):
    def test_rebase_after_a_deposit_does_not_book_it_again(self):
        tr, tg, store = self.settled()
        self.fake.outside_deposit(30.0)
        tr.cmd_rebase([])
        tot = tr.total_equity()[0]
        self.assertAlmostEqual(tr.state["total"]["base"], tot)
        self.run_for(tr, 3600)
        self.assertEqual(store.read_all("flows.jsonl"), [])
        self.assertAlmostEqual(self.ret(tr), 1.0, delta=0.02)

    def test_manual_command_does_not_double_book_what_was_detected(self):
        tr, tg, store = self.settled()
        self.fake.outside_deposit(25.0)
        say(tr, "/deposit 25")
        self.assertEqual([(f["net"], f["src"]) for f in store.read_all("flows.jsonl")], [(25.0, "auto")])
        self.assertIn("자동으로 반영", tg.texts()[-1])
        self.run_for(tr, 3600)
        self.assertEqual(len(store.read_all("flows.jsonl")), 1)

    def test_manual_record_and_undo(self):
        tr, tg, store = self.settled()
        base0, in0, r0 = tr.state["total"]["base"], tr.state["spot"]["net_in"], self.ret(tr)
        say(tr, "/deposit 10")
        self.assertIn("자동으로 감지된 입금이 없습니다", tg.texts()[-1])
        self.assertEqual(store.read_all("flows.jsonl"), [], "nothing is booked before the confirmation")
        press_ok(tr, tg)
        self.assertEqual([(f["net"], f["src"]) for f in store.read_all("flows.jsonl")], [(10.0, "manual")])
        self.assertGreater(tr.state["total"]["base"], base0)
        say(tr, "/flows")
        self.assertIn("수동", tg.texts()[-1])
        say(tr, "/flows undo")
        press_ok(tr, tg)
        self.assertAlmostEqual(tr.state["total"]["base"], base0, delta=base0 * 1e-9)
        self.assertAlmostEqual(tr.state["spot"]["net_in"], in0, delta=1e-6)
        self.assertAlmostEqual(float(tr.state["total"].get("net_flow") or 0.0), 0.0)
        self.assertAlmostEqual(self.ret(tr), r0, delta=1e-6)
        self.assertEqual([f["src"] for f in store.read_all("flows.jsonl")], ["manual", "undo"])
        self.assertAlmostEqual(RP.window_flows(store.read_all("flows.jsonl"), "2000-01-01", "2100-01-01"), 0.0)
        say(tr, "/flows undo")
        self.assertIn("취소할 기록이 없습니다", tg.texts()[-1])
        for bad in ("/deposit", "/deposit abc", "/withdraw -5", "/deposit 0"):
            say(tr, bad)
            self.assertIn("사용법", tg.texts()[-1], bad)

    def test_undo_of_a_futures_side_flow_restores_the_guard_baselines(self):
        tr, tg, store = self.settled()
        L0 = dict(tr.ledger)
        self.fake.outside_futures(20.0)
        self.run_for(tr, 1200)
        self.assertGreater(tr.ledger["start_equity"], L0["start_equity"] * 1.1)
        say(tr, "/flows undo")
        press_ok(tr, tg)
        self.assertAlmostEqual(tr.ledger["start_equity"], L0["start_equity"], delta=L0["start_equity"] * 1e-6)

    def test_paper_deposit_moves_virtual_money_and_keeps_the_return(self):
        tr, tg, store = self.settled(mode="paper")
        self.fake.outside_deposit(500.0)  # the real account is none of the paper bot's business
        self.run_for(tr, 3600)
        self.assertEqual(store.read_all("flows.jsonl"), [])
        r0, tot0 = self.ret(tr), tr.total_equity()[0]
        say(tr, "/deposit 40")
        self.assertIn("모의 모드", tg.texts()[-1])
        press_ok(tr, tg)
        self.assertAlmostEqual(tr.total_equity()[0], tot0 + 40.0, delta=0.5)
        self.assertAlmostEqual(self.ret(tr), r0, delta=0.005)
        self.assertEqual([(f["net"], f["src"], f["mode"]) for f in store.read_all("flows.jsonl")], [(40.0, "manual", "paper")])
        say(tr, "/withdraw 100000")
        press_ok(tr, tg)
        self.assertIn("그만큼 없습니다", tg.texts()[-1])
        self.assertEqual(len(store.read_all("flows.jsonl")), 1)
        self.assertEqual(self.fake.transfers, [], "paper mode never moves real money")
        say(tr, "/withdraw 5 선물")
        press_ok(tr, tg)
        self.assertEqual([(f["spot"], f["fut"]) for f in store.read_all("flows.jsonl")][-1], (0.0, -5.0))
        self.assertAlmostEqual(self.ret(tr), r0, delta=0.005)

    def test_mode_switch_starts_watching_afresh(self):
        tr, tg, store = self.settled(mode="paper")
        self.fake.outside_deposit(60.0)  # already in the real spot wallet when the user goes live
        tr.act_go_live()
        self.run_for(tr, 3600)
        self.assertEqual([f for f in store.read_all("flows.jsonl") if f["mode"] == "live"], [],
                         "what is in the wallets at /live is the starting capital, not a deposit")
        self.fake.outside_deposit(15.0)
        self.run_for(tr, 1200)
        self.assertEqual([f["net"] for f in store.read_all("flows.jsonl") if f["mode"] == "live"], [15.0])


class Reporting(Base):
    def test_daily_rows_carry_the_unit_price(self):
        tr, tg, store = self.settled()
        row = tr.state["hist"][-1]
        self.assertEqual(len(row), 4)
        self.assertAlmostEqual(row[3], (row[1] + row[2]) / tr.state["total"]["base"], places=5)
        self.fake.outside_deposit(50.0)
        self.run_for(tr, DAY)
        new = tr.state["hist"][-1]
        self.assertAlmostEqual(new[3], row[3], delta=0.05, msg="the unit price does not jump with a deposit")
        self.assertGreater(new[1] + new[2], row[1] + row[2] + 40.0)

    def test_monthly_report_uses_the_unit_price(self):
        start, end = RP.month_start(1_761_955_200 - 1), 1_761_955_200  # October 2025 (UTC)
        hist = [["2025-10-01", 50.0, 50.0, 1.0], ["2025-10-15", 105.0, 100.0, 1.05], ["2025-10-31", 110.0, 100.0, 1.10]]
        flows = [{"ts": start + 10 * DAY, "net": 100.0}, {"ts": start - DAY, "net": 999.0}]
        text = RP.monthly_text("2025년 10월", "실전(LIVE)", hist, start, end - 1, [], [], 0, None, flows)
        self.assertIn("총자산 $100.00 → $210.00 | 수익률 +10.0%", text)
        self.assertIn("순입금 +$100.00", text)
        self.assertNotIn("섞일 수 있습니다", text)
        # no outside money in the window: no deposit line
        text = RP.monthly_text("2025년 10월", "실전(LIVE)", hist, start, end - 1, [], [], 0, None, [])
        self.assertNotIn("순입금", text)
        # rows written before v2.5 (no unit price): the old wording and the old warning
        old = [h[:3] for h in hist]
        text = RP.monthly_text("2025년 10월", "실전(LIVE)", old, start, end - 1, [], [], 0)
        self.assertIn("총자산 $100.00 → $210.00 (+110.0%)", text)
        self.assertIn("섞일 수 있습니다", text)
        mixed = [hist[0][:3], hist[2]]
        self.assertIn("섞일 수 있습니다", RP.monthly_text("2025년 10월", "실전(LIVE)", mixed, start, end - 1, [], [], 0))

    def test_pnl_and_report_commands_after_a_deposit(self):
        tr, tg, store = self.settled()
        self.fake.outside_deposit(50.0)
        self.run_for(tr, 1200)
        for text in ("/pnl", "/status", "/flows", "/report 이번달", "/settings", "/help"):
            say(tr, text)
            self.assertNotIn("처리 중 오류", tg.texts()[-1], text)
        say(tr, "/pnl")
        self.assertIn("순입금 +$50.00 제외", tg.texts()[-1])
        say(tr, "/flows")
        self.assertIn("자동", tg.texts()[-1])

    def test_config_check_and_setting(self):
        tmp = tempfile.mkdtemp()
        try:
            self.assertEqual(B.check(tmp), 0)
            import json
            with open(os.path.join(tmp, "settings.json"), "w", encoding="utf-8") as fh:
                json.dump({"flow_detect": "yes"}, fh)
            with self.assertRaises(SystemExit):
                B.check(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        tr, tg, store = self.settled()
        say(tr, "/set flow_detect false")
        self.assertFalse(tr.cfg["flow_detect"])


if __name__ == "__main__":
    unittest.main()
