"""v2.8: running the bot without the Binance app — /address (USDT deposit addresses and deposit notices),
/balance (every wallet), /chart (view-only chart page) and the cooling-off for settings that raise the risk."""
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import unittest
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import bot as B  # noqa: E402
import cooloff as CO  # noqa: E402
import strategy as S  # noqa: E402
import wallet as WL  # noqa: E402
from fake_binance import FakeExchange, SimClock  # noqa: E402
from telegram_client import Telegram  # noqa: E402
from test_live_flow import START, make_trader  # noqa: E402

DAY = 86400
H = 3600


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        logging.disable(logging.NOTSET)

    def make(self, mode="live", spot=False, over=None, days=6, spot_balance=0.0):
        self.clock = SimClock(START)
        self.fake = FakeExchange(self.clock, history_days=430, test_days=days + 1, spot_balance=spot_balance)
        tr, tg, store = make_trader(self.fake, self.clock, self.tmp, mode=mode, spot=spot, over=over)
        tr.startup()
        return tr, tg, store

    def run_for(self, tr, seconds, step=60):
        for _ in range(int(seconds / step)):
            self.clock.advance(step)
            tr.step()

    def say(self, tr, text):
        tr.handle_update({"message": {"chat": {"id": 42}, "text": text}})

    def buttons(self, tg):
        for _, kw in reversed(tg.sent):
            if kw.get("inline"):
                return [b for row in kw["inline"] for b in row]
        return []

    def press(self, tr, tg, label):
        data = next(d for lab, d in self.buttons(tg) if label in lab)
        tr.handle_update({"callback_query": {"id": "1", "data": data, "message": {"chat": {"id": 42}}}})

    def told(self, tg, part):
        return [t for t in tg.texts() if part in t]


