"""v2.9: /dust (small balances -> BNB, never the coins the spot sleeve holds) and /kimp (tether kimchi premium)."""
import logging
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import wallet as WL  # noqa: E402
from fake_binance import FakeExchange, SimClock  # noqa: E402
from test_live_flow import START, make_trader  # noqa: E402

DAY = 86400


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        logging.disable(logging.NOTSET)

    def make(self, mode="live", days=3):
        self.clock = SimClock(START)
        self.fake = FakeExchange(self.clock, history_days=430, test_days=days + 1, spot_balance=200.0)
        tr, tg, store = make_trader(self.fake, self.clock, self.tmp, mode=mode, spot=True)
        tr.startup()
        tr.http_get = self.prices({})
        return tr, tg, store

    def run_for(self, tr, seconds, step=60):
        for _ in range(int(seconds / step)):
            self.clock.advance(step)
            tr.step()

    def say(self, tr, text):
        tr.handle_update({"message": {"chat": {"id": 42}, "text": text}})

    def press(self, tr, tg, label):
        for _, kw in reversed(tg.sent):
            if kw.get("inline"):
                data = next(d for row in kw["inline"] for lab, d in row if label in lab)
                break
        tr.handle_update({"callback_query": {"id": "1", "data": data, "message": {"chat": {"id": 42}}}})

    @staticmethod
    def prices(table):
        def get(url, timeout=5):
            if url not in table:
                raise OSError("unreachable")
            return table[url]
        return get


# ---------------------------------------------------------------- /dust
class Dust(Base):
    def leftovers(self, tr):
        """The sleeve holds AAA on purpose; BBB is a leftover worth about $2. Taken as the wallet's new normal."""
        tr.spot.state["targets"] = {"AAAUSDT": 0.5}
        self.fake.spot_bal["AAA"] = 3.0 / self.fake.price("AAAUSDT")
        self.fake.spot_bal["BBB"] = 2.0 / self.fake.price("BBBUSDT")
        tr.flow_reset()
        self.run_for(tr, 1200)                      # a fresh look at the wallet with the leftovers in it

    def test_leftovers_become_bnb_and_held_coins_stay(self):
        tr, tg, store = self.make()
        self.run_for(tr, 3600)
        self.leftovers(tr)
        aaa = self.fake.spot_bal["AAA"]
        self.say(tr, "/dust")
        text = tg.texts()[-1]
        self.assertIn("· BBB", text)
        self.assertNotIn("· AAA", text)
        self.assertIn("건드리지 않습니다: AAA", text)
        self.assertIn("되돌릴 수 없습니다", text)
        self.press(tr, tg, "실행")
        self.assertEqual(self.fake.dust_calls, [["BBB"]])
        self.assertEqual(self.fake.spot_bal["BBB"], 0.0)
        self.assertEqual(self.fake.spot_bal["AAA"], aaa, "the sleeve's coin is not touched")
        self.assertGreater(self.fake.spot_bal["BNB"], 0.0)
        self.assertIn("✅ 자투리 1개를 BNB", tg.texts()[-1])
        self.run_for(tr, 1800)
        self.assertEqual(store.read_all("flows.jsonl"), [], "the bot's own conversion is not a withdrawal")

    def test_paper_mode_only_lists(self):
        tr, tg, store = self.make(mode="paper")
        self.fake.spot_bal["BBB"] = 2.0 / self.fake.price("BBBUSDT")
        self.say(tr, "/dust")
        self.assertIn("모의 모드에서는 바꾸지 않습니다", tg.texts()[-1])
        self.assertNotIn("inline", tg.sent[-1][1])
        tr.act_dust(["BBB"])
        self.assertEqual(self.fake.dust_calls, [])

    def test_list_changed_before_the_press(self):
        tr, tg, store = self.make()
        self.run_for(tr, 3600)
        self.leftovers(tr)
        self.say(tr, "/dust")
        tr.spot.state["targets"]["BBBUSDT"] = 0.2   # the sleeve wants BBB now
        self.press(tr, tg, "실행")
        self.assertEqual(self.fake.dust_calls, [])
        self.assertIn("그사이 바뀌었습니다", tg.texts()[-1])

    def test_refused(self):
        tr, tg, store = self.make()
        self.run_for(tr, 3600)
        self.leftovers(tr)
        self.fake.dust_error = (-32110, "Only can be requested once within 6 hours.")
        self.say(tr, "/dust")
        self.press(tr, tg, "실행")
        self.assertIn("바꾸지 못했습니다", tg.texts()[-1])
        self.assertIn("6 hours", tg.texts()[-1])

    def test_lost_answer_is_not_sent_again(self):
        tr, tg, store = self.make()
        self.run_for(tr, 3600)
        self.leftovers(tr)
        self.fake.dust_drop_response = 1
        self.say(tr, "/dust")
        self.press(tr, tg, "실행")
        self.assertIn("결과를 확인하지 못했습니다", tg.texts()[-1])
        self.assertEqual(len(self.fake.dust_calls), 1)

    def test_nothing_to_convert(self):
        tr, tg, store = self.make()
        self.say(tr, "/dust")
        self.assertIn("BNB로 바꿀 자투리가 없습니다", tg.texts()[-1])

    def test_deltas(self):
        r = {"transferResult": [{"fromAsset": "AAA", "amount": "2", "transferedAmount": "0.01"},
                                {"fromAsset": "XYZ", "amount": "5", "transferedAmount": "0.02"}]}
        self.assertEqual(WL.dust_deltas(r, {"AAA", "BNB"}), {"AAA": -2.0, "BNB": 0.01},
                         "BNB bought with coins outside the bot is left to the outside-money check")
        self.assertEqual(WL.dust_deltas(r, {"AAA"}), {"AAA": -2.0})


