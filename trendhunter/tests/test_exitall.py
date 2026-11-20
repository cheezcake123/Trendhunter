"""v2.7: /closeall — 'futures only' or 'everything' (futures closed, spot coins sold, new entries and the daily spot
job halted until /resume)."""
import logging
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from fake_binance import FakeExchange, SimClock  # noqa: E402
from test_live_flow import START, make_trader  # noqa: E402

DAY = 86400


class ExitAll(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        logging.disable(logging.NOTSET)

    def make(self, mode="live", days=12):
        self.clock = SimClock(START)
        self.fake = FakeExchange(self.clock, history_days=430, test_days=days + 1)
        tr, tg, store = make_trader(self.fake, self.clock, self.tmp, mode=mode, spot=True)
        tr.startup()
        return tr, tg, store

    def run_for(self, tr, seconds, step=600):
        for _ in range(int(seconds / step)):
            self.clock.advance(step)
            tr.step()

    def until_held(self, tr):
        """Until the bot holds a futures position and spot coins."""
        for _ in range(10 * 144):
            self.clock.advance(600)
            tr.step()
            if tr.positions and tr.spot_coins()[0]:
                return
        self.fail("no futures position and spot coins together")

    def say(self, tr, text):
        tr.handle_update({"message": {"chat": {"id": 42}, "text": text}})

    def buttons(self, tg):
        for _, kw in reversed(tg.sent):
            if kw.get("inline"):
                return [b for row in kw["inline"] for b in row]
        return []

    def press(self, tr, tg, label, buttons=None):
        data = next(d for lab, d in (buttons or self.buttons(tg)) if label in lab)
        tr.handle_update({"callback_query": {"id": "1", "data": data, "message": {"chat": {"id": 42}}}})

    def coins(self, tr):
        return tr.spot_coins()[0]

    def assert_all_sold(self, tr, report):
        """Everything the exchange lets us sell is sold; what its lot size leaves behind is reported."""
        self.assertEqual(tr.spot.liquidate(self.clock())["trades"], [], "nothing sellable is left")
        for s, v in self.coins(tr):
            self.assertIn(f"{s[:-4]} ${v:.2f} 남음", report)
            self.assertIn("소액 자산 전환", report)

    def test_everything_sold_and_halted_until_resume(self):
        tr, tg, store = self.make()
        self.until_held(tr)
        held = [s for s, _ in self.coins(tr)]
        self.say(tr, "/closeall")
        text = tg.texts()[-1]
        self.assertIn("[선물] 포지션", text)
        self.assertIn("[현물] 코인", text)
        self.assertEqual([lab for lab, _ in self.buttons(tg)], ["🔴 선물만 청산", "🧹 전부 정리하고 멈춤", "취소"])
        self.press(tr, tg, "전부 정리")
        self.assertEqual(tr.positions, {})
        for s in self.fake.symbols:
            self.assertEqual(self.fake.open_algos(s), [], "no stop left behind")
        report = next(t for t in reversed(tg.texts()) if t.startswith("🧹 전부 정리 결과"))
        self.assert_all_sold(tr, report)
        for s in held:
            self.assertIn(s[:-4], report)
        self.assertIn("/resume", report)
        self.assertTrue(tr.state["paused"])
        self.assertTrue(tr.state["spot"]["halted"])
        self.assertEqual(store.read_all("flows.jsonl"), [], "the bot's own sales are not withdrawals")
        n_spot, n_fut = len(self.fake.spot_trades), len(self.fake.trades)
        self.run_for(tr, 2 * DAY)
        self.assertEqual(len(self.fake.spot_trades), n_spot, "the daily job does not buy back")
        self.assertEqual(len(self.fake.trades), n_fut, "no new futures entry")
        self.say(tr, "/status")
        self.assertIn("현물 리밸런싱 멈춤", tg.texts()[-1])
        self.say(tr, "/resume")
        self.assertIn("현물 리밸런싱도 다시 시작", tg.texts()[-1])
        self.assertFalse(tr.state["spot"]["halted"])
        self.run_for(tr, DAY)
        self.assertGreater(len(self.fake.spot_trades), n_spot, "after /resume the daily job buys again")
        self.assertEqual(store.read_all("flows.jsonl"), [])

    def test_sales_after_a_price_jump_are_not_outside_money(self):
        """The bot's own sales are taken out by quantity, so a price that moved since the last look at the wallet
        does not turn the sale proceeds into a 'deposit'."""
        tr, tg, store = self.make()
        self.until_held(tr)
        tr.spot.value(refresh=True)  # the wallet as last seen, at the old prices
        i = self.fake._minute(int(self.clock() * 1000))
        for sym in self.fake.paths:
            self.fake.paths[sym][i:] = [x * 1.25 for x in self.fake.paths[sym][i:]]
        self.say(tr, "/closeall")
        self.press(tr, tg, "전부 정리")
        self.run_for(tr, 3600)
        self.assertEqual(store.read_all("flows.jsonl"), [])

    def test_futures_only_keeps_the_coins_and_the_daily_job(self):
        tr, tg, store = self.make()
        self.until_held(tr)
        coins = self.coins(tr)
        self.say(tr, "/closeall")
        self.press(tr, tg, "선물만")
        self.assertEqual(tr.positions, {})
        self.assertEqual([s for s, _ in self.coins(tr)], [s for s, _ in coins])
        self.assertFalse(tr.state["paused"])
        self.assertFalse(tr.state["spot"].get("halted"))

    def test_the_other_buttons_are_void_once_one_is_pressed(self):
        tr, tg, store = self.make()
        self.until_held(tr)
        self.say(tr, "/closeall")
        both = self.buttons(tg)
        self.press(tr, tg, "선물만", both)
        n = len(self.fake.spot_trades)
        self.press(tr, tg, "전부 정리", both)
        self.assertIn("만료된 버튼", tg.texts()[-1])
        self.assertEqual(len(self.fake.spot_trades), n)
        self.assertFalse(tr.state["spot"].get("halted"))

    def test_cancel(self):
        tr, tg, store = self.make()
        self.until_held(tr)
        self.say(tr, "/closeall")
        both = self.buttons(tg)
        self.press(tr, tg, "취소", both)
        self.assertIn("취소했습니다", tg.texts()[-1])
        self.press(tr, tg, "전부 정리", both)
        self.assertIn("만료된 버튼", tg.texts()[-1])
        self.assertTrue(tr.positions)
        self.assertTrue(self.coins(tr))

    def test_paper_mode_sells_only_virtual_coins(self):
        tr, tg, store = self.make(mode="paper")
        self.until_held(tr)
        spot_orders, fut_orders = len(self.fake.spot_orders), len(self.fake.orders)
        self.say(tr, "/closeall")
        self.press(tr, tg, "전부 정리")
        self.assertEqual(tr.positions, {})
        self.assert_all_sold(tr, next(t for t in reversed(tg.texts()) if t.startswith("🧹 전부 정리 결과")))
        self.assertEqual(len(self.fake.spot_orders), spot_orders, "paper mode never sends a real order")
        self.assertEqual(len(self.fake.orders), fut_orders)
        self.assertTrue(tr.state["spot"]["halted"])

    def test_sale_with_unknown_outcome_is_not_sent_again(self):
        tr, tg, store = self.make()
        self.until_held(tr)
        first = self.coins(tr)[0][0]
        sells = lambda: [o for o in self.fake.spot_orders.values() if o.get("side") == "SELL"]  # noqa: E731
        n = len(sells())
        self.fake.spot_drop_response = 1   # the first sale is filled, its answer never arrives ...
        self.fake.spot_hide_lookups = 99   # ... and it cannot be looked up either
        self.say(tr, "/closeall")
        self.press(tr, tg, "전부 정리")
        report = next(t for t in reversed(tg.texts()) if t.startswith("🧹 전부 정리 결과"))
        self.assertIn("확인하지 못했습니다", report)
        self.assertIn(first[:-4], report)
        per_sym = [o for o in sells()[n:] if o.get("symbol") == first]
        self.assertEqual(len(per_sym), 1, "not sent a second time")
        self.fake.spot_hide_lookups = 0
        self.run_for(tr, 3600)
        self.assertEqual(store.read_all("flows.jsonl"), [], "USDT for coins: no money came or went")

    def test_spot_sleeve_not_ready(self):
        tr, tg, store = self.make()
        self.until_held(tr)
        tr.spot_ready = False
        tr.spot_next_try = self.clock() + 10 * DAY
        self.say(tr, "/closeall")
        self.assertIn("현물 모듈이 아직 준비되지 않아", tg.texts()[-1])
        self.press(tr, tg, "전부 정리")
        self.assertEqual(tr.positions, {})
        self.assertIn("코인을 팔지 못했습니다", tg.texts()[-1])
        self.assertTrue(tr.state["spot"]["halted"], "halted all the same: no buy-back when it comes back")

    def test_nothing_to_close(self):
        tr, tg, store = self.make()
        self.say(tr, "/closeall")
        self.assertIn("없습니다", tg.texts()[-1])
        self.assertNotIn("inline", tg.sent[-1][1])


if __name__ == "__main__":
    unittest.main()