# ---------------------------------------------------------------- /address
class Address(Base):
    def test_three_networks_with_copyable_addresses(self):
        tr, tg, store = self.make()
        self.say(tr, "/address")
        text, kw = tg.sent[-1]
        self.assertTrue(kw.get("html"), "Telegram HTML: the addresses are <code> (tap to copy)")
        for label, net in (("트론 TRC20", "trx"), ("카이아 Kaia", "kaia"), ("앱토스 Aptos", "apt")):
            self.assertIn(f"<b>{label}</b>", text)
            self.assertIn(f"<code>{net}-addr-0001</code>", text)
        self.assertNotIn("eth-addr", text, "only the networks in deposit_networks")
        self.assertIn("⭐ 추천", text.split("카이아")[0], "the first open network is the suggested one")
        copies = [d["copy_text"]["text"] for _, d in self.buttons(tg) if isinstance(d, dict)]
        self.assertEqual(copies, ["trx-addr-0001", "kaia-addr-0001", "apt-addr-0001"])
        self.assertIn("같은 네트워크", text)

    def test_plain_text_when_the_formatted_message_fails(self):
        tr, tg, store = self.make()
        real = tg.send
        tg.send = lambda text, **kw: None if kw.get("html") else real(text, **kw)
        self.say(tr, "/address")
        text = tg.texts()[-1]
        self.assertIn("trx-addr-0001", text)
        self.assertNotIn("<code>", text)

    def test_network_under_maintenance_is_shown_and_skipped(self):
        tr, tg, store = self.make()
        self.fake.networks[0]["depositEnable"] = False      # Tron down
        self.fake.networks[0]["depositDesc"] = "Wallet maintenance"
        self.fake.networks = [n for n in self.fake.networks if n["network"] != "APT"]  # Binance drops Aptos
        self.say(tr, "/address")
        text = tg.sent[-1][0]
        self.assertIn("트론 TRC20</b> — ❌ 지금 입금 중단(점검 중)", text)
        self.assertNotIn("trx-addr", text)
        self.assertIn("앱토스 Aptos</b> — ❌ 바이낸스가 이 네트워크로 USDT를 받지 않습니다", text)
        self.assertIn("<b>카이아 Kaia</b> ⭐ 추천", text)
        self.assertEqual([d["copy_text"]["text"] for _, d in self.buttons(tg)], ["kaia-addr-0001"])

    def test_all_networks_closed(self):
        tr, tg, store = self.make()
        for n in self.fake.networks:
            n["depositEnable"] = False
        self.say(tr, "/address")
        self.assertIn("모두 입금을 받지 않습니다", tg.sent[-1][0])
        self.assertIsNone(tg.sent[-1][1].get("inline"))

    def test_network_names_also_match(self):
        """Binance may list a network under another code (Kaia was Klaytn): the name is checked as well."""
        rows = [{"network": "KLAY", "name": "Klaytn"}, {"network": "APTOS2", "name": "Aptos"}]
        self.assertEqual(WL.match_network(rows, "KAIA")["network"], "KLAY")
        self.assertEqual(WL.match_network(rows, "APT")["network"], "APTOS2")
        self.assertIsNone(WL.match_network(rows, "TRX"))

    def test_binance_unreachable(self):
        tr, tg, store = self.make()
        self.fake.wallet_fail.add("/sapi/v1/capital/config/getall")
        self.say(tr, "/address")
        self.assertIn("입금 정보를 읽지 못했습니다", tg.texts()[-1])

    def test_deposit_on_its_way_then_arrived(self):
        tr, tg, store = self.make()
        old = self.fake.chain_deposit(50)
        self.fake.credit_deposit(old)                      # an earlier deposit: never announced again
        self.clock.advance(600)
        self.say(tr, "/address")
        self.assertIn("최근 입금:", tg.texts()[-1])
        dep = self.fake.chain_deposit(100.0, network="KAIA")
        self.run_for(tr, 300)
        coming = self.told(tg, "입금이 오고 있습니다")
        self.assertEqual(len(coming), 1)
        self.assertIn("100.00 USDT", coming[0])
        self.assertIn("카이아", coming[0])
        self.fake.credit_deposit(dep)
        self.run_for(tr, 900)
        done = self.told(tg, "입금 완료 —")
        self.assertEqual(len(done), 1, "told once")
        self.assertIn("현물 지갑", done[0])
        self.assertFalse([t for t in self.told(tg, "50.00 USDT") if "입금 완료 —" in t or "오고 있습니다" in t])
        self.run_for(tr, 2 * DAY, step=600)
        n = len([c for c in self.fake.calls if c[1] == "/sapi/v1/capital/deposit/hisrec"])
        self.run_for(tr, DAY, step=600)
        self.assertEqual(len([c for c in self.fake.calls if c[1] == "/sapi/v1/capital/deposit/hisrec"]), n,
                         "the watch ends 48 hours after /address")
        self.assertIsNone(tr.state.get("dep_watch"))

    def test_deposit_into_the_funding_wallet_points_to_balance(self):
        tr, tg, store = self.make()
        self.say(tr, "/address")
        dep = self.fake.chain_deposit(30.0, wallet_type=1)
        self.fake.credit_deposit(dep)
        self.run_for(tr, 300)
        done = self.told(tg, "입금 완료 —")
        self.assertTrue(done and "펀딩 지갑" in done[0] and "/balance" in done[0])

    def test_deposit_waiting_for_confirmation(self):
        tr, tg, store = self.make()
        self.say(tr, "/address")
        dep = self.fake.chain_deposit(20.0)
        self.run_for(tr, 300)
        self.fake.credit_deposit(dep, status=8)
        self.run_for(tr, 300)
        msg = self.told(tg, "확인해 달라고")
        self.assertTrue(msg and "binance.com" in msg[0])

    def test_history_outage_does_not_stop_trading(self):
        tr, tg, store = self.make(days=3)
        self.say(tr, "/address")
        self.fake.wallet_fail.add("/sapi/v1/capital/deposit/hisrec")
        self.run_for(tr, 2 * DAY, step=600)
        self.assertTrue(self.fake.trades, "entries go on")

    def test_paper_mode_reads_but_never_moves_money(self):
        tr, tg, store = self.make(mode="paper")
        self.say(tr, "/address")
        self.assertIn("trx-addr-0001", tg.sent[-1][0])
        self.assertEqual(self.fake.transfers, [])

    def test_no_withdrawal_anywhere(self):
        """CLAUDE.md: the bot never withdraws. No code path names Binance's withdrawal endpoints."""
        for name in os.listdir(ROOT):
            if name.endswith(".py"):
                with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
                    src = fh.read()
                self.assertNotIn("capital/withdraw", src, name)
                self.assertNotIn("MAIN_FUNDING", src, name)


