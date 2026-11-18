"""v2.5: outside money (deposits, withdrawals, transfers made by hand in the app).

The bot notices it (futures wallet: at the next step; spot wallet: within ten minutes) and takes it out of
every performance baseline, so return / drawdown / the monthly report measure the trading and the futures
guards keep their meaning. Until it is booked, the guards and the totals do not see the money at all.
What must NOT count as outside money: the bot's own orders, transfers and Earn moves — whenever they
happen, and also when their answer was lost.
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

    def tick(self, tr, n=1):
        """n steps of the real main loop (20 s apart)."""
        self.run_for(tr, n * 20, step=20)

    def run_to_utc(self, tr, secs, step=600):
        """Step until `secs` after the next 00:00 UTC."""
        goal = (self.clock() // DAY + 1) * DAY + secs
        while self.clock() + step <= goal:
            self.clock.advance(step)
            tr.step()

    def flows(self, store, mode="live"):
        return [f for f in store.read_all("flows.jsonl") if f.get("mode") == mode]

    def nets(self, store):
        return [f["net"] for f in self.flows(store)]

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
    """Nothing the bot does itself may ever be booked as a deposit or a withdrawal."""

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
        self.assertEqual(tr.held(), 0.0)

    def test_the_real_main_loop_through_daily_jobs_and_futures_trades(self):
        """20-second steps (the pace of the real loop) across two daily jobs, with Earn on: every window between
        an own order / transfer / Earn move and the next look at the wallets is exercised."""
        tr, tg, store = self.make(over={"spot_earn": True})
        tr.startup()
        held = []

        def ticks(n):
            for _ in range(n):
                self.tick(tr, 1)
                held.append(abs(tr.held()))

        ticks(90)  # the first job (split, buys, parking) and half an hour after it
        for alloc in (70.0, 35.0):
            self.run_to_utc(tr, 0)
            tr.act_alloc(alloc)  # the job due in five minutes has to move money, trade and redeem / park
            ticks(60)
        self.assertEqual(max(held), 0.0, "the bot's own transfers are never kept out of the guards' sight")
        self.assertGreaterEqual(len(store.read_all("transfers.jsonl")), 3)
        self.assertGreaterEqual(len(store.read_all("earn.jsonl")), 2)
        self.assertEqual(store.read_all("flows.jsonl"), [])
        self.assertEqual(tr.held(), 0.0)
        self.assertLess(abs(tr.state["spot"]["flow_acc"]), 0.5)

    def test_alloc_change_and_preset_are_not_flows(self):
        tr, tg, store = self.settled()
        tr.act_alloc(80.0)
        self.run_for(tr, 3600)
        tr.act_preset("공격")
        self.run_for(tr, DAY)
        self.assertGreaterEqual(len(store.read_all("transfers.jsonl")), 2)
        self.assertEqual(store.read_all("flows.jsonl"), [])

    def test_slow_earn_redemption_is_not_a_deposit_and_not_a_dip(self):
        tr, tg, store = self.make(over={"spot_earn": True})
        tr.startup()
        self.run_for(tr, DAY)
        parked = self.fake.earn_usdt
        self.assertGreater(parked, 5.0, "the daily job parked the idle USDT")
        r0 = self.ret(tr)
        self.fake.earn_redeem_delay = 150.0  # redeemed USDT takes its time to show up in the wallet
        tr.act_alloc(15.0)                   # the job now needs the parked USDT for the futures wallet
        self.tick(tr, 1)
        self.assertTrue(any(k == "redeem" for k, _ in self.fake.earn_ops))
        self.assertGreater(tr.state["spot"]["earn"]["transit"], 1.0, "on its way: still the sleeve's money")
        self.assertAlmostEqual(self.ret(tr), r0, delta=0.02, msg="no dip while the redemption is on its way")
        self.fake.outside_deposit(12.0)  # real new money arrives while the redemption is still out
        self.tick(tr, 35)                # the wallet is read again within ten minutes
        self.assertEqual(self.fake.earn_landing, [], "the redemption has landed")
        self.assertAlmostEqual(tr.state["spot"]["earn"]["transit"], 0.0, places=6)
        self.assertEqual(self.nets(store), [12.0], "the deposit is booked once, the redemption never")
        self.assertAlmostEqual(self.ret(tr), r0, delta=0.02)

    def test_redeemed_usdt_that_never_arrives_is_money_that_left(self):
        tr, tg, store = self.make(over={"spot_earn": True})
        tr.startup()
        self.run_for(tr, DAY)
        self.fake.earn_redeem_delay = 1e9
        tr.act_alloc(15.0)
        self.tick(tr, 1)
        self.fake.earn_redeem_delay = 0.0
        lost = sum(a for _, a in self.fake.earn_landing)
        self.assertGreater(lost, 1.0)
        self.fake.earn_landing.clear()  # redeemed, and it never shows up
        self.run_for(tr, 6 * 3600)
        self.assertEqual(store.read_all("flows.jsonl"), [], "still counted as on its way")
        self.assertAlmostEqual(tr.spot.value()["transit"], lost, delta=0.01)
        self.run_for(tr, DAY)
        self.assertEqual(len(self.nets(store)), 1)
        self.assertAlmostEqual(self.nets(store)[0], -lost, delta=0.01)
        self.assertEqual(tr.spot.value()["transit"], 0.0)

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
        self.assertEqual(self.nets(store), [-x])
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

    def test_earn_redemption_that_went_through_but_lost_its_answer(self):
        tr, tg, store = self.make(over={"spot_earn": True})
        tr.startup()
        self.run_for(tr, DAY)
        self.assertGreater(self.fake.earn_usdt, 5.0)
        n = len(self.fake.earn_ops)
        self.fake.earn_drop_response = 1
        tr.act_alloc(15.0)  # parked money is needed for the futures wallet
        self.tick(tr, 1)
        self.assertEqual(self.fake.earn_ops[n][0], "redeem")
        self.assertEqual(self.fake.earn_drop_response, 0, "carried out, answer lost")
        self.assertTrue(any("이자 예치" in t and "실패" in t for t in tg.texts()))
        self.run_for(tr, DAY + 3600)
        self.assertEqual(store.read_all("flows.jsonl"), [], "the bot's own Earn move, whatever the answer")

    def test_earn_redemption_with_lost_answer_that_lands_slowly(self):
        tr, tg, store = self.make(over={"spot_earn": True})
        tr.startup()
        self.run_for(tr, DAY)
        self.assertGreater(self.fake.earn_usdt, 5.0)
        r0 = self.ret(tr)
        self.fake.earn_drop_response = 1
        self.fake.earn_redeem_delay = 150.0
        tr.act_alloc(15.0)  # parked money is needed for the futures wallet
        self.tick(tr, 2)
        self.assertEqual(self.fake.earn_drop_response, 0, "carried out, answer lost")
        self.assertTrue(self.fake.earn_landing, "still on its way")
        self.assertAlmostEqual(self.ret(tr), r0, delta=0.02, msg="no dip while it is on its way")
        self.run_for(tr, DAY + 3600)
        self.assertEqual(store.read_all("flows.jsonl"), [], "neither a withdrawal nor a deposit")
        self.assertAlmostEqual(tr.spot.state["earn"].get("transit"), 0.0, places=6)

    def test_earn_redemption_that_failed_with_unknown_answer_changes_nothing(self):
        tr, tg, store = self.make(over={"spot_earn": True})
        tr.startup()
        self.run_for(tr, DAY)
        parked = self.fake.earn_usdt
        real = tr.spot.ex.earn_redeem

        def lost(*a, **kw):
            raise NetworkError("simulated timeout")  # never reached Binance

        tr.spot.ex.earn_redeem = lost
        tr.act_alloc(15.0)
        self.tick(tr, 2)
        tr.spot.ex.earn_redeem = real
        self.assertAlmostEqual(self.fake.earn_usdt, parked)
        self.run_for(tr, DAY + 3600)
        self.assertEqual(store.read_all("flows.jsonl"), [])
        self.assertIsNone(tr.spot.state["earn"].get("maybe_redeem"))
        self.assertEqual(tr.spot.state["earn"].get("transit") or 0.0, 0.0)

    def test_money_moved_while_earn_cannot_be_read_is_one_move(self):
        tr, tg, store = self.make(over={"spot_earn": True})
        tr.startup()
        self.run_for(tr, DAY)
        self.fake.outside_deposit(20.0)  # idle USDT in the spot wallet ...
        self.run_for(tr, 1200)
        self.assertEqual(self.nets(store), [20.0])
        self.fake.earn_fail_read = True
        self.fake.app_transfer(15.0, to_futures=True)  # ... that the user moves to futures while Earn is down
        self.run_for(tr, 1800, step=20)
        self.assertEqual(self.nets(store), [20.0], "the futures side alone is not booked")
        self.fake.earn_fail_read = False
        self.run_for(tr, 1200)
        flows = store.read_all("flows.jsonl")
        self.assertEqual(len(flows), 2)
        self.assertEqual((flows[1]["fut"], flows[1]["net"]), (15.0, 0.0))

    def test_earn_subscription_that_went_through_but_lost_its_answer(self):
        tr, tg, store = self.make(over={"spot_earn": True})
        tr.startup()
        self.run_for(tr, DAY)
        self.fake.outside_deposit(40.0)  # idle USDT for the next job to park
        self.run_for(tr, 1200)
        self.assertEqual(self.nets(store), [40.0])
        n = len(self.fake.earn_ops)
        self.fake.earn_drop_response = 1
        tr.spot.earn_park(self.clock())  # what the daily job does last: park the USDT that is left over
        self.assertEqual(self.fake.earn_ops[n][0], "subscribe")
        self.assertEqual(self.fake.earn_drop_response, 0, "carried out, answer lost")
        self.assertTrue(any("이자 예치" in t and "실패" in t for t in tg.texts()))
        self.run_for(tr, DAY + 3600)
        self.assertEqual(self.nets(store), [40.0], "the bot's own Earn move, whatever the answer")

    def test_deposit_is_not_judged_while_earn_cannot_be_read(self):
        tr, tg, store = self.make(over={"spot_earn": True})
        tr.startup()
        self.run_for(tr, DAY)
        r0 = self.ret(tr)
        self.fake.earn_fail_read = True
        self.fake.outside_deposit(40.0)  # ... or USDT that came back from Earn: it cannot be told yet
        self.run_for(tr, 1800)
        self.assertEqual(store.read_all("flows.jsonl"), [])
        self.assertTrue(tr.state["spot"]["flow_defer"])
        self.fake.earn_fail_read = False
        self.run_for(tr, 1200)
        self.assertEqual(self.nets(store), [40.0])
        self.assertAlmostEqual(self.ret(tr), r0, delta=0.01)

    def test_spot_order_with_unknown_outcome_that_was_filled(self):
        tr, tg, store = self.settled()
        n = len(self.fake.spot_trades)
        self.fake.spot_drop_response = 1   # the first order of the job is filled, its answer never arrives ...
        self.fake.spot_hide_lookups = 99   # ... and it cannot be looked up either
        tr.act_alloc(20.0)
        self.tick(tr, 1)
        self.assertGreater(len(self.fake.spot_trades), n)
        self.assertTrue(any("주문 결과를 확인하지 못했습니다" in t for t in tg.texts()))
        self.assertGreater(tr.state["spot"]["flow_unsure"] + len(store.read_all("flows.jsonl")), 0)
        self.fake.spot_hide_lookups = 0
        self.run_for(tr, 3600)
        self.assertEqual(store.read_all("flows.jsonl"), [], "USDT for coins: no money came or went")
        self.assertEqual(tr.state["spot"]["flow_unsure"], 0.0)

    def test_restart_right_after_an_own_transfer_and_trades(self):
        tr, tg, store = self.settled()
        n = len(store.read_all("transfers.jsonl"))
        tr.act_alloc(70.0)
        self.tick(tr, 1)  # the job moved money and traded; the process dies before it looks at the wallets again
        self.assertGreater(len(store.read_all("transfers.jsonl")), n)
        self.clock.advance(900)
        tr2, tg2, store2 = make_trader(self.fake, self.clock, self.tmp, spot=True)
        tr2.startup()
        self.run_for(tr2, 3600)
        self.assertEqual(store2.read_all("flows.jsonl"), [])

    def test_crash_in_the_middle_of_the_daily_job(self):
        """The process dies between two orders of the job and stays down for days: the coins it had already
        bought are its own trade, whatever their price is by then."""
        tr, tg, store = self.settled(balance=6000.0)
        real, calls = tr.spot.ex.order, []

        def order(*a, **kw):
            if calls:
                raise KeyboardInterrupt("killed")
            calls.append(1)
            return real(*a, **kw)

        tr.spot.ex.order = order
        tr.act_alloc(85.0)  # the job brings money over and buys coins
        self.clock.advance(20)
        with self.assertRaises(KeyboardInterrupt):
            tr.step()
        self.assertEqual(len(calls), 1, "one order went through before the crash")
        self.clock.advance(3 * DAY)
        tr2, tg2, store2 = make_trader(self.fake, self.clock, self.tmp, spot=True)
        tr2.startup()
        self.run_for(tr2, 3600)
        self.assertEqual(store2.read_all("flows.jsonl"), [])

    def test_self_test_round_trip_is_not_judged_half_way(self):
        tr, tg, store = self.settled()
        start0 = tr.ledger["start_equity"]
        tr.spot_busy = True               # the live self-test is moving 7 USDT over ...
        self.fake.app_transfer(7.0, to_futures=False)
        self.tick(tr, 2)
        self.assertEqual(store.read_all("flows.jsonl"), [])
        self.assertAlmostEqual(tr.held(), -7.0, delta=0.01, msg="kept out of the guards' sight")
        self.fake.app_transfer(7.0, to_futures=True)  # ... and back
        tr.spot_busy = False
        tr.spot.cache = None
        self.run_for(tr, 3600)
        self.assertEqual(store.read_all("flows.jsonl"), [])
        self.assertAlmostEqual(tr.ledger["start_equity"], start0, delta=start0 * 1e-9)


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

    def test_deposit_seen_by_a_command_before_the_regular_look_is_not_a_profit_for_a_moment(self):
        tr, tg, store = self.settled()
        r0, peak0 = self.ret(tr), tr.state["total"]["peak"]
        self.fake.outside_deposit(50.0)
        tr.spot.value(refresh=True)  # e.g. /spot: the wallet is read, the deposit is in the valuation
        tr.track_total(self.clock())
        self.assertAlmostEqual(tr.state["total"]["peak"], peak0, delta=peak0 * 0.001, msg="seen, not booked: not a peak")
        say(tr, "/pnl")
        self.assertNotIn("+5", tg.texts()[-1].split("최대낙폭")[0].split("대비")[-1])
        self.tick(tr, 1)
        self.assertEqual(self.nets(store), [50.0], "booked at the very next step")
        self.assertAlmostEqual(self.ret(tr), r0, delta=0.01)

    def test_withdrawal_and_coin_deposit(self):
        tr, tg, store = self.settled()
        r0 = self.ret(tr)
        out = round(min(self.usdt() * 0.8, 8.0), 2)
        self.assertGreater(out, 1.5)
        self.fake.outside_deposit(-out)
        self.run_for(tr, 1200)
        self.assertEqual(self.nets(store), [-out])
        self.assertTrue(any(t.startswith("💵 출금 감지") for t in tg.texts()))
        self.assertAlmostEqual(self.ret(tr), r0, delta=0.01, msg="a withdrawal is not a loss")
        px = self.fake.price("AAAUSDT")
        self.fake.outside_deposit(0.1, asset="AAA")  # coins sent in from another exchange
        self.run_for(tr, 1200)
        flows = store.read_all("flows.jsonl")
        self.assertEqual(len(flows), 2)
        self.assertAlmostEqual(flows[1]["net"], 0.1 * px, delta=0.1 * px * 0.03)
        self.assertAlmostEqual(self.ret(tr), r0, delta=0.015)

    def test_deposit_in_the_middle_of_the_daily_job_is_booked_in_full(self):
        tr, tg, store = self.settled()
        r0 = self.ret(tr)
        real, hit = tr.spot.ex.order, []

        def order(*a, **kw):
            if not hit:
                hit.append(1)
                self.fake.outside_deposit(30.0)  # lands between the job's own orders and transfers
            return real(*a, **kw)

        tr.spot.ex.order = order
        tr.act_alloc(25.0)
        self.tick(tr, 2)
        self.assertTrue(hit, "the job traded")
        self.assertEqual(self.nets(store), [30.0])
        self.assertAlmostEqual(self.ret(tr), r0, delta=0.01)

    def test_deposit_seconds_after_the_bots_own_moves_is_booked(self):
        tr, tg, store = self.settled()
        r0 = self.ret(tr)
        tr.act_alloc(70.0)
        self.tick(tr, 1)                 # the job: transfer and trades
        self.fake.outside_deposit(30.0)  # ... and right behind it, real new money
        self.tick(tr, 31)                # the spot wallet is read every ten minutes
        self.assertEqual(self.nets(store), [30.0])
        self.assertAlmostEqual(self.ret(tr), r0, delta=0.01)

    def test_money_moved_between_the_wallets_by_hand_nets_to_zero(self):
        tr, tg, store = self.settled()
        base0, in0, start0 = tr.state["total"]["base"], tr.state["spot"]["net_in"], tr.ledger["start_equity"]
        fv0 = tr.state["equity"]
        x = round(min(self.usdt() * 0.8, 10.0), 2)
        self.assertGreater(x, 1.5)
        self.fake.app_transfer(x, to_futures=True)
        self.fake.outside_deposit(0.02)  # and two cents of something else: still the same money, not a deposit
        self.tick(tr, 1)
        flows = store.read_all("flows.jsonl")
        self.assertEqual(len(flows), 1, "booked at the very next step")
        self.assertEqual((flows[0]["fut"], flows[0]["net"]), (x, 0.0))
        self.assertAlmostEqual(flows[0]["spot"], -x, delta=0.03)
        self.assertEqual(tr.state["total"]["base"], base0, "total return is untouched")
        self.assertAlmostEqual(float(tr.state["total"].get("net_flow") or 0.0), 0.0)
        self.assertAlmostEqual(tr.state["spot"]["net_in"], in0 - x, delta=0.03)
        self.assertAlmostEqual(tr.ledger["start_equity"] / start0, (fv0 + x) / fv0, delta=0.02,
                               msg="the futures floor baseline follows the money")
        self.assertTrue(any(t.startswith("↔️ 현물↔선물 지갑 사이를 오간 돈") for t in tg.texts()))
        self.assertFalse(tr.state.get("force_alloc"), "a move between the wallets is not new money to split")

    def test_outside_money_into_the_futures_wallet_never_reaches_the_guards_unbooked(self):
        tr, tg, store = self.settled()
        r0, start0, fv0 = self.ret(tr), tr.ledger["start_equity"], tr.state["equity"]
        tr.ledger["peak_equity"] = fv0  # the futures wallet stands at its peak
        tr.state["total"]["peak"] = tr.total_equity()[0]
        self.fake.outside_futures(20.0)
        self.tick(tr, 1)
        self.assertEqual([(f["spot"], f["fut"], f["net"]) for f in store.read_all("flows.jsonl")], [(0.0, 20.0, 20.0)],
                         "booked at the very next step")
        self.assertAlmostEqual(self.ret(tr), r0, delta=0.01)
        self.assertAlmostEqual(tr.ledger["start_equity"] / start0, (fv0 + 20.0) / fv0, delta=0.02)
        fv = tr.state["equity"]
        self.assertGreater((fv - tr.ledger["peak_equity"]) / tr.ledger["peak_equity"], -0.02,
                           "the peak must not be pushed up twice (once by the money, once by the scaling)")
        tot = tr.total_equity()[0]
        self.assertGreater((tot - tr.state["total"]["peak"]) / tr.state["total"]["peak"], -0.02)

    def test_big_withdrawal_from_the_futures_wallet_is_not_a_crash(self):
        tr, tg, store = self.settled()
        fv0, start0 = tr.state["equity"], tr.ledger["start_equity"]
        dd0, mdd0, r0 = tr.state["max_dd"], tr.state["total"]["mdd"], self.ret(tr)
        out = round(fv0 * 0.7, 2)  # far below the 50 % floor if it were a loss
        self.fake.outside_futures(-out)
        self.tick(tr, 3)
        self.assertEqual(self.nets(store), [-out])
        self.assertFalse(any("자본 바닥선" in t for t in tg.texts()), "a withdrawal is not a loss")
        self.assertFalse(tr.state.get("floor_alerted"))
        self.assertGreater(tr.state["max_dd"], dd0 - 3.0)
        self.assertGreater(tr.state["total"]["mdd"], mdd0 - 3.0)
        self.assertAlmostEqual(tr.ledger["start_equity"] / start0, (fv0 - out) / fv0, delta=0.03)
        self.assertAlmostEqual(self.ret(tr), r0, delta=0.01)
        eq = tr.broker.equity()["equity"]
        self.assertIsNone(B.R.account_guard(tr.ledger, self.clock(), tr.guard_equity(eq), tr.cfg))

    def test_manual_command_right_after_money_reached_the_futures_wallet_uses_fresh_balances(self):
        tr, tg, store = self.settled()
        fv0, start0, r0 = tr.state["equity"], tr.ledger["start_equity"], self.ret(tr)
        self.fake.outside_futures(60.0)
        say(tr, "/deposit 60 선물")  # before the next step has looked at the wallet
        self.assertEqual([(f["fut"], f["net"], f["src"]) for f in store.read_all("flows.jsonl")], [(60.0, 60.0, "auto")])
        self.assertIn("자동으로 반영", tg.texts()[-1])
        self.assertAlmostEqual(tr.ledger["start_equity"] / start0, (fv0 + 60.0) / fv0, delta=0.02)
        self.assertAlmostEqual(self.ret(tr), r0, delta=0.01)
        self.run_for(tr, 3600)
        self.assertEqual(len(store.read_all("flows.jsonl")), 1)

    def test_everything_withdrawn_then_fresh_money_starts_a_new_baseline(self):
        tr, tg, store = self.settled()
        tot, sv, fv = tr.total_equity()
        T0 = dict(tr.state["total"])
        tr.state["equity"], tr.spot.cache["equity"] = 0.0, 0.0  # both wallets emptied
        tr.book_flow(self.clock(), -sv, -fv, "manual")
        self.assertEqual(tr.state["total"]["base"], 0.0, "no -100% 'return' for taking the money out")
        say(tr, "/pnl")
        self.assertNotIn("처리 중 오류", tg.texts()[-1])
        tr.act_flow_undo()  # a record that emptied the baselines can be taken back in full
        self.assertAlmostEqual(tr.state["total"]["base"], T0["base"])
        self.assertGreater(tr.ledger["start_equity"], 0.0)
        tr.book_flow(self.clock(), -sv, -fv, "manual")
        tr.state["equity"] = 80.0
        tr.book_flow(self.clock(), 0.0, 80.0, "manual")
        self.assertEqual(tr.state["total"]["base"], 80.0)
        self.assertEqual(tr.state["total"]["net_flow"], 0.0, "the starting capital is not a later deposit")
        self.assertEqual(tr.ledger["start_equity"], 80.0)

    def test_small_noise_is_ignored_and_does_not_pile_up(self):
        tr, tg, store = self.settled()
        for _ in range(5):
            self.fake.outside_deposit(0.3)  # bonus interest paid into the wallet, dust ...
            self.run_for(tr, 1200)
        self.assertEqual(store.read_all("flows.jsonl"), [])
        self.assertEqual(tr.state["spot"]["flow_acc"], 0.0)

    def test_switched_off_and_on_again(self):
        tr, tg, store = self.settled(over={"flow_detect": False})
        self.fake.outside_deposit(50.0)
        self.fake.app_transfer(8.0, to_futures=True)
        self.run_for(tr, 3600)
        self.assertEqual(store.read_all("flows.jsonl"), [])
        say(tr, "/set flow_detect true")
        self.run_for(tr, 3600)
        self.assertEqual(store.read_all("flows.jsonl"), [], "watching starts when it is switched on, not before")
        self.fake.outside_deposit(15.0)
        self.run_for(tr, 1200)
        self.assertEqual(self.nets(store), [15.0])

    def test_switched_off_while_watching_and_on_again(self):
        tr, tg, store = self.settled()
        say(tr, "/set flow_detect false")
        self.fake.outside_deposit(30.0)
        self.fake.app_transfer(8.0, to_futures=True)
        self.fake.outside_futures(12.0)
        self.run_for(tr, 3600)
        say(tr, "/set flow_detect true")
        self.run_for(tr, 3600)
        self.assertEqual(store.read_all("flows.jsonl"), [], "what happened while it was off is not judged afterwards")
        self.fake.outside_deposit(15.0)
        self.run_for(tr, 1200)
        self.assertEqual(self.nets(store), [15.0])

    def test_switching_off_while_something_is_waiting_says_so(self):
        tr, tg, store = self.settled()
        self.fake.income_fail = True
        self.fake.outside_futures(25.0)
        self.tick(tr, 2)
        self.assertAlmostEqual(tr.held(), 25.0, delta=0.5)
        say(tr, "/set flow_detect false")
        self.assertIn("반영하지 않고 넘깁니다", tg.texts()[-1])
        self.assertIn("+$25.00", tg.texts()[-1])

    def test_restart_on_v25_after_running_v24_for_a_while(self):
        """/rollback to v2.4 and /update again: v2.4 knew nothing of the watch, so it starts afresh."""
        tr, tg, store = self.settled()
        tr.save()
        st = store.load_state()
        st["seen_version"] = "2.4.0"  # what v2.4 writes
        store.save_state(st)
        # what v2.4 did meanwhile: its own transfer and trades, invisible to the v2.5 bookkeeping
        self.fake.app_transfer(9.0, to_futures=False)
        self.fake.outside_deposit(-3.0, asset="USDT")
        self.fake.outside_deposit(0.05, asset="AAA")
        self.clock.advance(2 * DAY)
        tr2, tg2, store2 = make_trader(self.fake, self.clock, self.tmp, spot=True)
        tr2.startup()
        self.run_for(tr2, 3600)
        self.assertEqual(store2.read_all("flows.jsonl"), [])
        self.fake.outside_deposit(15.0)
        self.run_for(tr2, 1200)
        self.assertEqual(self.nets(store2), [15.0])

    def test_deposit_while_the_bot_was_down(self):
        tr, tg, store = self.settled()
        r0 = self.ret(tr)
        tr.save()
        self.fake.outside_deposit(40.0)
        self.fake.outside_futures(10.0)
        self.clock.advance(3 * 3600)
        tr2, tg2, store2 = make_trader(self.fake, self.clock, self.tmp, spot=True)
        tr2.startup()
        self.tick(tr2, 1)
        self.assertEqual([(f["spot"], f["fut"]) for f in store2.read_all("flows.jsonl")], [(40.0, 10.0)])
        self.assertAlmostEqual(self.ret(tr2), r0, delta=0.02)

    def test_futures_only_bot_books_outside_money_too(self):
        self.clock = SimClock(START)
        self.fake = FakeExchange(self.clock, history_days=430, test_days=3)
        tr, tg, store = make_trader(self.fake, self.clock, self.tmp, spot=False)
        tr.startup()
        self.run_for(tr, 3600)
        start0, fv0 = tr.ledger["start_equity"], tr.state["equity"]
        self.fake.outside_futures(25.0)
        self.tick(tr, 1)
        self.assertEqual([(f["spot"], f["fut"], f["net"]) for f in store.read_all("flows.jsonl")], [(0.0, 25.0, 25.0)])
        self.assertAlmostEqual(tr.ledger["start_equity"] / start0, (fv0 + 25.0) / fv0, delta=0.02)
        say(tr, "/pnl")
        self.assertNotIn("처리 중 오류", tg.texts()[-1])


class Unclear(Base):
    """What happens while the bot cannot tell yet what a change of the wallets was."""

    def test_unreadable_income_history_holds_everything_back(self):
        tr, tg, store = self.settled()
        r0, fv0, L0 = self.ret(tr), tr.state["equity"], dict(tr.ledger)
        n_moves = len(store.read_all("transfers.jsonl"))
        self.fake.income_fail = True
        self.fake.outside_deposit(50.0)
        self.fake.outside_futures(30.0)
        self.tick(tr, 3)
        self.assertEqual(store.read_all("flows.jsonl"), [], "both wallets are judged together or not at all")
        self.assertAlmostEqual(tr.held(), 30.0, delta=0.5)
        self.assertAlmostEqual(tr.guard_equity(tr.state["equity"]), fv0, delta=fv0 * 0.02)
        self.assertAlmostEqual(tr.ledger["start_equity"], L0["start_equity"])
        self.assertLessEqual(tr.ledger["peak_equity"], max(L0["peak_equity"], fv0 * 1.02), "not a new peak")
        say(tr, "/deposit 30 선물")
        self.assertIn("확인하는 중", tg.texts()[-1])
        self.assertNotIn("inline", tg.sent[-1][1], "no hand-written record while the bot's own look is open")
        # the trading day turns and the daily job runs while the history is still down
        tr.act_alloc(80.0)
        self.run_to_utc(tr, 600, step=20)
        self.assertEqual(store.read_all("flows.jsonl"), [])
        self.assertEqual(len(store.read_all("transfers.jsonl")), n_moves, "no money is moved on an unclear picture")
        self.assertTrue(tr.state.get("force_alloc"), "the split is still owed")
        self.assertLess(tr.ledger["day_start_equity"], fv0 * 1.1, "the new day did not start from unbooked money")
        self.assertLess(tr.state["total"]["peak"], tr.total_equity()[0] - 60.0, "80 USDT of unbooked money: not a peak")
        self.assertTrue(any("밀려 있습니다" in t for t in tg.texts()))
        r1 = (tr.total_equity()[0] - tr.unbooked()) / tr.state["total"]["base"]  # a day of trading, no deposit in it
        self.assertAlmostEqual(r1, r0, delta=0.15)
        self.fake.income_fail = False
        self.tick(tr, 4)
        self.assertEqual([(f["spot"], f["fut"], f["net"]) for f in store.read_all("flows.jsonl")], [(50.0, 30.0, 80.0)])
        self.assertEqual(tr.held(), 0.0)
        fv = tr.state["equity"]
        self.assertAlmostEqual(tr.ledger["start_equity"] / L0["start_equity"], fv / (fv - 30.0), delta=0.02)
        self.assertAlmostEqual(tr.ledger["day_start_equity"], fv, delta=fv * 0.05)
        self.assertAlmostEqual(self.ret(tr), r1, delta=0.01)
        self.run_to_utc(tr, 900)
        self.assertGreater(len(store.read_all("transfers.jsonl")), n_moves, "once it is clear, the split follows")
        self.assertEqual(len(store.read_all("flows.jsonl")), 1)

    def test_income_row_that_shows_up_late(self):
        tr, tg, store = self.settled()
        fv0, r0 = tr.state["equity"], self.ret(tr)
        tr.ledger["peak_equity"] = fv0
        self.fake.income_lag_s = 45.0
        self.fake.outside_futures(20.0)
        self.tick(tr, 2)
        self.assertEqual(store.read_all("flows.jsonl"), [])
        self.assertAlmostEqual(tr.held(), 20.0, delta=0.5, msg="unexplained: waits for its row")
        self.assertLess(tr.ledger["peak_equity"], fv0 * 1.03)
        self.tick(tr, 1)
        self.assertEqual(self.nets(store), [20.0])
        self.assertEqual(tr.held(), 0.0)
        self.assertAlmostEqual(self.ret(tr), r0, delta=0.01)

    def late_rows(self, lag, move):
        """Income rows show up `lag` seconds late; `move` changes the wallets. Returns the trader after the rows
        arrived and were booked, with the state before the move."""
        tr, tg, store = self.settled()
        fv0 = tr.state["equity"]
        tr.ledger["peak_equity"] = fv0  # the futures wallet and the total stand at their peaks
        tr.state["total"]["peak"] = tr.total_equity()[0]
        before = {"r": self.ret(tr), "fv": fv0, "L": dict(tr.ledger), "dd": tr.state["max_dd"],
                  "mdd": tr.state["total"]["mdd"]}
        self.fake.income_lag_s = lag
        move()
        self.tick(tr, int(lag / 20) - 2)
        self.assertEqual(store.read_all("flows.jsonl"), [], "the rows are not there yet")
        self.assertNotEqual(tr.held(), 0.0, "still waiting for its rows")
        self.tick(tr, 6)
        return tr, tg, store, before

    def assert_figures_kept(self, tr, before, fut):
        self.assertAlmostEqual(self.ret(tr), before["r"], delta=0.01)
        fv = tr.state["equity"]
        self.assertAlmostEqual(tr.ledger["start_equity"] / before["L"]["start_equity"], fv / (fv - fut), delta=0.02)
        self.assertAlmostEqual(tr.ledger["peak_equity"] / before["L"]["peak_equity"], fv / (fv - fut), delta=0.03,
                               msg="the peak is scaled once, not raised by the money first")
        self.assertGreater(tr.state["max_dd"], before["dd"] - 3.0, "no drawdown from money that left")
        self.assertGreater(tr.state["total"]["mdd"], before["mdd"] - 3.0)
        tot = tr.total_equity()[0]
        self.assertGreater((tot - tr.state["total"]["peak"]) / tr.state["total"]["peak"], -0.03)
        self.assertEqual(tr.held(), 0.0)

    def test_deposit_whose_row_is_minutes_late(self):
        tr, tg, store, before = self.late_rows(240.0, lambda: self.fake.outside_futures(50.0))
        self.assertEqual(self.nets(store), [50.0])
        self.assert_figures_kept(tr, before, 50.0)
        self.run_for(tr, 3600)
        self.assertEqual(len(store.read_all("flows.jsonl")), 1)

    def test_withdrawal_whose_row_is_minutes_late(self):
        out = []

        def move():
            out.append(round(self.fake.wallet * 0.6, 2))  # below the floor if it were a loss
            self.fake.outside_futures(-out[0])

        tr, tg, store, before = self.late_rows(240.0, move)
        self.assertEqual(self.nets(store), [-out[0]])
        self.assert_figures_kept(tr, before, -out[0])
        self.assertFalse(tr.state.get("floor_alerted"), "released once it was clear the money was taken out")
        floor_msgs = [t for t in tg.texts() if "자본 바닥선 도달" in t]
        self.assertTrue(all("확인하는 중" in t for t in floor_msgs), "the floor alert says the change is being checked")
        if floor_msgs:
            self.assertTrue(any("신규 진입을 다시 허용" in t for t in tg.texts()))
        eq = tr.broker.equity()["equity"]
        self.assertIsNone(B.R.account_guard(tr.ledger, self.clock(), tr.guard_equity(eq), tr.cfg))

    def test_app_transfer_whose_futures_row_is_minutes_late(self):
        x = []

        def move():
            x.append(round(min(self.usdt() * 0.8, 10.0), 2))
            self.fake.app_transfer(x[0], to_futures=True)

        tr, tg, store, before = self.late_rows(240.0, move)
        flows = store.read_all("flows.jsonl")
        self.assertEqual(len(flows), 1, "one move between the wallets, not a withdrawal and then a deposit")
        self.assertEqual((flows[0]["fut"], flows[0]["net"]), (x[0], 0.0))
        self.assertFalse(any("출금 감지" in t or "입금 감지" in t for t in tg.texts()))
        self.assertAlmostEqual(self.ret(tr), before["r"], delta=0.005)

    def test_change_no_row_ever_explains_counts_as_a_result_later(self):
        tr, tg, store = self.settled()
        L0 = dict(tr.ledger)
        self.fake.wallet += 20.0  # no row is ever written for it
        self.tick(tr, 40)
        self.assertAlmostEqual(tr.held(), 20.0, delta=0.5, msg="a late row is still possible")
        self.assertAlmostEqual(tr.ledger["start_equity"], L0["start_equity"])
        self.tick(tr, 15)
        self.assertEqual(tr.held(), 0.0, "once no row can turn up any more, it is a result")
        self.assertEqual(store.read_all("flows.jsonl"), [])
        self.assertAlmostEqual(tr.ledger["start_equity"], L0["start_equity"], msg="a result does not move the floor")

    def test_trading_result_is_not_a_flow_and_reaches_the_guards(self):
        tr, tg, store = self.settled()
        fv0 = tr.state["equity"]
        # a stop was hit: the wallet drops and Binance shows the rows
        self.fake.wallet -= 6.0
        t_ms = int(self.clock() * 1000)
        self.fake.income.append({"symbol": "AAAUSDT", "incomeType": "REALIZED_PNL", "income": "-5.9", "asset": "USDT",
                                 "time": t_ms, "tranId": 1})
        self.fake.income.append({"symbol": "AAAUSDT", "incomeType": "COMMISSION", "income": "-0.1", "asset": "USDT",
                                 "time": t_ms, "tranId": 2})
        self.tick(tr, 1)
        self.assertEqual(tr.held(), 0.0, "explained by its rows at once")
        self.assertAlmostEqual(tr.stats_equity(tr.state["equity"]), fv0 - 6.0, delta=fv0 * 0.02)
        # the same without rows (they lag): the floor sees the drop at once, the figures once its rows are there
        self.fake.wallet -= 6.0
        self.tick(tr, 1)
        self.assertAlmostEqual(tr.held(), -6.0, delta=0.5)
        self.assertAlmostEqual(tr.guard_equity(tr.state["equity"]), fv0 - 12.0, delta=fv0 * 0.02)
        self.fake.income.append({"symbol": "AAAUSDT", "incomeType": "REALIZED_PNL", "income": "-6", "asset": "USDT",
                                 "time": int(self.clock() * 1000), "tranId": 3})
        self.tick(tr, 1)
        self.assertEqual(tr.held(), 0.0)
        self.assertEqual(store.read_all("flows.jsonl"), [])
        self.assertAlmostEqual(tr.stats_equity(tr.state["equity"]), fv0 - 12.0, delta=fv0 * 0.02)

    def test_a_loss_reaches_the_equity_floor_while_the_history_cannot_be_read(self):
        """Money safety: whatever the bot cannot explain, a drop of the futures wallet is never hidden from the
        equity floor (the floor would otherwise stay open for a day while the account bleeds)."""
        tr, tg, store = self.settled()
        self.fake.income_fail = True
        self.fake.wallet *= 0.4  # a 60 % loss, for all the bot can tell
        self.tick(tr, 1)
        eq = tr.broker.equity()["equity"]
        self.assertEqual(B.R.account_guard(tr.ledger, self.clock(), tr.guard_equity(eq), tr.cfg), "EQUITY_FLOOR")
        self.assertTrue(tr.state.get("floor_alerted"))
        self.assertTrue(any("자본 바닥선 도달" in t and "확인하는 중" in t for t in tg.texts()))
        # ... while a rise is not believed before it is judged
        self.fake.wallet /= 0.4
        self.fake.wallet += 30.0
        self.tick(tr, 1)
        eq = tr.broker.equity()["equity"]
        self.assertLess(tr.guard_equity(eq), eq - 25.0)

    def test_a_loss_after_an_unconfirmed_deposit_still_reaches_the_floor(self):
        tr, tg, store = self.settled()
        fv0 = tr.state["equity"]
        self.fake.income_fail = True
        self.fake.outside_futures(round(fv0 * 0.8, 2))  # a deposit that cannot be confirmed yet ...
        self.tick(tr, 2)
        self.fake.wallet -= fv0 * 0.7  # ... and then a loss: the real balance is still above the old floor
        self.tick(tr, 1)
        eq = tr.broker.equity()["equity"]
        floor = tr.ledger["start_equity"] * tr.cfg["equity_floor_pct"] / 100.0
        self.assertGreater(eq, floor, "the real balance alone would not trip the floor")
        self.assertEqual(B.R.account_guard(tr.ledger, self.clock(), tr.guard_equity(eq), tr.cfg), "EQUITY_FLOOR",
                         "the loss is not netted against money that may just have been put in")

    def test_withdrawal_during_a_long_outage_can_be_recorded_after_the_give_up(self):
        tr, tg, store = self.settled()
        out = round(self.fake.wallet * 0.6, 2)
        self.fake.income_fail = True
        self.fake.outside_futures(-out)
        self.tick(tr, 3)
        eq = tr.broker.equity()["equity"]
        self.assertEqual(B.R.account_guard(tr.ledger, self.clock(), tr.guard_equity(eq), tr.cfg), "EQUITY_FLOOR",
                         "fails safe while it cannot be told")
        self.run_for(tr, 25 * 3600, step=300)
        self.assertTrue(any("지금 바로 적을 수 있고" in t for t in tg.texts()))
        say(tr, f"/withdraw {out} 선물")  # what the message asks for, while the history is still down
        press_ok(tr, tg)
        self.assertEqual([(f["fut"], f["src"]) for f in store.read_all("flows.jsonl")], [(-out, "manual")])
        eq = tr.broker.equity()["equity"]
        self.assertIsNone(B.R.account_guard(tr.ledger, self.clock(), tr.guard_equity(eq), tr.cfg))
        self.fake.income_fail = False
        self.run_for(tr, 3600)
        self.assertEqual(len(store.read_all("flows.jsonl")), 1, "the row of the outage is never booked on top")

    def test_second_change_during_a_wait_is_not_booked_twice(self):
        tr, tg, store = self.settled()
        start0 = tr.ledger["start_equity"]
        self.fake.wallet += 20.0              # no row is ever written for this one
        self.tick(tr, 15)                     # five minutes into the wait ...
        self.fake.income_lag_s = 13 * 60
        self.fake.outside_futures(15.0)       # ... a deposit whose row shows up only after the release
        self.run_for(tr, 3600, step=20)
        self.assertEqual(tr.held(), 0.0)
        # both were counted as results at the release (a known trade-off for rows that late); what must never
        # happen is the deposit being booked on top afterwards, scaling baselines that already contain it
        self.assertEqual(store.read_all("flows.jsonl"), [])
        self.assertAlmostEqual(tr.ledger["start_equity"], start0, delta=start0 * 1e-6)
        self.fake.income_lag_s = 0.0
        self.run_for(tr, 3600)
        self.assertEqual(store.read_all("flows.jsonl"), [], "nothing booked on top afterwards")

    def test_unexplained_change_is_let_through_after_a_day_at_the_latest(self):
        tr, tg, store = self.settled()
        self.fake.income_fail = True
        self.fake.wallet -= 6.0  # a loss, for all the bot can tell
        self.tick(tr, 3)
        self.assertAlmostEqual(tr.held(), -6.0, delta=0.5)
        self.run_for(tr, 12 * 3600, step=300)
        self.assertNotEqual(tr.held(), 0.0, "still unclear")
        self.run_for(tr, 13 * 3600, step=300)
        self.assertEqual(tr.held(), 0.0, "not for ever")
        self.assertEqual(store.read_all("flows.jsonl"), [])
        self.assertTrue(any("매매 결과로 처리했습니다" in t for t in tg.texts()), "the user is told")
        self.fake.income_fail = False
        self.run_for(tr, 3600)
        self.assertEqual(store.read_all("flows.jsonl"), [], "rows of that day are never booked afterwards")
        self.assertEqual(tr.held(), 0.0)

    def test_history_down_with_nothing_waiting_does_not_stop_the_split(self):
        tr, tg, store = self.settled()
        n = len(store.read_all("transfers.jsonl"))
        self.fake.income_fail = True
        tr.act_alloc(80.0)
        self.tick(tr, 3)
        self.assertGreater(len(store.read_all("transfers.jsonl")), n, "nothing to judge: money still moves")
        self.fake.income_fail = False
        self.run_for(tr, 3600)
        self.assertEqual(store.read_all("flows.jsonl"), [])

    def test_waits_while_a_transfer_of_the_bot_is_unconfirmed(self):
        tr, tg, store = self.settled()
        self.fake.outside_deposit(50.0)
        tr.state["pending_transfer"] = {"amount": 5.0}
        self.assertEqual(tr.flow_pass(self.clock(), force=True), "blocked")
        self.assertEqual(store.read_all("flows.jsonl"), [])
        tr.state["pending_transfer"] = None
        self.assertEqual(tr.flow_pass(self.clock(), force=True), "booked")
        self.assertEqual(self.nets(store), [50.0])
        self.assertEqual(tr.flow_pass(self.clock(), force=True), "none")

    def lost_transfer(self, tr):
        """The next transfer of the bot gets no answer and cannot be confirmed; returns what it tried."""
        tried = []

        def transfer(to_futures, amount):
            tried.append((to_futures, amount))
            raise NetworkError("simulated timeout")

        tr.spot.ex.transfer = transfer
        self.fake.transfer_history_fail = True
        tr.act_alloc(15.0)
        self.tick(tr, 1)
        self.assertTrue(tr.state.get("pending_transfer"), "outcome unknown")
        return tried[0]

    def test_own_transfer_confirmed_late_is_not_a_flow(self):
        tr, tg, store = self.settled()
        start0, fv0 = tr.ledger["start_equity"], tr.state["equity"]
        to_futures, amount = self.lost_transfer(tr)
        self.assertTrue(to_futures)
        self.fake.app_transfer(amount, to_futures=True)  # it did go through, a little later
        self.tick(tr, 1)
        self.assertFalse(tr.state.get("pending_transfer"))
        self.assertTrue(any("확인 중이던 이체가 처리됐습니다" in t for t in tg.texts()))
        self.fake.transfer_history_fail = False
        self.run_for(tr, 3600)
        self.assertEqual(store.read_all("flows.jsonl"), [])
        self.assertEqual(tr.held(), 0.0)
        self.assertAlmostEqual(tr.ledger["start_equity"] / start0, (fv0 + amount) / fv0, delta=0.03)

    def test_own_transfer_judged_not_executed_that_did_go_through(self):
        tr, tg, store = self.settled()
        start0, fv0, base0 = tr.ledger["start_equity"], tr.state["equity"], tr.state["total"]["base"]
        to_futures, amount = self.lost_transfer(tr)
        self.run_for(tr, 3700, step=100)
        self.assertFalse(tr.state.get("pending_transfer"))
        self.assertTrue(any("실행되지 않은 것으로 판단" in t for t in tg.texts()))
        self.fake.app_transfer(amount, to_futures=True)  # ... and then it shows up after all
        self.tick(tr, 1)
        flows = store.read_all("flows.jsonl")
        self.assertEqual([(f["fut"], f["net"]) for f in flows], [(amount, 0.0)], "money that changed wallets, nothing else")
        self.assertEqual(tr.state["total"]["base"], base0)
        self.assertAlmostEqual(tr.ledger["start_equity"] / start0, (fv0 + amount) / fv0, delta=0.03)

    def test_trouble_inside_the_look_never_stops_trading(self):
        tr, tg, store = self.settled()
        day0, real = tr.state["spot"]["last_day"], tr.futures_income

        def broken(F):
            raise RuntimeError("something unforeseen")

        tr.futures_income = broken
        self.fake.outside_deposit(30.0)
        self.run_for(tr, DAY)
        self.assertNotEqual(tr.state["spot"]["last_day"], day0, "the daily job still ran")
        self.assertTrue(any("입금·출금 확인 중 오류" in t for t in tg.texts()))
        self.assertEqual(store.read_all("flows.jsonl"), [])
        tr.futures_income = real
        self.run_for(tr, 1200)
        self.assertEqual(self.nets(store), [30.0], "nothing was lost: booked once the look works again")

    def test_one_unreadable_income_row_is_skipped(self):
        tr, tg, store = self.settled()
        self.fake.income.append({"symbol": "", "incomeType": "TRANSFER", "income": "not-a-number", "asset": "USDT",
                                 "time": int(self.clock() * 1000) + 1000, "tranId": 77})
        self.fake.outside_futures(20.0)
        self.tick(tr, 2)
        self.assertEqual(self.nets(store), [20.0])
        self.assertFalse(any("입금·출금 확인 중 오류" in t for t in tg.texts()))


class Commands(Base):
    def test_rebase_after_a_deposit_does_not_book_it_again(self):
        tr, tg, store = self.settled()
        self.fake.outside_deposit(30.0)
        self.fake.outside_futures(10.0)
        tr.cmd_rebase([])
        self.tick(tr, 1)
        tot = tr.total_equity()[0]
        self.assertAlmostEqual(tr.state["total"]["base"], tot, delta=tot * 0.01)
        self.run_for(tr, 3600)
        self.assertEqual(store.read_all("flows.jsonl"), [])
        self.assertAlmostEqual(self.ret(tr), 1.0, delta=0.02)

    def test_rebase_while_the_spot_sleeve_is_not_ready(self):
        tr, tg, store = self.settled()
        base0 = tr.state["total"]["base"]
        tr.spot_ready = False  # e.g. its start-up failed and is being retried
        tr.cmd_rebase([])
        self.assertIn("기준점을 다시 잡을 수 없습니다", tg.texts()[-1])
        self.assertEqual(tr.state["total"]["base"], base0)
        tr.spot_ready = True
        self.fake.outside_deposit(40.0)  # a deposit afterwards is still a deposit
        self.run_for(tr, 1200)
        self.assertEqual(self.nets(store), [40.0])

    def test_rebase_keeps_the_unit_price_going(self):
        tr, tg, store = self.settled()
        tr.state["total"]["base"] *= 0.8  # as if the account had earned 25 % since the base was set
        self.run_to_utc(tr, 900)
        nav0 = tr.state["hist"][-1][3]
        self.assertGreater(nav0, 1.2)
        tr.cmd_rebase([])
        self.assertAlmostEqual(self.ret(tr), 1.0, delta=0.001)
        self.run_to_utc(tr, 900)
        nav1 = tr.state["hist"][-1][3]
        self.assertAlmostEqual(nav1 / nav0, self.ret(tr), delta=0.01,
                               msg="one more day of trading on top of the old unit price: no -20 % month in the report")
        say(tr, "/report 이번달")
        self.assertNotIn("처리 중 오류", tg.texts()[-1])

    def test_manual_command_does_not_double_book_what_was_detected(self):
        tr, tg, store = self.settled()
        self.fake.outside_deposit(25.0)
        say(tr, "/deposit 25")
        self.assertEqual([(f["net"], f["src"]) for f in store.read_all("flows.jsonl")], [(25.0, "auto")])
        self.assertIn("자동으로 반영", tg.texts()[-1])
        self.run_for(tr, 3600)
        self.assertEqual(len(store.read_all("flows.jsonl")), 1)

    def test_money_arriving_between_question_and_confirmation_is_not_booked_twice(self):
        tr, tg, store = self.settled()
        say(tr, "/deposit 25")
        self.assertIn("자동으로 감지된 입금이 없습니다", tg.texts()[-1])
        self.fake.outside_deposit(25.0)  # it arrives only now
        press_ok(tr, tg)
        self.assertEqual([(f["net"], f["src"]) for f in store.read_all("flows.jsonl")], [(25.0, "auto")])
        self.assertIn("자동으로 반영", tg.texts()[-1])

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

    def test_a_typo_cannot_wipe_the_baseline(self):
        tr, tg, store = self.settled()
        T0 = dict(tr.state["total"])
        say(tr, "/deposit 100000")
        self.assertIn("크거나 같습니다", tg.texts()[-1])
        self.assertNotIn("inline", tg.sent[-1][1])
        self.assertEqual(store.read_all("flows.jsonl"), [])
        self.assertEqual(tr.state["total"]["base"], T0["base"])

    def test_futures_record_bigger_than_the_futures_wallet_is_refused(self):
        tr, tg, store = self.settled()
        fv = tr.state["equity"]
        say(tr, f"/deposit {fv + 5:.0f} 선물")
        self.assertIn("선물 지갑", tg.texts()[-1])
        self.assertIn("크거나 같습니다", tg.texts()[-1])
        self.assertEqual(store.read_all("flows.jsonl"), [])

    def test_undo_of_a_futures_side_flow_restores_the_guard_baselines(self):
        tr, tg, store = self.settled()
        L0 = dict(tr.ledger)
        self.fake.outside_futures(20.0)
        self.tick(tr, 1)
        self.assertGreater(tr.ledger["start_equity"], L0["start_equity"] * 1.1)
        say(tr, "/flows undo")
        press_ok(tr, tg)
        self.assertAlmostEqual(tr.ledger["start_equity"], L0["start_equity"], delta=L0["start_equity"] * 1e-6)

    def test_paper_deposit_moves_virtual_money_and_keeps_the_return(self):
        tr, tg, store = self.settled(mode="paper")
        self.fake.outside_deposit(500.0)  # the real account is none of the paper bot's business
        self.fake.outside_futures(300.0)
        self.run_for(tr, 3600)
        self.assertEqual(store.read_all("flows.jsonl"), [])
        self.assertEqual(tr.held(), 0.0)
        self.assertIsNone(tr.spot.state.get("snap"), "paper mode does not watch any wallet")
        self.assertEqual(tr.unbooked(), 0.0)
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
        self.run_for(tr, DAY)
        self.assertEqual(len(store.read_all("flows.jsonl")), 2)
        self.assertEqual(self.fake.transfers, [])
        self.assertEqual(self.fake.spot_trades, [])

    def test_mode_switch_starts_watching_afresh(self):
        tr, tg, store = self.settled(mode="paper")
        self.fake.outside_deposit(60.0)  # already in the real spot wallet when the user goes live
        tr.act_go_live()
        self.run_for(tr, 3600)
        self.assertEqual(self.flows(store), [],
                         "what is in the wallets at /live is the starting capital, not a deposit")
        self.fake.outside_deposit(15.0)
        self.run_for(tr, 1200)
        self.assertEqual(self.nets(store), [15.0])


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

    def test_state_written_by_v24_keeps_working(self):
        tr, tg, store = self.settled()
        base0 = tr.state["total"]["base"]
        tr.save()
        st = store.load_state()
        for k in ("flow_fut", "flow_hold", "nav_k", "nav_last", "last_flow"):
            st.pop(k, None)
        for k in ("snap", "flow_acc", "own_dq", "flow_unsure", "flow_defer"):
            st["spot"].pop(k, None)
        st["hist"] = [h[:3] for h in st["hist"]]
        st["total"].pop("net_flow", None)
        store.save_state(st)
        tr2, tg2, store2 = make_trader(self.fake, self.clock, self.tmp, spot=True)
        tr2.startup()
        self.run_for(tr2, 3600)
        self.assertEqual(store2.read_all("flows.jsonl"), [], "what was there before the update is not a deposit")
        self.assertEqual(tr2.state["total"]["base"], base0)
        self.fake.outside_deposit(20.0)
        self.run_for(tr2, 1200)
        self.assertEqual(self.nets(store2), [20.0])

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