# ---------------------------------------------------------------- /kimp
FULL = {WL.BITHUMB[0]: [{"market": "KRW-USDT", "trade_price": 1452.0}], WL.UPBIT: [{"trade_price": 1450.0}],
        WL.FX[0]: [{"code": "FRX.KRWUSD", "basePrice": 1400.0}]}


class Kimp(Base):
    def test_premium(self):
        v = WL.kimp_view(self.prices(FULL))
        self.assertAlmostEqual(v["premium"], (1452 / 1400 - 1) * 100)
        tr, tg, store = self.make()
        tr.http_get = self.prices(FULL)
        self.say(tr, "/kimp")
        text = tg.texts()[-1]
        self.assertIn("빗썸 1,452원 · 업비트 1,450원", text)
        self.assertIn("달러 환율: 1,400.0원 (은행 매매기준율)", text)
        self.assertIn("김프 +3.7%", text)
        self.assertIn("비싸게 삽니다", text)

    def test_fallback_sources(self):
        get = self.prices({WL.BITHUMB[1]: {"status": "0000", "data": {"closing_price": "1380"}},
                           WL.FX[1]: {"result": "success", "rates": {"KRW": 1400.0}}})
        v = WL.kimp_view(get)
        self.assertEqual(v["bithumb"], 1380.0)
        self.assertTrue(v["fx_daily"])
        text = WL.kimp_text(v, "11/21 14:00")
        self.assertIn("하루 한 번", text)
        self.assertIn("역프", text)

    def test_nothing_readable(self):
        tr, tg, store = self.make()
        self.say(tr, "/kimp")
        self.assertIn("계산할 수 없습니다", tg.texts()[-1])
        self.say(tr, "/balance")
        self.assertNotIn("만 원", next(t for t in reversed(tg.texts()) if t.startswith("💼")))
        self.say(tr, "/address")
        self.assertNotIn("테더 김프", tg.sent[-1][0])

    def test_shown_with_balance_and_address(self):
        tr, tg, store = self.make()
        tr.http_get = self.prices(FULL)
        self.say(tr, "/balance")
        text = next(t for t in reversed(tg.texts()) if t.startswith("💼"))
        self.assertIn("만 원", text)
        self.assertIn("테더 김프 +3.7% (빗썸 1,452원 / 환율 1,400.0원)", text)
        self.say(tr, "/address")
        self.assertIn("테더 김프 +3.7%", tg.sent[-1][0])

    def test_a_broken_answer_is_skipped(self):
        v = WL.kimp_view(self.prices({WL.BITHUMB[0]: {"error": "x"}, WL.UPBIT: [{"trade_price": 0}],
                                      WL.FX[0]: [{"basePrice": 1400.0}]}))
        self.assertIsNone(v["bithumb"])
        self.assertIsNone(v["premium"])


if __name__ == "__main__":
    unittest.main()