# ---------------------------------------------------------------- /balance
class Balance(Base):
    def test_every_wallet_and_coins_outside_the_bot(self):
        tr, tg, store = self.make(spot=True, spot_balance=200.0)
        self.run_for(tr, 2 * DAY, step=600)                    # the spot sleeve buys coins
        self.fake.spot_bal["XYZ"] = 7.0                        # a coin the bot does not trade (no USDT price)
        self.fake.funding_bal["USDT"] = 12.5
        self.fake.earn_locked = [{"asset": "USDT", "amount": "40"}]
        self.fake.other_wallets = {"Cross Margin": 3.25}
        self.say(tr, "/balance")
        text = next(t for t in reversed(tg.texts()) if t.startswith("💼"))
        spot = self.fake.spot_value() - self.fake.earn_usdt
        fut = self.fake.wallet + self.fake.upnl()
        total = spot + fut + self.fake.earn_usdt + 40 + 12.5 + 3.25
        self.assertIn(f"합계 약 {B.usd(total, False)}", text)
        self.assertIn(f"· 현물 {B.usd(spot, False)}", text)
        self.assertIn("봇이 관리하지 않는 코인: XYZ 7 (시세 없음)", text)
        self.assertIn("· 선물(USDⓈ-M)", text)
        self.assertIn("· 이자 예치(Earn)", text)
        self.assertIn("· 펀딩 지갑 $12.50 — USDT 12.50", text)
        self.assertIn("교차 마진 $3.25", text)
        self.assertIn("현물 지갑으로 옮길까요", tg.texts()[-1], "live: offered to move the funding USDT")

    def test_funding_usdt_moved_to_spot_and_booked_as_a_deposit(self):
        tr, tg, store = self.make(spot=True, spot_balance=200.0, days=3)
        self.run_for(tr, DAY, step=600)
        self.fake.funding_bal["USDT"] = 25.0
        spot_before = self.fake.spot_bal["USDT"]
        self.say(tr, "/balance")
        self.press(tr, tg, "실행")
        self.assertAlmostEqual(self.fake.funding_bal["USDT"], 0.0)
        self.assertAlmostEqual(self.fake.spot_bal["USDT"], spot_before + 25.0)
        self.assertIn("현물 지갑으로 옮겼습니다", tg.texts()[-1])
        self.run_for(tr, 1800, step=60)
        flows = store.read_all("flows.jsonl")
        self.assertEqual(len(flows), 1)
        self.assertAlmostEqual(flows[0]["net"], 25.0, places=2)

    def test_paper_mode_never_transfers(self):
        tr, tg, store = self.make(mode="paper", spot=True, spot_balance=200.0, days=2)
        self.fake.funding_bal["USDT"] = 25.0
        self.say(tr, "/balance")
        self.assertIn("모의 모드: 봇의 가상 잔고가 아니라 실제 계정 잔고", tg.texts()[-1])
        self.assertFalse(self.told(tg, "옮길까요"))
        tr.act_funding_to_spot()
        self.assertIn("모의 모드에서는 이체하지 않습니다", tg.texts()[-1])
        self.assertEqual(self.fake.funding_bal["USDT"], 25.0)

    def test_a_wallet_that_cannot_be_read(self):
        tr, tg, store = self.make()
        self.fake.wallet_fail.add("/sapi/v1/asset/get-funding-asset")
        self.say(tr, "/balance")
        text = tg.texts()[-1]
        self.assertIn("읽지 못함: 펀딩", text)
        self.assertIn("합계 약", text)
        self.assertIn("(읽지 못한 지갑은 빠짐)", text)


