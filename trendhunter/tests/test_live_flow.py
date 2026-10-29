"""End-to-end tests: the real Trader + LiveBroker/PaperBroker + signing code against FakeExchange.

Simulated clock; several simulated days per scenario. Checks the safety invariants on every step:
- every open position has exactly ONE reduce-only stop on the exchange, matching size and side
- stops only ever tighten
- no stray stops for flat symbols, never more positions than allowed
- realized PnL booked in the journal == the exchange wallet change (fees + funding included)
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
import strategy as S  # noqa: E402
from binance_api import BinanceError, BinanceFutures  # noqa: E402
from fake_binance import FakeExchange, SimClock, make_api, make_spot_api, serve_http  # noqa: E402
from store import Store  # noqa: E402

START = 1_760_000_000 // 3600 * 3600 + 1800  # half past an hour, UTC
logging.basicConfig(level=logging.WARNING)
LOG = logging.getLogger("test")


class FakeTG:
    chat_id = "42"

    def __init__(self):
        self.sent = []

    def send(self, text, **kw):
        self.sent.append((text, kw))
        return {"message_id": len(self.sent)}

    def set_commands(self, cmds):
        pass

    def send_document(self, name, data, caption=None):
        self.docs = getattr(self, "docs", [])
        self.docs.append((name, data, caption))
        return {"message_id": len(self.docs)}

    def answer_callback(self, *a, **k):
        pass

    def texts(self):
        return [t for t, _ in self.sent]


def make_trader(fake, clock, tmp, mode="live", over=None, spot=False):
    store = Store(tmp)
    if mode == "live" and not store.load_state():
        store.save_state({"mode": "live"})
    cfg = B.load_config(B.APP_DIR, store)
    cfg["symbols"] = list(fake.symbols)
    cfg["spot_enabled"] = spot
    cfg["spot_candidates"] = list(fake.symbols)
    cfg.update(over or {})
    tg = FakeTG()
    tr = B.Trader(cfg, store, make_api(fake, clock), tg, LOG, clock=clock, sleep=lambda s: None,
                  public_api_factory=lambda: make_api(fake, clock),
                  spot_api=make_spot_api(fake, clock), spot_public_factory=lambda: make_spot_api(fake, clock))
    return tr, tg, store


def fake_signal(tr, sym, side=S.LONG, atr_pct=0.01):
    m = tr.marks.get(sym)
    return {"side": side, "close": m, "atr": m * atr_pct, "adx": 30.0, "rsi": 60.0, "regime": side,
            "code": "BREAKOUT_LONG" if side == S.LONG else "BREAKOUT_SHORT", "strength": 30.0}


class LiveScenario(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def check(self, tr, fake, last_stops):
        self.assertEqual(set(fake.positions), set(tr.positions), "bot and exchange disagree on positions")
        self.assertLessEqual(len(fake.positions), tr.cfg["max_open_positions"])
        for sym, p in fake.positions.items():
            algos = fake.open_algos(sym)
            self.assertEqual(len(algos), 1, f"{sym} must have exactly one stop, has {len(algos)}")
            a = algos[0]
            self.assertTrue(a["reduceOnly"])
            self.assertEqual(a["side"], "SELL" if p["amt"] > 0 else "BUY")
            self.assertAlmostEqual(float(a["quantity"]), abs(p["amt"]), places=9)
            mine = tr.positions[sym]
            self.assertAlmostEqual(float(a["triggerPrice"]), mine["stop"], delta=abs(mine["stop"]) * 1e-6)
            key = (sym, mine["opened_ms"])
            trig = float(a["triggerPrice"])
            if key in last_stops:
                if p["amt"] > 0:
                    self.assertGreaterEqual(trig, last_stops[key] - 1e-9, "long stop loosened")
                else:
                    self.assertLessEqual(trig, last_stops[key] + 1e-9, "short stop loosened")
            last_stops[key] = trig
        for sym in fake.symbols:
            if sym not in fake.positions:
                self.assertEqual(fake.open_algos(sym), [], f"stray stop on flat {sym}")

    def run_scenario(self, direction, days=18, step=120, over=None):
        clock = SimClock(START)
        fake = FakeExchange(clock, direction=direction, test_days=days + 1)
        tr, tg, store = make_trader(fake, clock, self.tmp, over=over)
        tr.startup()
        w0 = fake.wallet
        last = {}
        for _ in range(int(days * 86400 / step)):
            clock.advance(step)
            tr.step()
            self.check(tr, fake, last)
        tr.act_closeall()
        self.assertEqual(fake.positions, {})
        self.assertEqual(fake.open_algos(), [])
        journal = store.read_all("journal.jsonl")
        booked = sum(j["pnl"] for j in journal)
        self.assertAlmostEqual(fake.wallet - w0, booked, delta=0.01,
                               msg=f"wallet change {fake.wallet - w0:.4f} vs booked {booked:.4f}")
        return tr, tg, store, fake, journal

    def test_uptrend_longs(self):
        tr, tg, store, fake, journal = self.run_scenario(+1)
        self.assertGreaterEqual(len(journal), 2, "expected some long trades")
        self.assertTrue(any(j["side"] == S.LONG for j in journal))
        entries = [e for e in store.read_all("events.jsonl") if e["kind"] == "ENTRY"]
        for e in entries:
            self.assertLessEqual(e["risk"], 100 * 0.03 * 1.6, "risk per trade above target tolerance")
        texts = tg.texts()
        self.assertTrue(any("진입" in t for t in texts))
        self.assertTrue(any(t.startswith("☀️ 아침 브리핑") for t in texts))
        self.assertTrue(any(t.startswith("🌙 저녁 브리핑") for t in texts))
        mornings = [t for t in texts if t.startswith("☀️")]
        self.assertLessEqual(len(mornings), 19)
        print(f"\n[uptrend] trades={len(journal)} pnl={sum(j['pnl'] for j in journal):+.2f} "
              f"reasons={sorted(set(j['reason'] for j in journal))}")

    def test_downtrend_shorts(self):
        tr, tg, store, fake, journal = self.run_scenario(-1, over={"allow_short": True})
        self.assertGreaterEqual(len(journal), 2, "expected some short trades")
        self.assertTrue(any(j["side"] == S.SHORT for j in journal))
        print(f"\n[downtrend] trades={len(journal)} pnl={sum(j['pnl'] for j in journal):+.2f} "
              f"reasons={sorted(set(j['reason'] for j in journal))}")


class FailurePaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.clock = SimClock(START)
        self.fake = FakeExchange(self.clock, direction=1, test_days=3, history_days=260)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def started(self, mode="live", over=None):
        tr, tg, store = make_trader(self.fake, self.clock, self.tmp, mode=mode, over=over)
        tr.startup()
        tr.marks = tr.api.mark_prices()
        return tr, tg, store

    def test_stop_rejected_means_emergency_close(self):
        tr, tg, _ = self.started()
        self.fake.reject_algo = True
        ok = tr.enter("AAAUSDT", fake_signal(tr, "AAAUSDT"), tr.broker.equity())
        self.assertFalse(ok)
        self.assertEqual(self.fake.positions, {})
        self.assertEqual(tr.positions, {})
        self.assertTrue(any("손절 주문을 걸지 못했습니다" in t for t in tg.texts()))

    def test_stop_vanished_is_replaced(self):
        tr, tg, _ = self.started()
        self.assertTrue(tr.enter("AAAUSDT", fake_signal(tr, "AAAUSDT"), tr.broker.equity()))
        a = self.fake.open_algos("AAAUSDT")[0]
        a["algoStatus"] = "CANCELED"  # e.g. cancelled by hand in the app
        self.clock.advance(20)
        tr.step()
        self.assertEqual(len(self.fake.open_algos("AAAUSDT")), 1)
        self.assertTrue(any("사라져" in t for t in tg.texts()))

    def test_duplicate_stops_are_cleaned(self):
        tr, tg, _ = self.started()
        tr.enter("AAAUSDT", fake_signal(tr, "AAAUSDT"), tr.broker.equity())
        pos = tr.positions["AAAUSDT"]
        tr.broker.place_stop("AAAUSDT", "SELL", pos["qty"], round(pos["stop"] * 0.99, 2))
        self.assertEqual(len(self.fake.open_algos("AAAUSDT")), 2)
        self.clock.advance(20)
        tr.step()
        self.assertEqual(len(self.fake.open_algos("AAAUSDT")), 1)

    def test_immediate_trigger_closes_position(self):
        tr, tg, _ = self.started()
        tr.enter("AAAUSDT", fake_signal(tr, "AAAUSDT"), tr.broker.equity())
        pos = tr.positions["AAAUSDT"]
        bad = round(tr.marks["AAAUSDT"] * 1.01, 2)  # a long's stop above the price would trigger at once
        self.assertFalse(tr.place_stop_safe("AAAUSDT", pos, trigger=bad))
        self.assertEqual(self.fake.positions, {})

    def test_soft_stop_backup(self):
        tr, tg, store = self.started()
        tr.enter("AAAUSDT", fake_signal(tr, "AAAUSDT"), tr.broker.equity())
        pos = tr.positions["AAAUSDT"]
        pos["stop"] = tr.marks["AAAUSDT"] * 1.02  # pretend price is already through the stop
        for _ in range(4):
            self.clock.advance(20)
            tr.step()
        self.assertEqual(self.fake.positions, {})
        self.assertEqual(store.read_all("journal.jsonl")[-1]["reason"], "SOFT_STOP")

    def test_exit_after_exchange_stop_already_filled(self):
        tr, tg, store = self.started()
        tr.enter("AAAUSDT", fake_signal(tr, "AAAUSDT"), tr.broker.equity())
        pos = tr.positions["AAAUSDT"]
        # the exchange stop fills first (a fast drop between two loops) ...
        algo = self.fake.open_algos("AAAUSDT")[0]
        amt = abs(self.fake.positions["AAAUSDT"]["amt"])
        self.fake._fill("AAAUSDT", "SELL", amt, pos["stop"], int(self.clock() * 1000), reduce_only=True)
        algo["algoStatus"] = "FINISHED"
        # ... and right after that the bot wants out too (time stop, reverse signal, /close): -2022 from Binance
        self.assertTrue(tr.exit_position("AAAUSDT", "TIME"))
        self.assertNotIn("AAAUSDT", tr.positions)
        self.assertFalse(any("청산 주문 실패" in t for t in tg.texts()), "a filled stop is not an emergency")
        self.assertEqual(store.read_all("journal.jsonl")[-1]["reason"], "STOP")

    def test_reduce_only_rejection_with_position_still_open_alerts(self):
        tr, tg, store = self.started()
        tr.enter("AAAUSDT", fake_signal(tr, "AAAUSDT"), tr.broker.equity())

        def reject(*a, **k):
            raise BinanceError(400, -2022, "ReduceOnly Order is rejected.", "/fapi/v1/order")

        tr.broker.market = reject
        self.assertFalse(tr.exit_position("AAAUSDT", "MANUAL"))
        self.assertIn("AAAUSDT", tr.positions)
        self.assertTrue(any("청산 주문 실패" in t for t in tg.texts()))

    def test_adopts_external_position(self):
        self.fake._fill("AAAUSDT", "BUY", 0.5, self.fake.price("AAAUSDT"), int(START * 1000))
        tr, tg, _ = self.started()
        self.assertIn("AAAUSDT", tr.positions)
        self.assertEqual(len(self.fake.open_algos("AAAUSDT")), 1)
        self.assertTrue(any("발견" in t for t in tg.texts()))

    def test_restart_keeps_managing(self):
        tr, tg, store = self.started()
        tr.enter("AAAUSDT", fake_signal(tr, "AAAUSDT"), tr.broker.equity())
        algo_id = self.fake.open_algos("AAAUSDT")[0]["algoId"]
        tr2, tg2, _ = make_trader(self.fake, self.clock, self.tmp)
        tr2.startup()
        self.assertIn("AAAUSDT", tr2.positions)
        self.assertFalse(any("발견" in t for t in tg2.texts()), "restart must not look like an external position")
        self.assertEqual([a["algoId"] for a in self.fake.open_algos("AAAUSDT")], [algo_id])

    def test_manual_close_via_telegram(self):
        tr, tg, store = self.started()
        tr.enter("AAAUSDT", fake_signal(tr, "AAAUSDT"), tr.broker.equity())
        tr.handle_update({"message": {"chat": {"id": 42}, "text": "/close aaa"}})
        _, kw = tg.sent[-1]
        cb = kw["inline"][0][0][1]
        tr.handle_update({"callback_query": {"id": "1", "data": cb, "message": {"chat": {"id": 42}}}})
        self.assertEqual(self.fake.positions, {})
        self.assertEqual(store.read_all("journal.jsonl")[-1]["reason"], "MANUAL")

    def test_ignores_strangers(self):
        tr, tg, _ = self.started()
        n = len(tg.sent)
        tr.handle_update({"message": {"chat": {"id": 999}, "text": "/closeall"}})
        self.assertEqual(len(tg.sent), n)

    def test_commands_smoke(self):
        tr, tg, _ = self.started()
        tr.enter("AAAUSDT", fake_signal(tr, "AAAUSDT"), tr.broker.equity())
        tr.refresh_data()
        for text in ["/help", "/status", "/pnl", "/scan", "/log", "/log 5", "/settings", "/risk", "/risk 2.5",
                     "/set max_open_positions 2", "/set symbols aaa,bbb", "/set symbols zzz", "/set risk_per_trade_pct 9",
                     "/pause", "/resume", "/version", "/nonsense", "📊 상태", "hello", "/selftest", "/rebase",
                     "/report", "/report 이번달", "/set monthly_report false"]:
            n = len(tg.sent)
            tr.handle_update({"message": {"chat": {"id": 42}, "text": text}})
            self.assertGreater(len(tg.sent), n, f"no reply to {text}")
            self.assertNotIn("처리 중 오류", tg.sent[-1][0], f"{text}: {tg.sent[-1][0]}")
        self.assertEqual(tr.cfg["risk_per_trade_pct"], 2.5)
        self.assertEqual(tr.cfg["symbols"], ["AAAUSDT", "BBBUSDT"])
        self.assertTrue(any("값 오류" in t for t in tg.texts()))

    def test_paper_to_live_switch_fixes_account_modes(self):
        self.fake.dual = True
        self.fake.multi = True
        tr, tg, store = self.started(mode="paper")
        self.assertEqual(tr.state["mode"], "paper")
        tr.handle_update({"message": {"chat": {"id": 42}, "text": "/live"}})
        text, kw = tg.sent[-1]
        self.assertIn("실전 모드로 전환할까요", text)
        cb = kw["inline"][0][0][1]
        tr.handle_update({"callback_query": {"id": "1", "data": cb, "message": {"chat": {"id": 42}}}})
        self.assertEqual(tr.state["mode"], "live")
        self.assertFalse(self.fake.dual)
        self.assertFalse(self.fake.multi)
        self.assertIsInstance(tr.broker, B.LiveBroker)
        self.assertTrue(any("실전 모드 시작" in t for t in tg.texts()))

    def test_live_roundtrip_selftest(self):
        import selftest
        api = make_api(self.fake, self.clock)
        out = selftest.live_roundtrip(api, {}, {}, LOG, candidates=("BBBUSDT",))
        self.assertTrue(out["ok"], out["text"])
        self.assertEqual(self.fake.positions, {})
        self.assertEqual(self.fake.open_algos(), [])


class PaperScenario(unittest.TestCase):
    def test_paper_mode(self):
        tmp = tempfile.mkdtemp()
        try:
            clock = SimClock(START)
            fake = FakeExchange(clock, direction=1, test_days=13)
            tr, tg, store = make_trader(fake, clock, tmp, mode="paper")
            tr.startup()
            b0 = tr.broker.st["balance"]
            for _ in range(int(12 * 86400 / 120)):
                clock.advance(120)
                tr.step()
                self.assertEqual(fake.positions, {}, "paper mode must never touch the real account")
                st = tr.broker.st
                self.assertEqual(set(st["positions"]), set(tr.positions))
                for sym in st["positions"]:
                    self.assertEqual(len([s for s in st["stops"].values() if s["sym"] == sym]), 1)
            tr.act_closeall()
            journal = store.read_all("journal.jsonl")
            self.assertGreaterEqual(len(journal), 1)
            self.assertAlmostEqual(tr.broker.st["balance"] - b0, sum(j["pnl"] for j in journal), delta=0.01)
            self.assertEqual(fake.trades, [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class MonthlyReport(unittest.TestCase):
    """v2.1: one report per month (UTC month = the bot's 09:00 KST trading day), with a futures health check."""

    @staticmethod
    def trade(pnl, ts, r=None):
        return {"ts": ts, "mode": "paper", "sym": "AAAUSDT", "side": S.LONG, "pnl": pnl, "fee": 0.05,
                "funding": -0.01, "r": pnl if r is None else r, "reason": "STOP"}

    def test_text_has_total_btc_and_futures_numbers(self):
        import report as RP
        start, end, label = RP.prev_month(RP.month_start(1_762_000_000))  # 2025-10
        self.assertEqual(label, "2025년 10월")
        hist = [["2025-09-30", 50.0, 40.0], ["2025-10-01", 50.0, 50.0], ["2025-10-15", 52.0, 50.0],
                ["2025-11-01", 55.0, 55.0], ["2025-11-02", 99.0, 99.0]]
        day = 86_400_000
        btc = [[int(start * 1000), "100", "0", "0", "0"], [int(end * 1000), "105", "0", "0", "0"],
               [int(end * 1000) - day, "104", "0", "0", "0"]]
        trades = [self.trade(4.0, start + 100), self.trade(-1.0, start + 200)]
        text = RP.monthly_text(label, "모의(PAPER)", hist, start, end, trades, trades, 3, btc)
        self.assertIn("2025년 10월", text)
        self.assertIn("$100.00 → $110.00 (+10.0%)", text)
        self.assertIn("BTC 보유: +5.0%", text)
        self.assertIn("거래 2건 · 승률 50%", text)
        self.assertIn("수익팩터 4.00", text)
        self.assertIn("리밸런싱 체결 3건", text)
        self.assertNotIn("🩺", text)  # fewer than 10 trades: no verdict
        short = RP.monthly_text(label, "모의(PAPER)", hist[3:4], start, end, [], [], 0)
        self.assertIn("기록이 아직 부족", short)
        self.assertIn("청산된 거래 없음", short)

    def test_health_line_needs_enough_trades(self):
        import report as RP
        losing = [self.trade(-1.0, i) for i in range(16)] + [self.trade(2.0, 100 + i) for i in range(6)]
        self.assertIn("/preset 안정", RP.health_line(losing))           # 22 trades, PF 0.75
        self.assertIn("표본이 적어", RP.health_line(losing[3:]))        # 19 trades, PF 0.92: too early to judge
        winning = [self.trade(1.0, i) for i in range(8)] + [self.trade(-1.0, 50 + i) for i in range(4)]
        self.assertIn("정상 범위", RP.health_line(winning))
        self.assertIsNone(RP.health_line(winning[:5]))

    def test_sent_once_a_month_after_the_daily_close(self):
        import report as RP
        tmp = tempfile.mkdtemp()
        try:
            clock = SimClock(START)
            fake = FakeExchange(clock, direction=1, test_days=1, history_days=60)
            tr, tg, store = make_trader(fake, clock, tmp, mode="paper")
            tr.monthly(START)  # first run after the update: remember the month, send nothing
            self.assertEqual(tg.texts(), [])
            nxt = RP.month_start(START + 32 * 86400)
            tr.monthly(nxt + 300)  # 09:05 KST: the spot run has not happened yet
            self.assertEqual(tg.texts(), [])
            tr.monthly(nxt + 900)
            tr.monthly(nxt + 5000)
            reports = [t for t in tg.texts() if t.startswith("📅 월간 리포트")]
            self.assertEqual(len(reports), 1)
            self.assertIn(RP.month_label(START), reports[0])
            tr.cfg["monthly_report"] = False
            tr.monthly(RP.month_start(nxt + 32 * 86400) + 900)
            self.assertEqual(len([t for t in tg.texts() if t.startswith("📅")]), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_daily_total_is_recorded_without_spot_sleeve(self):
        tmp = tempfile.mkdtemp()
        try:
            clock = SimClock(START)
            fake = FakeExchange(clock, direction=1, test_days=3, history_days=260)
            tr, tg, store = make_trader(fake, clock, tmp, mode="paper")  # spot sleeve off
            tr.startup()
            for _ in range(int(2.2 * 86400 / 600)):
                clock.advance(600)
                tr.step()
            days = [h[0] for h in tr.state["hist"]]
            self.assertGreaterEqual(len(days), 3)
            self.assertEqual(len(days), len(set(days)), "one row per UTC day")
            self.assertTrue(all(h[2] > 0 and h[1] == 0 for h in tr.state["hist"]))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class HttpTransport(unittest.TestCase):
    def test_real_http_roundtrip(self):
        clock = SimClock(START)
        fake = FakeExchange(clock, direction=1, test_days=1, history_days=30)
        srv, base = serve_http(fake)
        try:
            api = BinanceFutures(fake.key, fake.secret, base_url=base, clock=clock, sleep=lambda s: None)
            self.assertEqual(api.server_time(), int(START * 1000))
            self.assertIn("AAAUSDT", api.mark_prices())
            self.assertTrue(api.klines("AAAUSDT", "1h", limit=5))
            acc = api.account()
            self.assertAlmostEqual(float(acc["totalWalletBalance"]), 100.0)
            api.set_leverage("AAAUSDT", 5)
            self.assertEqual(api.set_margin_type("AAAUSDT", "ISOLATED").get("code"), 200)
            self.assertEqual(api.set_margin_type("AAAUSDT", "ISOLATED").get("code"), -4046)
            r = api.new_order(symbol="AAAUSDT", side="BUY", type="MARKET", quantity="0.10", newOrderRespType="RESULT")
            self.assertEqual(r["status"], "FILLED")
            with self.assertRaises(BinanceError) as cm:
                api.new_order(symbol="AAAUSDT", side="SELL", type="STOP_MARKET", stopPrice="90", quantity="0.10")
            self.assertEqual(cm.exception.code, -4120)
            a = api.new_algo_order(symbol="AAAUSDT", side="SELL", type="STOP_MARKET", triggerPrice="90.00",
                                   quantity="0.10", reduceOnly=True, workingType="MARK_PRICE")
            self.assertEqual(len(api.open_algo_orders("AAAUSDT")), 1)
            api.cancel_algo_order(algo_id=a["algoId"])
            self.assertEqual(api.open_algo_orders("AAAUSDT"), [])
            bad = BinanceFutures(fake.key, "x" * 64, base_url=base, clock=clock)
            with self.assertRaises(BinanceError) as cm:
                bad.account()
            self.assertEqual(cm.exception.code, -1022)
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