# ---------------------------------------------------------------- /chart
class Chart(Base):
    def held(self, tr, spot=True):
        for _ in range(10 * 144):
            self.clock.advance(600)
            tr.step()
            if tr.positions and (not spot or tr.spot_coins()[0]):
                return
        self.fail("nothing held")

    def links(self, tg):
        return [(lab, d["web_app"]["url"]) for lab, d in self.buttons(tg) if isinstance(d, dict) and "web_app" in d]

    def test_buttons_for_positions_and_spot_coins(self):
        tr, tg, store = self.make(spot=True, spot_balance=200.0, days=12)
        self.held(tr)
        self.say(tr, "/chart")
        links = self.links(tg)
        fut = [(lab, u) for lab, u in links if "선물" in lab]
        spot = [(lab, u) for lab, u in links if "현물" in lab]
        self.assertEqual(len(fut), len(tr.positions))
        self.assertEqual(len(spot), len(tr.spot_coins()[0]))
        sym, pos = next(iter(tr.positions.items()))
        lab, url = next((lab, u) for lab, u in fut if B.short(sym) in lab)
        self.assertTrue(url.startswith(tr.cfg["chart_url"] + "?"))
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.assertEqual(q["s"], sym)
        self.assertEqual(q["m"], "f")
        self.assertEqual(q["side"], "L" if pos["side"] == S.LONG else "S")
        self.assertAlmostEqual(float(q["e"]), pos["entry"])
        self.assertAlmostEqual(float(q["sl"]), pos["stop"])
        self.assertNotIn("qty", q, "no position size in the link")
        q2 = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(spot[0][1]).query))
        self.assertEqual(q2["m"], "s")
        self.assertNotIn("e", q2)
        self.assertIn("보기 전용", tg.texts()[-1])

    def test_falls_back_to_browser_links(self):
        tr, tg, store = self.make()
        self.held(tr, spot=False)
        real = tg.send
        tg.send = lambda text, **kw: None if any("web_app" in d for row in kw.get("inline") or [] for _, d in row
                                                 if isinstance(d, dict)) else real(text, **kw)
        self.say(tr, "/chart")
        urls = [d["url"] for _, d in self.buttons(tg) if isinstance(d, dict)]
        self.assertTrue(urls and all(u.startswith(tr.cfg["chart_url"]) for u in urls))

    def test_only_held_symbols(self):
        tr, tg, store = self.make()
        self.say(tr, "/chart")
        self.assertIn("보유 중인 종목이 없어", tg.texts()[-1])
        self.held(tr, spot=False)
        self.say(tr, "/chart DOGE")
        self.assertIn("보유 종목만 볼 수 있습니다", tg.texts()[-1])
        sym = next(iter(tr.positions))
        self.say(tr, f"/chart {B.short(sym)}")
        self.assertEqual(len(self.links(tg)), 1)

    def test_chart_page_exists_and_has_no_order_calls(self):
        page = os.path.join(os.path.dirname(ROOT), "docs", "chart.html")
        with open(page, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotRegex(src, r"/fapi/v1/order|/api/v3/order|X-MBX-APIKEY|signature", "view only: no keys, no orders")
        self.assertIn("fstream.binance.com/market/ws/", src, "futures klines stream on the /market path (2026-04)")
        lib = re.search(r'src="(vendor/[^"]+)"', src).group(1)
        self.assertTrue(os.path.exists(os.path.join(os.path.dirname(page), lib)), "the chart library is served with it")


# ---------------------------------------------------------------- cooling-off
class Rules(unittest.TestCase):
    def test_directions(self):
        up = [("risk_per_trade_pct", 2.5, 3.0), ("max_leverage", 5, 10), ("equity_floor_pct", 50, 30),
              ("alloc_spot_pct", 50, 40), ("allow_short", False, True), ("pyramid_max_adds", 0, 2),
              ("daily_loss_limit_pct", 8, 15), ("cooldown_after_loss_hours", 12, 0), ("spot_btc_gate", True, False),
              ("funding_max_pct", 0.04, 0.0), ("funding_max_pct", 0.04, 0.08), ("cool_off_hours", 24, 0),
              ("symbols", ["BTCUSDT"], ["BTCUSDT", "PEPEUSDT"]), ("adx_min", 20, 10)]
        down = [("risk_per_trade_pct", 3.0, 2.0), ("alloc_spot_pct", 40, 60), ("allow_short", True, False),
                ("funding_max_pct", 0.04, 0.02), ("funding_max_pct", 0.0, 0.04), ("cool_off_hours", 24, 48),
                ("symbols", ["BTCUSDT", "ETHUSDT"], ["BTCUSDT"]), ("risk_per_trade_pct", 2.5, 2.5),
                ("brief_morning_kst", "08:00", "07:00"), ("spot_earn", False, True), ("update_url", "", "https://x")]
        for k, o, n in up:
            self.assertTrue(CO.riskier(k, o, n), (k, o, n))
        for k, o, n in down:
            self.assertFalse(CO.riskier(k, o, n), (k, o, n))

    def test_every_settable_number_has_a_direction_or_is_neutral_on_purpose(self):
        neutral = {"alloc_band_pct", "spot_universe_size", "spot_band_pct", "spot_cash_yield", "time_stop_hours",
                   "liq_buffer_mult", "funding_max_pct"}
        for key, (kind, _, _) in B.SETTABLE.items():
            if kind in (float, int):
                self.assertTrue(key in CO.RISK_DIR or key in neutral, f"{key}: riskier or not?")


class CoolOff(Base):
    def make(self, **kw):
        over = dict(kw.pop("over", {}) or {})
        over.setdefault("cool_off_hours", 24.0)
        return super().make(over=over, **kw)

    def test_more_risk_waits_a_day_then_applies(self):
        tr, tg, store = self.make()
        before = tr.cfg["risk_per_trade_pct"]
        self.say(tr, "/risk 4")
        self.assertEqual(tr.cfg["risk_per_trade_pct"], before)
        self.assertIn("⏳ risk_per_trade_pct", tg.texts()[-1])
        self.assertIn("/cancel", tg.texts()[-1])
        self.assertNotIn("risk_per_trade_pct", tr.store.load_settings())
        self.run_for(tr, 23 * H, step=600)
        self.assertEqual(tr.cfg["risk_per_trade_pct"], before)
        self.assertTrue(self.told(tg, "1시간 뒤 적용 예정: risk_per_trade_pct"))
        self.run_for(tr, H + 600, step=600)
        self.assertEqual(tr.cfg["risk_per_trade_pct"], 4.0)
        self.assertEqual(tr.store.load_settings()["risk_per_trade_pct"], 4.0)
        self.assertTrue(self.told(tg, "⏰ risk_per_trade_pct"))
        self.assertEqual(tr.state["cool"], {})

    def test_less_risk_applies_now_and_drops_the_waiting_change(self):
        tr, tg, store = self.make()
        self.say(tr, "/risk 4")
        self.say(tr, "/risk 1.5")
        self.assertEqual(tr.cfg["risk_per_trade_pct"], 1.5)
        self.assertIn("즉시 적용", tg.texts()[-1])
        self.assertIn("예약돼 있던 risk_per_trade_pct → 4 변경은 취소", tg.texts()[-1])
        self.run_for(tr, 25 * H, step=1800)
        self.assertEqual(tr.cfg["risk_per_trade_pct"], 1.5)

    def test_a_second_request_restarts_the_wait(self):
        tr, tg, store = self.make()
        self.say(tr, "/set max_leverage 15")
        self.run_for(tr, 20 * H, step=1800)
        self.say(tr, "/set max_leverage 20")
        self.assertIn("앞의 예약을 바꿨습니다", tg.texts()[-1])
        self.run_for(tr, 10 * H, step=1800)
        self.assertNotEqual(tr.cfg["max_leverage"], 20)
        self.run_for(tr, 15 * H, step=1800)
        self.assertEqual(tr.cfg["max_leverage"], 20)

    def test_cancel_with_buttons(self):
        tr, tg, store = self.make()
        self.say(tr, "/risk 4")
        self.say(tr, "/set allow_short true")
        self.say(tr, "/status")
        self.assertIn("기다리는 설정 변경 2건", tg.texts()[-1])
        self.say(tr, "/cancel")
        labels = [lab for lab, _ in self.buttons(tg)]
        self.assertIn("↩️ 모두 취소", labels)
        self.assertEqual(labels[-1], "그대로 두기")
        self.press(tr, tg, "모두 취소")
        self.assertIn("취소했습니다", tg.texts()[-1])
        self.run_for(tr, 25 * H, step=1800)
        self.assertFalse(tr.cfg["allow_short"])
        self.assertNotEqual(tr.cfg["risk_per_trade_pct"], 4.0)
        self.say(tr, "/cancel")
        self.assertIn("기다리는 설정 변경이 없습니다", tg.texts()[-1])

    def test_cancel_one_by_name(self):
        tr, tg, store = self.make()
        self.say(tr, "/risk 4")
        self.say(tr, "/set funding_max_pct 0")
        self.say(tr, "/cancel funding_max_pct")
        self.assertEqual(list(tr.state["cool"]), ["risk_per_trade_pct"])

    def test_preset_splits_what_raises_and_what_lowers(self):
        tr, tg, store = self.make()
        tr._save_setting("alloc_spot_pct", 70.0)
        tr._save_setting("risk_per_trade_pct", 3.0)
        tr.act_preset("기본")                                   # alloc 70 -> 50 waits, risk 3 -> 2.5 now
        self.assertEqual(tr.cfg["risk_per_trade_pct"], 2.5)
        self.assertEqual(tr.cfg["alloc_spot_pct"], 70.0)
        text = tg.texts()[-1]
        self.assertIn("바로 적용: risk_per_trade_pct 3 → 2.5", text)
        self.assertIn("⏳ alloc_spot_pct: 70 → 50", text)
        self.run_for(tr, 24 * H + 600, step=1800)
        self.assertEqual(tr.cfg["alloc_spot_pct"], 50.0)
        self.assertTrue(tr.state.get("force_alloc") or tr.state["spot"].get("last_day") is None)

    def test_preset_with_less_risk_applies_at_once(self):
        tr, tg, store = self.make()
        tr._save_setting("pyramid_max_adds", 2)  # what the presets use: otherwise switching pyramiding on waits
        tr.act_preset("안정")
        self.assertEqual(tr.cfg["alloc_spot_pct"], 70.0)
        self.assertEqual(tr.cfg["risk_per_trade_pct"], 2.0)
        self.assertIn("✅ '안정' 단계 적용", tg.texts()[-1])

    def test_confirm_texts_say_it_waits(self):
        tr, tg, store = self.make()
        self.say(tr, "/preset 공격")
        self.assertIn("시간 뒤에 적용합니다", tg.texts()[-1])
        self.say(tr, "/alloc 20")
        self.assertIn("시간 뒤에 적용합니다", tg.texts()[-1])
        self.say(tr, "/alloc 80")
        self.assertNotIn("시간 뒤에", tg.texts()[-1])

    def test_alloc_toward_futures_waits(self):
        tr, tg, store = self.make(spot=True, spot_balance=200.0, days=3)
        self.run_for(tr, DAY, step=600)
        n = len(self.fake.transfers)
        tr.act_alloc(20.0)
        self.run_for(tr, 2 * H, step=600)
        self.assertEqual(tr.cfg["alloc_spot_pct"], 50)
        self.assertEqual(len([t for t in self.fake.transfers[n:] if t["type"] == "MAIN_UMFUTURE"]), 0,
                         "no money moved to futures while it waits")

    def test_the_wait_itself_cannot_be_shortened_at_once(self):
        tr, tg, store = self.make()
        self.say(tr, "/set cool_off_hours 0")
        self.assertEqual(tr.cfg["cool_off_hours"], 24.0)
        self.say(tr, "/set cool_off_hours 48")
        self.assertEqual(tr.cfg["cool_off_hours"], 48.0)
        self.assertEqual(tr.state["cool"], {}, "the lower wait was dropped by the higher one")

    def test_new_coin_waits_dropping_one_does_not(self):
        tr, tg, store = self.make()
        syms = list(tr.cfg["symbols"])
        self.say(tr, "/set symbols " + ",".join(B.short(s) for s in syms[:-1]))
        self.assertEqual(tr.cfg["symbols"], syms[:-1])
        self.say(tr, "/set symbols " + ",".join(B.short(s) for s in syms))
        self.assertEqual(tr.cfg["symbols"], syms[:-1])
        self.assertIn("symbols", tr.state["cool"])

    def test_waiting_changes_survive_a_restart(self):
        tr, tg, store = self.make()
        self.say(tr, "/risk 4")
        tr.save()
        tr2, tg2, _ = make_trader(self.fake, self.clock, self.tmp, over={"cool_off_hours": 24.0})
        tr2.startup()
        self.assertIn("risk_per_trade_pct", tr2.state["cool"])
        self.run_for(tr2, 25 * H, step=1800)
        self.assertEqual(tr2.cfg["risk_per_trade_pct"], 4.0)

    def test_rebase_that_lowers_the_floor_waits(self):
        tr, tg, store = self.make()
        start = tr.ledger["start_equity"]
        self.fake.wallet -= 30.0                         # a loss
        self.run_for(tr, 600)
        tr.flow_reset()
        self.say(tr, "/rebase")
        self.assertIn("바닥선이", tg.texts()[-1])
        self.assertIn("⏳ /rebase", tg.texts()[-1])
        self.assertEqual(tr.ledger["start_equity"], start)
        self.run_for(tr, 24 * H + 600, step=1800)
        self.assertLess(tr.ledger["start_equity"], start)
        self.assertTrue(self.told(tg, "예약했던 /rebase 를 실행했습니다"))

    def test_rebase_above_the_base_is_immediate(self):
        tr, tg, store = self.make()
        self.run_for(tr, 60)
        start = tr.ledger["start_equity"]
        self.say(tr, "/rebase")
        self.assertIn("재설정했습니다", tg.texts()[-1])
        self.assertGreaterEqual(tr.ledger["start_equity"], start)

    def test_back_to_live_waits_after_going_paper(self):
        """live -> paper -> live would start a fresh ledger at once (equity floor and guards forgotten)."""
        tr, tg, store = self.make(days=3)
        tr.act_go_paper()
        self.assertEqual(tr.state["mode"], "paper")
        self.say(tr, "/live")
        self.assertIn("실전으로 돌아갈 수 없습니다", tg.texts()[-1])
        self.assertEqual(tr.state["mode"], "paper")
        self.clock.advance(24 * H + 60)
        self.say(tr, "/live")
        self.assertNotIn("돌아갈 수 없습니다", tg.texts()[-1])

    def test_switched_off(self):
        tr, tg, store = self.make(over={"cool_off_hours": 0.0})
        self.say(tr, "/risk 4")
        self.assertEqual(tr.cfg["risk_per_trade_pct"], 4.0)


class Settings(unittest.TestCase):
    def test_config_check(self):
        tmp = tempfile.mkdtemp()
        try:
            self.assertEqual(B.check(tmp), 0)
            for bad in ({"cool_off_hours": -1}, {"cool_off_hours": "24"}, {"deposit_networks": []},
                        {"deposit_networks": ["TRX", "bad net"]}, {"chart_url": "http://x"}, {"chart_url": 3}):
                with open(os.path.join(tmp, "settings.json"), "w", encoding="utf-8") as fh:
                    json.dump(bad, fh)
                with self.assertRaises(SystemExit, msg=str(bad)):
                    B.check(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_netlist(self):
        self.assertEqual(B.netlist("trx, kaia,apt"), ["TRX", "KAIA", "APT"])
        for bad in ("", "a,b,c,d,e", "tr x!"):
            with self.assertRaises(ValueError):
                B.netlist(bad)

    def test_new_files_are_installed(self):
        import updater
        for f in ("wallet.py", "cooloff.py"):
            self.assertIn(f, updater.REQUIRED)

    def test_keyboard_buttons_are_commands(self):
        for row in B.KEYBOARD:
            for label in row:
                self.assertTrue(hasattr(B.Trader, "cmd_" + B.LABEL_CMD[label]), label)


class TelegramButtons(unittest.TestCase):
    def test_button_kinds_and_html(self):
        tg = Telegram("t", chat_id=1)
        sent = []
        tg.call = lambda method, params=None, timeout=20: sent.append(params) or {"message_id": 1}
        tg.send("<b>x</b>", inline=[[("a", {"web_app": {"url": "https://e/x"}}), ("b", "ok:1")],
                                    [("c", {"copy_text": {"text": "T123"}})]], html=True)
        p = sent[-1]
        self.assertEqual(p["parse_mode"], "HTML")
        kb = p["reply_markup"]["inline_keyboard"]
        self.assertEqual(kb[0][0], {"text": "a", "web_app": {"url": "https://e/x"}})
        self.assertEqual(kb[0][1], {"text": "b", "callback_data": "ok:1"})
        self.assertEqual(kb[1][0], {"text": "c", "copy_text": {"text": "T123"}})
        tg.send("plain")
        self.assertNotIn("parse_mode", sent[-1])


if __name__ == "__main__":
    unittest.main()
