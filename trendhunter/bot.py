#!/usr/bin/env python3
"""TrendHunter v2 — Binance spot momentum sleeve + USD-M futures breakout sleeve, operated from Telegram.

Run:  python3 bot.py [--data ~/trendhunter-data]
Mode starts as PAPER (simulated fills on live prices). Switch with /live in Telegram.
Futures sleeve: 4h Donchian breakout in a 1d EMA uptrend, exchange-side stops (strategy.py).
Spot sleeve: daily time-series momentum + volatility targeting on the top-volume majors (spot_strategy.py).
Capital is split between the two wallets (alloc_spot_pct) and rebalanced with universal transfers.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import math
import os
import queue
import secrets as pysecrets
import signal
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import report as RP  # noqa: E402
import risk as R  # noqa: E402
import strategy as S  # noqa: E402
from binance_api import BinanceError, BinanceFutures, NetworkError, parse_filters  # noqa: E402
from binance_spot import BinanceSpot  # noqa: E402
from brokers import LiveBroker, PaperBroker  # noqa: E402
from spot import SpotManager, utc_day  # noqa: E402
from store import Store, load_config  # noqa: E402
from telegram_client import Poller, Telegram  # noqa: E402

KST = timezone(timedelta(hours=9))
TF_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200,
              "4h": 14400, "6h": 21600, "8h": 28800, "12h": 43200, "1d": 86400}

SIDE_KO = {S.LONG: "롱", S.SHORT: "숏"}
SIDE_ICON = {S.LONG: "🟢", S.SHORT: "🔴"}
EXIT_KO = {
    "STOP": "손절",
    "TRAIL": "추적 손절·이익 보호",
    "TIME": "시간 손절",
    "REVERSE": "반대 신호",
    "SOFT_STOP": "봇 백업 손절",
    "STOP_NOW": "손절가 통과 즉시 청산",
    "NO_STOP": "손절 주문 실패 안전 청산",
    "MANUAL": "수동 청산",
    "EXTERNAL": "외부 청산(앱에서 직접 등)",
}

KEYBOARD = [["📊 상태", "💰 손익"], ["🪙 현물", "🔎 스캔"], ["📜 기록", "⚖️ 배분"], ["⏸ 정지", "▶️ 재개"], ["❓ 도움말"]]
LABEL_CMD = {"📊 상태": "status", "💰 손익": "pnl", "🔎 스캔": "scan", "📜 기록": "log", "🪙 현물": "spot",
             "⚖️ 배분": "alloc", "⏸ 정지": "pause", "▶️ 재개": "resume", "❓ 도움말": "help"}
PRESETS = {  # name: (spot %, futures risk per trade %, spot vol target) — see the research report
    "안정": (70, 2.0, 0.6),
    "기본": (50, 3.0, 0.6),
    "공격": (40, 4.0, 0.6),
}
PRESET_NOTE = {  # v2.1 rules on Binance data 2020-01 ~ 2026-10, real funding rates
    "안정": "과거 최대낙폭 약 -28%, 2024~26 연 31%",
    "기본": "과거 최대낙폭 약 -35%, 2024~26 연 39%",
    "공격": "과거 최대낙폭 약 -44%, 2024~26 연 48%",
}
COMMANDS = [
    ("status", "현재 상태·보유 포지션"), ("pnl", "손익 성과"), ("spot", "현물 보유·목표 비중"),
    ("alloc", "현물/선물 배분: /alloc 50"), ("preset", "위험 단계: /preset 기본"), ("scan", "종목별 신호 상태"),
    ("log", "최근 판단 기록"), ("pause", "신규 진입 일시정지"), ("resume", "신규 진입 재개"),
    ("close", "포지션 청산: /close BTC"), ("closeall", "전체 청산"), ("risk", "1회 위험 변경: /risk 3"),
    ("settings", "설정 보기"), ("set", "설정 변경: /set 키 값"), ("backtest", "백테스트: /backtest 365"),
    ("selftest", "계정·API 점검"), ("live", "실전 모드 전환"), ("paper", "모의 모드 전환"),
    ("update", "코드 업데이트"), ("rollback", "직전 버전 복구"), ("restart", "봇 재시작"),
    ("rebase", "자본 기준점 재설정"), ("export", "거래기록 CSV 받기"), ("report", "월간 리포트"), ("help", "도움말"),
]
SETTABLE = {
    "risk_per_trade_pct": (float, 0.25, 5.0),
    "max_open_positions": (int, 1, 6),
    "max_same_direction": (int, 1, 6),
    "max_entries_per_day": (int, 1, 24),
    "daily_loss_limit_pct": (float, 2.0, 30.0),
    "max_leverage": (int, 1, 20),
    "equity_floor_pct": (float, 0.0, 95.0),
    "loss_streak_limit": (int, 2, 10),
    "loss_streak_pause_hours": (float, 0.0, 72.0),
    "cooldown_after_loss_hours": (float, 0.0, 72.0),
    "time_stop_hours": (float, 0.0, 480.0),
    "allow_short": (bool, None, None),
    "allow_long": (bool, None, None),
    "adx_min": (float, 0.0, 50.0),
    "symbols": (list, None, None),
    "spot_enabled": (bool, None, None),
    "alloc_spot_pct": (float, 0.0, 100.0),
    "alloc_band_pct": (float, 2.0, 30.0),
    "spot_vol_target": (float, 0.2, 1.5),
    "spot_universe_size": (int, 1, 10),
    "spot_band_pct": (float, 1.0, 20.0),
    "spot_candidates": ("spotlist", None, None),
    "update_url": (str, None, None),
    "brief_morning_kst": ("hhmm", None, None),
    "brief_evening_kst": ("hhmm", None, None),
    "monthly_report": (bool, None, None),
}


# ---------------------------------------------------------------- formatting

def short(sym):
    return sym[:-4] if sym.endswith("USDT") else sym


def usd(x, signed=True):
    if not signed:
        return f"${x:,.2f}"
    sign = "+" if x > 0.004 else ("-" if x < -0.004 else "")
    return f"{sign}${abs(x):,.2f}"


def px(x):
    if x is None:
        return "-"
    ax = abs(x)
    if ax >= 1000:
        return f"{x:,.1f}"
    if ax >= 100:
        return f"{x:,.2f}"
    if ax >= 1:
        return f"{x:,.4f}"
    return f"{x:.5f}"


def tfl(tf):
    return tf.upper()


def kst(ts):
    return datetime.fromtimestamp(ts, KST)


def hhmm(ts):
    return kst(ts).strftime("%m/%d %H:%M")


def setup_logging(store):
    log = logging.getLogger("trendhunter")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(store.path("bot.log"), maxBytes=1_000_000, backupCount=5,
                                              encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log


# ---------------------------------------------------------------- trader

class Trader:
    def __init__(self, cfg, store, api, tg, log, clock=time.time, sleep=time.sleep, public_api_factory=None,
                 spot_api=None, spot_public_factory=None):
        self.cfg = cfg
        self.store = store
        self.api = api
        self.tg = tg
        self.log = log
        self.clock = clock
        self.sleep = sleep
        self.public_api_factory = public_api_factory or (lambda: BinanceFutures(base_url=api.base))
        self.spot_api = spot_api
        self.spot_public_factory = spot_public_factory or (
            lambda: BinanceSpot(base_url=self.spot_api.base if self.spot_api else "https://api.binance.com"))
        self.spot = None
        self.spot_ready = False
        self.spot_next_try = 0.0
        st = store.load_state() or {}
        st.setdefault("mode", "paper")
        st.setdefault("paused", False)
        st.setdefault("positions", {})
        st.setdefault("ledger", None)
        st.setdefault("last_candle", 0)
        st.setdefault("briefs", {})
        st.setdefault("blocked", None)
        st.setdefault("equity", None)
        st.setdefault("max_dd", 0.0)
        st.setdefault("spot", {})
        st.setdefault("total", {})
        st.setdefault("hist", [])
        self.state = st
        self.filters = {}
        self.filters_loaded_at = 0.0
        self.data = {}
        self.marks = {}
        self.broker = None
        self.pending = {}
        self.exit_code = None
        self.bg_busy = None
        self.err_count = 0
        self.err_alerted = False
        self.last_alert = {}

    # ------------------------------------------------------------ basics
    @property
    def p(self):
        return self.cfg

    @property
    def positions(self):
        return self.state["positions"]

    @property
    def ledger(self):
        return self.state["ledger"]

    def now(self):
        return self.clock()

    def now_ms(self):
        return int(self.clock() * 1000)

    def symbols(self):
        return [s for s in self.cfg["symbols"] if s in self.filters] if self.filters else list(self.cfg["symbols"])

    def mode_ko(self):
        return "실전(LIVE)" if self.state["mode"] == "live" else "모의(PAPER)"

    def save(self):
        self.store.save_state(self.state)

    def event(self, kind, **kw):
        try:
            self.store.event(kind, ts=self.now(), **kw)
        except Exception as e:  # logging must never break trading
            self.log.warning("event log failed: %s", e)

    def send(self, text, **kw):
        self.log.info("TG> %s", text.replace("\n", " | ")[:300])
        return self.tg.send(text, **kw) if self.tg else None

    def alert(self, key, text, every=1800):
        now = self.now()
        if now - self.last_alert.get(key, 0) >= every:
            self.last_alert[key] = now
            self.send(text)

    def make_broker(self):
        if self.state["mode"] == "live":
            return LiveBroker(self.api, self.filters, self.cfg, self.log)
        return PaperBroker(self.api, self.filters, self.cfg, self.store.path("paper.json"), self.log, self.clock)

    # ------------------------------------------------------------ lifecycle
    def startup(self):
        self.api.sync_time()
        self.load_filters()
        self.broker = self.make_broker()
        if self.state["mode"] == "live":
            self.check_account_settings(fix=True)
        for s in self.symbols():
            self.broker.prepare(s)
        self.marks = self.api.mark_prices(self.mark_symbols())
        self.broker.tick(self.marks)
        eq = self.broker.equity()
        now = self.now()
        if not self.ledger:
            self.state["ledger"] = R.new_ledger(eq["equity"], now)
        self.state["equity"] = eq["equity"]
        self.refresh_data()
        tf = TF_SECONDS[self.cfg["signal_tf"]]
        cur_open = int(now // tf) * tf
        if not self.state.get("last_candle"):
            self.state["last_candle"] = (cur_open - tf) * 1000  # do not act on a candle that closed before start
        self.reconcile(startup=True)
        self.spot_startup()
        self.save()
        if self.tg:
            self.tg.set_commands(COMMANDS)
        lines = [f"🤖 {self.cfg['bot_name']} v{self.cfg['version']} 시작 — {self.mode_ko()}",
                 f"선물 {usd(eq['equity'], False)} | 감시 {len(self.symbols())}종목 | 1회 위험 {self.cfg['risk_per_trade_pct']}%",
                 f"보유 포지션 {len(self.positions)}개"]
        if self.cfg.get("spot_enabled", True):
            if self.spot_ready:
                sv = self.spot.cache or {}
                lines.append(f"현물 {usd(sv.get('equity', 0.0), False)} | 목표 배분 현물 {self.cfg['alloc_spot_pct']:g}% : 선물 "
                             f"{100 - float(self.cfg['alloc_spot_pct']):g}% | 리밸런싱 매일 09:05 KST")
            else:
                lines.append("⚠️ 현물 모듈 준비 실패 — 10분마다 재시도합니다 (/selftest 로 점검)")
        if self.state.get("blocked"):
            lines.append(f"⚠️ 신규 진입 차단: {self.state['blocked']}")
        if self.state.get("paused"):
            lines.append("⏸ 일시정지 상태입니다 (/resume 으로 재개)")
        self.send("\n".join(lines), keyboard=KEYBOARD)

    def load_filters(self):
        info = self.api.exchange_info()
        self.filters.clear()
        self.filters.update(parse_filters(info))
        self.filters_loaded_at = self.now()
        bad = [s for s in self.cfg["symbols"] if s not in self.filters or self.filters[s]["status"] != "TRADING"]
        if bad:
            self.alert("bad_symbols", f"⚠️ 거래 불가 종목 제외: {', '.join(bad)}", every=86400)

    def check_account_settings(self, fix=False):
        problems = []
        try:
            has_pos = bool(self.broker.positions()) if self.broker else False
            if self.api.get_position_mode():
                if fix and not has_pos:
                    self.api.set_position_mode(False)
                    self.send("🔧 포지션 모드를 헤지 → 단방향(One-way)으로 바꿨습니다.")
                else:
                    problems.append("헤지 모드 — 단방향(One-way)으로 바꿔주세요")
            if self.api.get_multi_assets():
                if fix and not has_pos:
                    self.api.set_multi_assets(False)
                    self.send("🔧 멀티에셋 모드 → 단일자산 모드로 바꿨습니다(격리 마진 사용을 위해).")
                else:
                    problems.append("멀티에셋 모드 — 단일자산 모드로 바꿔주세요")
        except (BinanceError, NetworkError) as e:
            problems.append(f"계정 설정 확인 실패: {e}")
        self.state["blocked"] = "; ".join(problems) or None
        return problems

    # ------------------------------------------------------------ market data
    def refresh_data(self):
        now = self.now()
        now_ms = int(now * 1000)
        tf_sec = TF_SECONDS[self.cfg["signal_tf"]]
        for sym in self.symbols():
            try:
                raw1 = self.api.klines(sym, self.cfg["signal_tf"], limit=500)
                raw4 = self.api.klines(sym, self.cfg["trend_tf"], limit=1000)
            except (BinanceError, NetworkError) as e:
                self.log.warning("klines %s: %s", sym, e)
                self.data.pop(sym, None)
                continue
            c1 = S.candles_from_klines(raw1, now_ms)
            c4 = S.candles_from_klines(raw4, now_ms)
            if len(c1["c"]) < 60 or not c4["c"]:
                self.data.pop(sym, None)
                continue
            ind = S.prepare_1h(c1, self.cfg)
            r4 = S.prepare_4h(c4, self.cfg)
            i = len(ind["c"]) - 1
            reg = S.regime_at(r4, ind["tc"][i])
            sig = S.evaluate(ind, i, reg, self.cfg)
            fresh = (now_ms - ind["tc"][i]) <= (tf_sec * 1000 + 180_000)
            self.data[sym] = {"ind": ind, "r4": r4, "sig": sig, "updated": now, "fresh": fresh}

    # ------------------------------------------------------------ main step
    def mark_symbols(self):
        return sorted(set(self.symbols()) | set(self.positions))

    def step(self):
        now = self.now()
        self.marks = self.api.mark_prices(self.mark_symbols())
        self.broker.tick(self.marks)
        if now - self.filters_loaded_at > 6 * 3600:
            self.load_filters()
        self.track_equity(now)
        self.reconcile()
        tf = TF_SECONDS[self.cfg["signal_tf"]]
        cur_open = int(now // tf) * tf
        last_closed_ms = (cur_open - tf) * 1000
        if last_closed_ms > int(self.state.get("last_candle") or 0) and now - cur_open >= 5:
            self.state["last_candle"] = last_closed_ms
            self.on_candle(now, allow_entries=(now - cur_open) <= 600)
        self.spot_step(now)
        if self.state.get("hist_day") != utc_day(now):
            self.record_daily(now)  # one total-equity row per UTC day, with or without the spot sleeve
        self.briefs(now)
        self.monthly(now)
        self.save()

    def track_equity(self, now):
        eq = self.broker.equity()
        e = eq["equity"]
        self.state["equity"] = e
        self.state["available"] = eq["available"]
        L = self.ledger
        if L is None:
            self.state["ledger"] = L = R.new_ledger(e, now)
        R.roll_day(L, now, e)
        if e > 0:  # a wallet that started empty (money arrived later by transfer): guards need a real baseline
            if float(L.get("start_equity") or 0) <= 0:
                L["start_equity"] = L["peak_equity"] = e
            if float(L.get("day_start_equity") or 0) <= 0:
                L["day_start_equity"] = e
        L["peak_equity"] = max(float(L.get("peak_equity") or e), e)
        if L["peak_equity"] > 0:
            dd = (e - L["peak_equity"]) / L["peak_equity"] * 100.0
            self.state["max_dd"] = min(float(self.state.get("max_dd") or 0.0), dd)
        floor = float(L.get("start_equity", 0)) * float(self.cfg["equity_floor_pct"]) / 100.0
        if e < floor and not self.state.get("floor_alerted") and not self.state.get("pending_transfer"):
            self.state["floor_alerted"] = True
            self.send(f"🛑 자본 바닥선 도달 — 잔고 {usd(e, False)} < 바닥선 {usd(floor, False)}\n"
                      "신규 진입을 멈췄습니다. 보유 포지션은 손절이 걸린 채 유지됩니다.\n"
                      "전략을 점검한 뒤 /rebase (현재 잔고를 새 기준으로) 로 해제하세요.")
            self.event("GUARD", code="EQUITY_FLOOR", equity=e)
        return eq

    # ------------------------------------------------------------ spot sleeve & allocation
    def spot_startup(self):
        """(Re)build the spot sleeve from scratch for the CURRENT mode. Failures never stop the futures sleeve."""
        self.spot_ready = False
        self.spot = None
        if not self.cfg.get("spot_enabled", True):
            return False
        self.spot_next_try = self.now() + 600
        try:
            api = self.spot_api_or_new()
            if self.state["mode"] == "live":
                api.sync_time()
            mgr = SpotManager(self.cfg, self.store, api, self.spot_public_factory(), self.log,
                              self.send, self.state.setdefault("spot", {}), clock=self.clock, sleep=self.sleep)
            mgr.load_filters()
            if not mgr.tradable():
                raise RuntimeError("현물 종목 정보를 받지 못했습니다")
            mgr.make_ex(self.state["mode"])
            mgr.value(refresh=True)
        except Exception as e:  # noqa: BLE001 — anything here must not stop the futures sleeve
            self.log.warning("spot startup: %s", e)
            hint = ""
            if isinstance(e, BinanceError) and e.code == -2015:
                hint = "\nAPI 키에 'Enable Spot & Margin Trading' 권한과 서버 IP 등록이 필요합니다."
            self.alert("spot_start", f"⚠️ 현물 모듈 시작 실패: {e}{hint}\n선물 매매는 정상 진행합니다. 10분마다 재시도.",
                       every=6 * 3600)
            return False
        self.spot = mgr
        self.spot_ready = True
        return True

    def spot_mode_ok(self):
        return self.spot is not None and self.spot.ex is not None and self.spot.ex.name == self.state["mode"]

    def spot_step(self, now):
        if not self.cfg.get("spot_enabled", True) or getattr(self, "spot_busy", False):
            return
        if self.spot_ready and not self.spot_mode_ok():
            self.spot_ready = False  # never let a paper bot hold a live back-end, or the reverse
            self.spot_next_try = 0.0
        if not self.spot_ready:
            if now >= self.spot_next_try:
                self.spot_startup()
            if not self.spot_ready:
                return
        try:
            self.resolve_pending_transfer(now)
            if self.spot.due(now):
                self.daily_spot(now)
            else:
                self.spot.value()  # cached; refreshes at most every 10 minutes
            self.track_total(now)
        except Exception as e:  # spot problems must never stop futures risk management
            st = self.state["spot"]
            st["fails"] = int(st.get("fails", 0)) + 1
            st["retry_at"] = now + 600
            self.log.error("spot step failed: %s\n%s", e, traceback.format_exc())
            self.event("ERROR", where="spot", msg=str(e)[:300])
            if st["fails"] >= 3:
                self.alert("spot_fail", f"⚠️ 현물 리밸런싱 실패 {st['fails']}회: {e}\n10분 뒤 다시 시도합니다.", every=6 * 3600)
            if st["fails"] % 6 == 0:  # an hour of failures: rebuild the sleeve (fresh filters, fresh clients)
                self.spot_ready = False
                self.spot_next_try = now + 600

    def alloc_plan(self, now):
        """Signed USDT amount to move: + = spot -> futures, - = futures -> spot (0 = inside the band)."""
        spot_eq = self.spot.value(refresh=True)["equity"]
        fut = self.broker.equity()
        total = spot_eq + fut["equity"]
        if total <= 2:
            return 0.0
        target = float(self.cfg.get("alloc_spot_pct", 50)) / 100.0
        drift = spot_eq / total - target
        band = float(self.cfg.get("alloc_band_pct", 10)) / 100.0
        if datetime.fromtimestamp(now, timezone.utc).day == 1:
            band = min(band, 0.02)  # monthly: pull back close to the target
        if self.state.get("force_alloc"):
            band = 0.005  # the user just changed the target
        if abs(drift) < band:
            return 0.0
        amt = drift * total
        if amt < 0:  # futures -> spot: only free margin can move
            amt = -min(-amt, max(fut["available"] - 1.0, 0.0))
        if abs(amt) < max(1.0, total * 0.003):
            return 0.0
        return amt

    def transfer(self, amount, to_futures, why="배분 조정"):
        """Move USDT between the spot and futures wallets. Returns the amount moved (0 on failure / not yet known)."""
        amount = math.floor(max(amount, 0.0) * 100) / 100
        if amount < 1.0:
            return 0.0
        if not self.spot_mode_ok():
            raise RuntimeError("현물 모듈과 모드가 맞지 않아 이체를 멈췄습니다")
        before = self.broker.equity()
        fut_before = before["equity"]
        if self.state["mode"] != "live":
            self.broker.adjust_balance(amount if to_futures else -amount)
            self.spot.ex.adjust_usdt(-amount if to_futures else amount)
            self._book_transfer(amount, to_futures, fut_before, why)
            return amount
        t0 = self.server_ms()
        try:
            self.spot.ex.transfer(to_futures, amount)
        except (BinanceError, NetworkError) as e:
            if isinstance(e, BinanceError) and (e.status or 0) < 500:
                self.event("ERROR", where="transfer", msg=str(e)[:300])
                self.alert("transfer", f"⚠️ 현물↔선물 자동 이체 실패: {e}\n"
                           "API 키에 '범용 이체(Permits Universal Transfer)' 권한이 있는지 확인하세요. "
                           "없으면 비율 조정만 멈추고 두 전략은 각자 지갑에서 계속 운용됩니다.", every=86400)
                return 0.0
            # outcome unknown: ask the transfer history, then look at the futures wallet itself
            ok = self.confirm_transfer(amount, to_futures, t0)
            if ok is not True and self.wallet_moved(before.get("wallet"), amount, to_futures):
                ok = True
            if ok is None:
                self.state["pending_transfer"] = {"amount": amount, "to_futures": to_futures, "t0": t0,
                                                  "fut_before": fut_before, "wallet_before": before.get("wallet"),
                                                  "why": why, "until": self.now() + 3600}
                self.save()
                self.alert("transfer_pending", f"⏳ 이체 응답이 없어({e}) 결과를 확인하는 중입니다. 확인 전까지 배분 조정을 멈춥니다.",
                           every=3600)
                return 0.0
            if not ok:
                self.event("ERROR", where="transfer", msg=f"not executed: {e}"[:300])
                return 0.0
        self._book_transfer(amount, to_futures, fut_before, why)
        return amount

    def server_ms(self):
        """Binance server time (transfer history is filtered and stamped in it, not in our clock)."""
        api = self.spot_api_or_new()
        return int(self.now_ms() + float(getattr(api, "time_offset_ms", 0.0) or 0.0))

    def wallet_moved(self, wallet_before, amount, to_futures):
        """True if the futures wallet balance changed by about the transfer amount, in the right direction."""
        if wallet_before is None:
            return False
        for _ in range(2):
            try:
                now_w = self.broker.equity()["wallet"]
                break
            except (BinanceError, NetworkError):
                self.sleep(2.0)
        else:
            return False
        delta = now_w - float(wallet_before)
        expect = amount if to_futures else -amount
        return abs(delta - expect) <= max(0.3, abs(amount) * 0.05)

    def confirm_transfer(self, amount, to_futures, t0):
        """True / False from Binance's transfer history, None while it cannot be told."""
        kind = "MAIN_UMFUTURE" if to_futures else "UMFUTURE_MAIN"
        for _ in range(3):
            try:
                rows = self.spot_api_or_new().transfer_history(kind, start_ms=t0 - 60_000, retries=0)
                break
            except (BinanceError, NetworkError):
                self.sleep(2.0)
        else:
            return None
        for r in rows:
            if abs(float(r.get("amount") or 0) - amount) < 0.011 and int(r.get("timestamp") or 0) >= t0 - 60_000:
                st = str(r.get("status") or "").upper()
                if st == "CONFIRMED":
                    return True
                if st == "FAILED":
                    return False
                return None
        return None if self.server_ms() - t0 < 600_000 else False

    def resolve_pending_transfer(self, now):
        p = self.state.get("pending_transfer")
        if not p:
            return
        ok = self.confirm_transfer(p["amount"], p["to_futures"], p["t0"])
        if ok is not True and self.wallet_moved(p.get("wallet_before"), p["amount"], p["to_futures"]):
            ok = True
        if ok is None and now < float(p.get("until") or 0):
            return
        self.state["pending_transfer"] = None
        if ok:
            self._book_transfer(p["amount"], p["to_futures"], p["fut_before"], p.get("why", "배분 조정"))
            self.send(f"✅ 확인 중이던 이체가 처리됐습니다: {'현물→선물' if p['to_futures'] else '선물→현물'} "
                      f"{usd(p['amount'], False)}")
        else:
            self.send("ℹ️ 확인 중이던 이체는 실행되지 않은 것으로 판단했습니다(이체 기록·선물 지갑 모두 변화 없음). "
                      "다음 점검 때 다시 맞춥니다. 잔고가 이상해 보이면 /rebase 로 기준점을 다시 잡으세요.")
        self.save()

    def _book_transfer(self, amount, to_futures, fut_before, why):
        fut_after = fut_before + (amount if to_futures else -amount)
        L = self.ledger
        if L is not None:
            if fut_before > 0 and fut_after > 0:
                k = fut_after / fut_before
                for key in ("start_equity", "peak_equity", "day_start_equity"):
                    L[key] = float(L.get(key) or 0.0) * k
            if float(L.get("start_equity") or 0) <= 0 < fut_after:
                L["start_equity"] = L["peak_equity"] = L["day_start_equity"] = fut_after
            floor = float(L.get("start_equity") or 0) * float(self.cfg["equity_floor_pct"]) / 100.0
            if self.state.get("floor_alerted") and fut_after >= floor:
                self.state["floor_alerted"] = False  # the alert came from an unscaled baseline
        st = self.state["spot"]
        st["net_in"] = float(st.get("net_in") or 0.0) + (-amount if to_futures else amount)
        if self.spot is not None:
            self.spot.cache = None
        self.state["equity"] = fut_after
        rec = {"ts": self.now(), "mode": self.state["mode"], "to": "futures" if to_futures else "spot",
               "amount": amount, "why": why}
        self.store.append("transfers.jsonl", rec)
        self.event("TRANSFER", msg=f"{'현물→선물' if to_futures else '선물→현물'} {amount:.2f}")
        self.save()

    def daily_spot(self, now):
        if not self.spot_mode_ok():
            raise RuntimeError("현물 모듈과 모드가 맞지 않습니다")
        if self.state.get("pending_transfer"):
            return  # wait until the last transfer is confirmed before moving more money
        self.spot.load_filters()  # cheap; also drops a delisted candidate
        if not self.spot.tradable():
            raise RuntimeError("거래 가능한 현물 종목이 없습니다(바이낸스 점검 중일 수 있음)")
        amt = self.alloc_plan(now)
        moved_in = moved_out = 0.0
        if amt < 0:
            moved_in = self.transfer(-amt, to_futures=False)
        res = self.spot.rebalance(now, reserve=amt if amt > 0 else 0.0)
        if amt > 0:
            cash = self.spot.value(refresh=True)["usdt_free"]
            moved_out = self.transfer(min(amt, cash * 0.999), to_futures=True)
        if not amt or moved_in or moved_out:
            self.state["force_alloc"] = False
        self.record_daily(now)
        if res["trades"] or res["errors"] or moved_in or moved_out:
            lines = ["🪙 현물 리밸런싱 (일봉 마감 기준)"]
            if moved_in:
                lines.append(f"↔️ 선물 → 현물 {usd(moved_in, False)} 이체 (목표 배분 유지)")
            if moved_out:
                lines.append(f"↔️ 현물 → 선물 {usd(moved_out, False)} 이체 (목표 배분 유지)")
            for t in res["trades"]:
                lines.append(f"{'🟢 매수' if t['side'] == 'BUY' else '🔴 매도'} {short(t['sym'])} {usd(t['quote'], False)} "
                             f"(목표 {t['target_w'] * 100:.0f}%)")
            for e in res["errors"]:
                lines.append(f"⚠️ {e}")
            lines += self.spot.lines(detail=False)
            self.send("\n".join(lines), silent=True)

    def record_daily(self, now):
        try:
            sv = self.spot.value()["equity"] if self.spot_ready else 0.0
        except (BinanceError, NetworkError) as e:  # try again on the next step
            self.log.warning("daily record: %s", e)
            return
        fv = float(self.state.get("equity") or 0.0)
        day = utc_day(now)
        self.state["hist_day"] = day
        h = self.state.setdefault("hist", [])
        if h and h[-1][0] == day:
            h[-1] = [day, round(sv, 4), round(fv, 4)]
        else:
            h.append([day, round(sv, 4), round(fv, 4)])
        del h[:-1500]

    def total_equity(self):
        fv = float(self.state.get("equity") or 0.0)
        sv = (self.spot.cache or {}).get("equity", 0.0) if self.spot_ready else 0.0
        return fv + sv, sv, fv

    def track_total(self, now):
        tot, sv, fv = self.total_equity()
        T = self.state.setdefault("total", {})
        if not T.get("base"):
            T["base"] = tot
            T["since"] = now
        T["peak"] = max(float(T.get("peak") or tot), tot)
        if T["peak"] > 0:
            T["mdd"] = min(float(T.get("mdd") or 0.0), (tot - T["peak"]) / T["peak"] * 100)
        T["last"] = tot

    # ------------------------------------------------------------ reconcile & protection
    def reconcile(self, startup=False):
        ex = self.broker.positions()
        known = set(self.positions)  # symbols handled below must not be "adopted" in the same pass
        for sym, pos in list(self.positions.items()):
            e = ex.get(sym)
            if not e or (e["amt"] > 0) != (pos["side"] == S.LONG):
                self.finalize(sym, pos)
                continue
            qty = abs(e["amt"])
            if abs(qty - pos["qty"]) > 1e-12:
                pos["qty"] = qty
            m = self.marks.get(sym) or e["mark"]
            pos["mark"] = m
            pos["upnl"] = e["upnl"]
            pos["liq"] = e.get("liq", 0.0)
            if m:
                pos["extreme"] = max(pos["extreme"], m) if pos["side"] == S.LONG else min(pos["extreme"], m)
            if not self.ensure_stop(sym, pos):
                continue
            breached = m and ((pos["side"] == S.LONG and m <= pos["stop"]) or
                              (pos["side"] == S.SHORT and m >= pos["stop"]))
            if breached:
                pos["breach"] = int(pos.get("breach", 0)) + 1
                if pos["breach"] >= 3:
                    self.alert(f"soft_{sym}", f"⚠️ {short(sym)}: 가격이 손절가를 넘었는데 거래소 손절이 체결되지 않아 봇이 직접 청산합니다.", every=60)
                    self.exit_position(sym, "SOFT_STOP")
                    continue
            else:
                pos["breach"] = 0
            self.maybe_trail(sym, pos)
        for sym, e in ex.items():
            if sym in self.positions or sym in known:
                continue
            if sym not in self.cfg["symbols"]:
                self.alert(f"foreign_{sym}", f"ℹ️ 봇 감시 목록 밖의 포지션({short(sym)})은 관리하지 않습니다.", every=86400)
                continue
            self.adopt(sym, e)

    def ensure_stop(self, sym, pos):
        """Make sure exactly one protective stop sits on the exchange. Returns False if the position was closed."""
        close_side = "SELL" if pos["side"] == S.LONG else "BUY"
        try:
            stops = self.broker.open_stops(sym)
        except (BinanceError, NetworkError) as e:
            self.log.warning("open stops %s: %s", sym, e)
            return True
        mine = [s for s in stops if s["side"] == close_side and (s["reduce_only"] or s["close_position"])]
        ids = {s["id"] for s in mine}
        if pos.get("stop_id") in ids:
            ours = next(s for s in mine if s["id"] == pos["stop_id"])
            for s in mine:
                if s["id"] != pos["stop_id"]:
                    self.broker.cancel_stop(sym, s["id"])
            if not ours["close_position"] and abs(ours["qty"] - pos["qty"]) > 1e-9:
                self.replace_stop(sym, pos, pos["stop"], note=False)
            return True
        if mine:
            # adopt the most protective stop that already exists, drop the rest
            if pos["side"] == S.LONG:
                best = max(mine, key=lambda s: s["trigger"])
                take = best["trigger"] >= pos["stop"]
            else:
                best = min(mine, key=lambda s: s["trigger"])
                take = best["trigger"] <= pos["stop"]
            for s in mine:
                if s["id"] != best["id"] or not take:
                    self.broker.cancel_stop(sym, s["id"])
            if take:
                pos["stop_id"] = best["id"]
                pos["stop"] = best["trigger"]
                self.save()
                return True
        if pos.get("stop_id"):
            self.alert(f"stop_gone_{sym}", f"⚠️ {short(sym)} 손절 주문이 거래소에서 사라져 다시 걸었습니다.", every=600)
        return self.place_stop_safe(sym, pos)

    def place_stop_safe(self, sym, pos, trigger=None):
        trigger = pos["stop"] if trigger is None else trigger
        close_side = "SELL" if pos["side"] == S.LONG else "BUY"
        last = None
        for attempt in range(3):
            try:
                sid = self.broker.place_stop(sym, close_side, pos["qty"], trigger)
                pos.update(stop_id=sid, stop=trigger, stop_set_ms=self.now_ms())
                self.save()
                return True
            except BinanceError as e:
                last = e
                if e.code == -2021:  # would immediately trigger: price is already through the stop
                    self.exit_position(sym, "STOP_NOW")
                    return False
            except NetworkError as e:
                last = e
                try:
                    for s in self.broker.open_stops(sym):
                        if s["side"] == close_side and abs(s["trigger"] - trigger) <= abs(trigger) * 1e-6 + 1e-12:
                            pos.update(stop_id=s["id"], stop=trigger, stop_set_ms=self.now_ms())
                            self.save()
                            return True
                except Exception:
                    pass
            self.sleep(1.0)
        self.send(f"🚨 {short(sym)} 손절 주문을 걸지 못했습니다({last}). 안전을 위해 즉시 청산합니다.")
        self.event("ERROR", where="place_stop", sym=sym, msg=str(last))
        self.exit_position(sym, "NO_STOP")
        return False

    def replace_stop(self, sym, pos, new_trigger, note=True):
        close_side = "SELL" if pos["side"] == S.LONG else "BUY"
        old_id, old_stop = pos.get("stop_id"), pos["stop"]
        try:
            sid = self.broker.place_stop(sym, close_side, pos["qty"], new_trigger)
        except BinanceError as e:
            if e.code == -2021:
                self.exit_position(sym, "TRAIL")
            else:
                self.log.warning("replace stop %s: %s", sym, e)
                self.event("ERROR", where="replace_stop", sym=sym, msg=str(e))
            return False
        except NetworkError as e:
            self.log.warning("replace stop %s: %s", sym, e)
            return False
        pos.update(stop_id=sid, stop=new_trigger, stop_set_ms=self.now_ms())
        if old_id and old_id != sid:
            self.broker.cancel_stop(sym, old_id)
        self.save()
        self.event("TRAIL", sym=sym, old=old_stop, new=new_trigger)
        locked = (pos["side"] == S.LONG and new_trigger > pos["entry"]) or \
                 (pos["side"] == S.SHORT and new_trigger < pos["entry"])
        if note and locked and not pos.get("locked_notified"):
            pos["locked_notified"] = True
            gain = abs(new_trigger - pos["entry"]) * pos["qty"]
            self.send(f"🔒 {short(sym)} {SIDE_KO[pos['side']]}: 손절을 수익 구간 {px(new_trigger)}로 올렸습니다 "
                      f"(최소 {usd(gain)} 확보, 수수료 전).", silent=True)
        return True

    def maybe_trail(self, sym, pos, force=False):
        atr_v = pos.get("atr_now") or pos.get("atr")
        if not atr_v:
            return
        f = self.filters.get(sym, R.DEFAULT_FILTER)
        cand = S.trail_stop(pos["side"], pos["entry"], pos["r_dist"], pos["stop"], pos["extreme"], atr_v, self.cfg)
        cand = R.round_step(cand, f["tick"])
        improve = (cand - pos["stop"]) if pos["side"] == S.LONG else (pos["stop"] - cand)
        min_step = max(float(self.cfg["trail_min_step_atr"]) * atr_v, float(f["tick"]))
        if improve < min_step:
            return
        if not force and self.now_ms() - int(pos.get("stop_set_ms") or 0) < 300_000:
            return
        m = self.marks.get(sym)
        if m and ((pos["side"] == S.LONG and m <= cand) or (pos["side"] == S.SHORT and m >= cand)):
            self.exit_position(sym, "TRAIL")
            return
        self.replace_stop(sym, pos, cand)

    # ------------------------------------------------------------ candle close
    def on_candle(self, now, allow_entries=True):
        self.refresh_data()
        for sym, pos in list(self.positions.items()):
            d = self.data.get(sym)
            if not d:
                continue
            ind = d["ind"]
            i = len(ind["c"]) - 1
            start = int(pos["opened_ms"])
            hs = [ind["h"][k] for k in range(len(ind["t"])) if ind["tc"][k] > start]
            ls = [ind["l"][k] for k in range(len(ind["t"])) if ind["tc"][k] > start]
            if hs:
                pos["extreme"] = max(pos["extreme"], max(hs)) if pos["side"] == S.LONG else min(pos["extreme"], min(ls))
            pos["atr_now"] = ind["atr"][i] or pos.get("atr")
            if S.time_stop_due(start, int(now * 1000), pos["side"], pos["entry"], pos["extreme"], pos["r_dist"], self.cfg):
                self.exit_position(sym, "TIME")
                continue
            sig = d["sig"]
            if sig.get("side") and sig["side"] != pos["side"]:
                self.exit_position(sym, "REVERSE")
                continue
            self.maybe_trail(sym, pos, force=True)
        if allow_entries:
            self.try_entries(now)
        self.save()

    def try_entries(self, now):
        cands = [(sym, d["sig"]) for sym, d in self.data.items()
                 if d.get("fresh") and d["sig"].get("side") and sym not in self.positions]
        cands.sort(key=lambda x: -x[1].get("strength", 0))
        for sym, sig in cands:
            eq = self.broker.equity()
            ok, code = R.can_enter(self.ledger, self.positions, sym, sig["side"], now, eq["equity"], self.cfg,
                                   paused=self.state.get("paused"), blocked=bool(self.state.get("blocked")))
            if not ok:
                self.event("SKIP", sym=sym, side=sig["side"], code=code)
                continue
            self.enter(sym, sig, eq)

    def enter(self, sym, sig, eq):
        side = sig["side"]
        f = self.filters[sym]
        mark = self.marks.get(sym) or sig["close"]
        stop_ref, dist = S.initial_stop(side, mark, sig["atr"], self.cfg)
        sz = R.size_position(eq["equity"], eq["available"], mark, dist, self.cfg, f)
        if not sz["ok"]:
            self.event("SKIP", sym=sym, side=side, code=sz["code"])
            return False
        try:
            self.broker.prepare(sym)
            self.broker.set_leverage(sym, sz["lev"])
            fill = self.broker.market(sym, "BUY" if side == S.LONG else "SELL", sz["qty"], tag="E")
        except (BinanceError, NetworkError) as e:
            self.alert(f"entry_{sym}", f"⚠️ {short(sym)} 진입 주문 실패: {e}", every=600)
            self.event("ERROR", where="entry", sym=sym, msg=str(e))
            return False
        if fill["qty"] <= 0 or fill["avg"] <= 0:
            self.alert(f"entry_{sym}", f"⚠️ {short(sym)} 진입 주문이 체결되지 않았습니다(상태 {fill.get('status')}).", every=600)
            return False
        entry = fill["avg"]
        stop = R.round_step(entry - dist if side == S.LONG else entry + dist, f["tick"])
        pos = {
            "sym": sym, "side": side, "qty": fill["qty"], "entry": entry, "stop": stop, "init_stop": stop,
            "r_dist": dist, "atr": sig["atr"], "atr_now": sig["atr"], "opened_ms": self.now_ms(),
            "extreme": entry, "stop_id": None, "stop_set_ms": 0, "lev": sz["lev"],
            "risk_amt": fill["qty"] * dist, "reason": sig["code"], "breach": 0,
            "entry_order_id": fill.get("order_id"),
            "signal": {"regime": sig["regime"], "adx": round(sig["adx"], 1), "rsi": round(sig["rsi"], 1)},
        }
        self.positions[sym] = pos
        R.on_entry(self.ledger, self.now())
        self.save()
        if not self.place_stop_safe(sym, pos):
            return False
        self.event("ENTRY", sym=sym, side=side, qty=pos["qty"], entry=entry, stop=stop, lev=sz["lev"],
                   risk=round(pos["risk_amt"], 4))
        stop_pct = abs(entry - stop) / entry * 100
        eqv = eq["equity"] or 1
        regime_word = S.REGIME_KO.get(sig["regime"], sig["regime"])
        brk = "고점" if side == S.LONG else "저점"
        self.send(
            f"{SIDE_ICON[side]} {SIDE_KO[side]} 진입 — {short(sym)} (x{sz['lev']}, 격리)\n"
            f"진입가 {px(entry)} | 수량 {fill['qty']:g}\n"
            f"손절 {px(stop)} ({'-' if side == S.LONG else '+'}{stop_pct:.2f}%) — 거래소에 걸어둠\n"
            f"위험 {usd(pos['risk_amt'], False)} (잔고의 {pos['risk_amt'] / eqv * 100:.1f}%) | 증거금 {usd(sz['margin'], False)}\n"
            f"근거: {tfl(self.cfg['trend_tf'])} {regime_word} · {tfl(self.cfg['signal_tf'])} {self.cfg['donchian_len']}봉 {brk} 돌파 · ADX {sig['adx']:.0f} · RSI {sig['rsi']:.0f}"
            + ("\n※ 증거금 한도로 위험을 줄였습니다." if sz.get("capped") else ""))
        return True

    # ------------------------------------------------------------ exits
    def exit_position(self, sym, reason):
        pos = self.positions.get(sym)
        if not pos:
            return False
        pos["exit_reason"] = reason
        close_side = "SELL" if pos["side"] == S.LONG else "BUY"
        try:
            self.broker.market(sym, close_side, pos["qty"], reduce_only=True, tag="X")
        except (BinanceError, NetworkError) as e:
            pos.pop("exit_reason", None)
            if isinstance(e, BinanceError) and e.code == -2022 and self.gone_on_exchange(sym, pos):
                # the exchange stop (or a close in the app) got there first: book what really happened, no alarm
                self.finalize(sym, pos)
                return True
            self.send(f"🚨 {short(sym)} 청산 주문 실패: {e}\n거래소 손절 주문은 유지됩니다. 앱에서 확인해 주세요.")
            self.event("ERROR", where="exit", sym=sym, msg=str(e))
            return False
        self.finalize(sym, pos, reason)
        return True

    def gone_on_exchange(self, sym, pos):
        """True only when the exchange positively shows no position on our side of sym."""
        try:
            e = self.broker.positions().get(sym)
        except (BinanceError, NetworkError):
            return False
        return not e or (e["amt"] > 0) != (pos["side"] == S.LONG)

    def finalize(self, sym, pos, reason=None):
        reason = reason or pos.get("exit_reason")
        since = max(int(pos["opened_ms"]) - 3000, int(self.state.get("last_close_ms", {}).get(sym, 0)) + 1)
        try:
            r = self.broker.realized(sym, since, pos["side"], pos.get("entry_order_id"))
        except (BinanceError, NetworkError) as e:
            self.log.warning("realized %s: %s", sym, e)
            r = None
        if (r is None or r["closed_qty"] < pos["qty"] * 0.99) and int(pos.get("fin_tries", 0)) < 6:
            pos["fin_tries"] = int(pos.get("fin_tries", 0)) + 1
            return False
        if r is None:
            r = {"pnl": 0.0, "fee": 0.0, "funding": 0.0, "closed_qty": 0.0, "exit_price": 0.0}
        exit_px = r["exit_price"] or self.marks.get(sym) or pos["entry"]
        pnl = r["pnl"] - r["fee"] + r["funding"]
        if reason is None:
            near = abs(exit_px - pos["stop"]) / max(pos["stop"], 1e-12) < 0.006
            locked = (pos["side"] == S.LONG and pos["stop"] > pos["entry"]) or \
                     (pos["side"] == S.SHORT and pos["stop"] < pos["entry"])
            reason = ("TRAIL" if locked else "STOP") if near else "EXTERNAL"
        try:
            for s in self.broker.open_stops(sym):
                self.broker.cancel_stop(sym, s["id"])
        except (BinanceError, NetworkError):
            pass
        rmult = pnl / pos["risk_amt"] if pos.get("risk_amt") else 0.0
        now = self.now()
        events = R.on_exit(self.ledger, sym, pnl, now, self.cfg)
        hours = (self.now_ms() - int(pos["opened_ms"])) / 3_600_000
        rec = {"ts": now, "mode": self.state["mode"], "sym": sym, "side": pos["side"], "qty": pos["qty"],
               "entry": pos["entry"], "exit": exit_px, "pnl": round(pnl, 6), "fee": round(r["fee"], 6),
               "funding": round(r["funding"], 6), "r": round(rmult, 3), "reason": reason,
               "opened_ms": pos["opened_ms"], "hours": round(hours, 2), "lev": pos.get("lev")}
        self.store.append("journal.jsonl", rec)
        self.positions.pop(sym, None)
        self.state.setdefault("last_close_ms", {})[sym] = self.now_ms()
        self.save()
        self.event("EXIT", sym=sym, side=pos["side"], pnl=round(pnl, 4), r=round(rmult, 2), reason=reason)
        head = "✅ 익절" if pnl > 0 else "❌ 손절" if pnl < 0 else "➖ 본전"
        try:
            eq = self.broker.equity()["equity"]
            self.state["equity"] = eq
        except (BinanceError, NetworkError):
            eq = self.state.get("equity") or 0
        self.send(f"{head} — {short(sym)} {SIDE_KO[pos['side']]} ({EXIT_KO.get(reason, reason)})\n"
                  f"진입 {px(pos['entry'])} → 청산 {px(exit_px)} | 보유 {hours:.1f}시간\n"
                  f"손익 {usd(pnl)} ({rmult:+.2f}R, 수수료·펀딩 포함)\n"
                  f"오늘 누적 {usd(self.ledger['day_realized'])} | 잔고 {usd(eq, False)}")
        for ev in events:
            if ev == "DAILY_LIMIT":
                self.send(f"⛔ 일일 손실 한도(-{self.cfg['daily_loss_limit_pct']}%) 도달 — 오늘(09시 KST 기준) 신규 진입을 멈춥니다.\n"
                          "보유 포지션은 손절이 걸린 채 유지됩니다.")
            elif ev == "STREAK_PAUSE":
                until = self.ledger.get("pause_until", 0)
                self.send(f"⏸ {self.cfg['loss_streak_limit']}연속 손실 — {hhmm(until)} (KST)까지 신규 진입 휴식.")
            self.event("GUARD", code=ev)
        return True

    def adopt(self, sym, e):
        side = S.LONG if e["amt"] > 0 else S.SHORT
        d = self.data.get(sym)
        atr_v = None
        if d:
            atr_v = d["ind"]["atr"][-1]
        if not atr_v:
            atr_v = e["entry"] * 0.01
        f = self.filters.get(sym, R.DEFAULT_FILTER)
        stop, dist = S.initial_stop(side, e["entry"], atr_v, self.cfg)
        m = self.marks.get(sym) or e["mark"]
        if (side == S.LONG and m <= stop) or (side == S.SHORT and m >= stop):
            stop, dist = S.initial_stop(side, m, atr_v, self.cfg)
            dist = abs(e["entry"] - stop)
        stop = R.round_step(stop, f["tick"])
        pos = {"sym": sym, "side": side, "qty": abs(e["amt"]), "entry": e["entry"], "stop": stop, "init_stop": stop,
               "r_dist": dist, "atr": atr_v, "atr_now": atr_v, "opened_ms": self.now_ms(), "extreme": m or e["entry"],
               "stop_id": None, "stop_set_ms": 0, "lev": None, "risk_amt": abs(e["amt"]) * dist,
               "reason": "ADOPTED", "breach": 0, "adopted": True}
        self.positions[sym] = pos
        self.save()
        self.send(f"ℹ️ {short(sym)} {SIDE_KO[side]} 포지션을 발견해 관리합니다(봇이 연 포지션이 아니거나 재시작 복구).\n"
                  f"보호 손절 {px(stop)} 설정 중…")
        self.ensure_stop(sym, pos)
        self.event("ADOPT", sym=sym, side=side, qty=pos["qty"], stop=stop)

    # ------------------------------------------------------------ briefs
    def briefs(self, now):
        k = kst(now)
        today = k.strftime("%Y-%m-%d")
        for name in ("morning", "evening"):
            try:
                hh, mm = [int(x) for x in str(self.cfg[f"brief_{name}_kst"]).split(":")]
            except Exception:
                continue
            sched = k.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if k >= sched and (k - sched) < timedelta(hours=2) and self.state["briefs"].get(name) != today:
                self.state["briefs"][name] = today
                try:
                    self.send(self.brief_text(name, now))
                except Exception as e:
                    self.log.warning("brief failed: %s", e)

    def monthly(self, now):
        """Once a month, from 09:10 KST on the 1st (after the daily spot run), report the month that just ended."""
        if not self.cfg.get("monthly_report", True):
            return
        key = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")
        last = self.state.get("report_month")
        if last is None:
            self.state["report_month"] = key  # first run (new install or update): the first report is next month
            return
        if last == key or now - RP.month_start(now) < 600:
            return
        self.state["report_month"] = key
        try:
            start, end, label = RP.prev_month(now)
            self.send(self.monthly_text(start, end, label))
        except Exception as e:  # a report must never disturb trading
            self.log.warning("monthly report failed: %s", e)

    def monthly_text(self, start, end, label):
        js = self.journal()
        trades = [t for t in js if start <= t.get("ts", 0) < end]
        fills = [t for t in self.store.read_all("spot_trades.jsonl")
                 if t.get("mode") == self.state["mode"] and start <= t.get("ts", 0) < end]
        try:
            btc = self.api.klines("BTCUSDT", "1d", limit=45)
        except (BinanceError, NetworkError) as e:
            self.log.warning("monthly report BTC prices: %s", e)
            btc = None
        return RP.monthly_text(label, self.mode_ko(), self.state.get("hist") or [], start, end, trades, js[-30:],
                               len(fills), btc)

    def worst_case(self):
        tot = 0.0
        for pos in self.positions.values():
            diff = (pos["stop"] - pos["entry"]) if pos["side"] == S.LONG else (pos["entry"] - pos["stop"])
            tot += diff * pos["qty"]
        return tot

    def brief_text(self, name, now):
        eq = self.state.get("equity") or 0.0
        if name == "morning":
            since = now - 11 * 3600
            title = "☀️ 아침 브리핑 — 밤사이(약 21시~08시)"
        else:
            since = datetime(*kst(now).timetuple()[:3], tzinfo=KST).timestamp()
            title = "🌙 저녁 브리핑 — 오늘(00시~지금 KST)"
        trades = [t for t in self.store.read_all("journal.jsonl") if t.get("ts", 0) >= since
                  and t.get("mode") == self.state["mode"]]
        entries = [e for e in self.store.tail("events.jsonl", 300) if e.get("kind") == "ENTRY" and e.get("ts", 0) >= since]
        pnl = sum(t["pnl"] for t in trades)
        lines = [title, f"모드 {self.mode_ko()} | " + self.total_line()]
        lines.append(f"[선물] 진입 {len(entries)}건 · 청산 {len(trades)}건 · 실현손익 {usd(pnl)}")
        for t in trades[-6:]:
            lines.append(f"  {'✅' if t['pnl'] > 0 else '❌'} {short(t['sym'])} {SIDE_KO.get(t['side'], '')} "
                         f"{usd(t['pnl'])} ({t['r']:+.1f}R, {EXIT_KO.get(t['reason'], t['reason'])})")
        if self.positions:
            lines.append(f"[선물] 보유 {len(self.positions)}개:")
            lines.extend(self.positions_lines())
            wc = self.worst_case()
            lines.append(f"모든 손절이 체결되면: {usd(wc)} (수수료 전)")
        else:
            lines.append("선물 보유 포지션 없음")
        lines.append(self.guard_line())
        if self.spot_ready:
            sp = [t for t in self.store.read_all("spot_trades.jsonl") if t.get("ts", 0) >= since
                  and t.get("mode") == self.state["mode"]]
            lines.append("[현물] " + (f"오늘 리밸런싱 {len(sp)}건" if sp else "변경 없음") + " (매일 09:05 KST)")
            lines.extend(self.spot.lines(detail=True))
        if name == "evening":
            lines.append("폰 반납 후에도 봇과 거래소 손절은 계속 작동합니다. 내일 08시에 다시 보고할게요.")
        return "\n".join(lines)

    # ------------------------------------------------------------ text blocks
    def total_line(self):
        tot, sv, fv = self.total_equity()
        if not self.cfg.get("spot_enabled", True) or not self.spot_ready:
            return f"선물 잔고 {usd(fv, False)}"
        share = sv / tot * 100 if tot else 0.0
        T = self.state.get("total") or {}
        base = float(T.get("base") or 0.0)
        chg = f" (기준 대비 {(tot / base - 1) * 100:+.1f}%)" if base else ""
        return (f"총자산 {usd(tot, False)}{chg} = 현물 {usd(sv, False)} ({share:.0f}%) + 선물 {usd(fv, False)} "
                f"({100 - share:.0f}%) | 목표 현물 {float(self.cfg.get('alloc_spot_pct', 50)):g}%")

    def positions_lines(self):
        out = []
        for sym, p in self.positions.items():
            m = p.get("mark") or self.marks.get(sym) or p["entry"]
            chg = (m - p["entry"]) / p["entry"] * 100 * (1 if p["side"] == S.LONG else -1)
            upnl = (m - p["entry"]) * p["qty"] * (1 if p["side"] == S.LONG else -1)
            rnow = upnl / p["risk_amt"] if p.get("risk_amt") else 0
            stop_pct = (p["stop"] - p["entry"]) / p["entry"] * 100 * (1 if p["side"] == S.LONG else -1)
            lev = f" x{p['lev']}" if p.get("lev") else ""
            out.append(f"{SIDE_ICON[p['side']]} {short(sym)} {SIDE_KO[p['side']]}{lev} | {px(p['entry'])} → {px(m)} ({chg:+.2f}%)")
            out.append(f"   손절 {px(p['stop'])} ({stop_pct:+.2f}%) | 미실현 {usd(upnl)} ({rnow:+.2f}R)")
        return out

    def guard_line(self):
        L = self.ledger or {}
        lim = float(L.get("day_start_equity", 0)) * float(self.cfg["daily_loss_limit_pct"]) / 100
        parts = [f"오늘 진입 {L.get('day_entries', 0)}/{self.cfg['max_entries_per_day']}",
                 f"연속손실 {L.get('loss_streak', 0)}/{self.cfg['loss_streak_limit']}",
                 f"일일손익 {usd(float(L.get('day_realized', 0)))} (한도 -{usd(lim, False)})"]
        if self.now() < float(L.get("pause_until", 0)):
            parts.append(f"휴식 ~{hhmm(L['pause_until'])}")
        if L.get("day_limit_hit"):
            parts.append("일일한도 도달")
        return "가드: " + " | ".join(parts)

    # ------------------------------------------------------------ telegram
    def handle_update(self, u):
        owner = str(self.tg.chat_id) if self.tg else None
        if "callback_query" in u:
            cb = u["callback_query"]
            chat = str(((cb.get("message") or {}).get("chat") or {}).get("id"))
            if owner and chat != owner:
                return
            self.tg.answer_callback(cb["id"])
            self.on_callback(cb.get("data", ""))
            return
        msg = u.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id"))
        if owner and chat != owner:
            return
        text = (msg.get("text") or "").strip()
        if not text:
            return
        if text in LABEL_CMD:
            text = "/" + LABEL_CMD[text]
        if not text.startswith("/"):
            self.send("명령은 아래 버튼이나 /help 를 사용하세요.", keyboard=KEYBOARD)
            return
        parts = text.split()
        cmd = parts[0][1:].split("@")[0].lower()
        args = parts[1:]
        fn = getattr(self, f"cmd_{cmd}", None)
        if not fn:
            self.send(f"모르는 명령: /{cmd}  — /help 참고")
            return
        try:
            fn(args)
        except Exception as e:
            self.log.error("command %s failed: %s\n%s", cmd, e, traceback.format_exc())
            self.send(f"⚠️ /{cmd} 처리 중 오류: {e}")

    def confirm(self, text, action, *args):
        nonce = pysecrets.token_hex(3)
        self.pending = {k: v for k, v in self.pending.items() if v[2] > self.now()}
        self.pending[nonce] = (action, args, self.now() + 180)
        self.send(text + "\n(3분 안에 눌러주세요)", inline=[[("✅ 실행", f"ok:{nonce}"), ("취소", f"no:{nonce}")]])

    def on_callback(self, data):
        kind, _, nonce = data.partition(":")
        item = self.pending.pop(nonce, None)
        if kind == "no":
            self.send("취소했습니다.")
            return
        if not item or item[2] < self.now():
            self.send("만료된 버튼입니다. 명령을 다시 보내주세요.")
            return
        action, args, _ = item
        getattr(self, f"act_{action}")(*args)

    # --- commands
    def cmd_start(self, args):
        self.cmd_help(args)

    def cmd_help(self, args):
        self.send(
            f"🤖 {self.cfg['bot_name']} 명령어\n"
            "📊 /status 상태·보유 포지션\n💰 /pnl 손익 성과\n🪙 /spot 현물 보유·목표 비중\n"
            "⚖️ /alloc 현물/선물 배분 (/alloc 50 = 현물 50%)\n🎚 /preset 안정·기본·공격 위험 단계\n"
            "🔎 /scan 선물 종목별 신호 상태\n📜 /log 최근 판단 기록\n📁 /export 거래기록 CSV(세금 신고용)\n"
            "📅 /report 지난달 리포트 (/report 이번달) — 매월 1일 자동 발송\n"
            "⏸ /pause 신규 진입 정지 · ▶️ /resume 재개\n"
            "/close BTC 한 종목 청산 · /closeall 전체 청산\n"
            "/risk 3 — 1회 위험(%) 변경 · /settings 설정 보기 · /set 키 값\n"
            "/backtest 365 — 최근 N일 백테스트\n/selftest — API·계정 점검 (/selftest live: 실주문 왕복 테스트)\n"
            "/live 실전 전환 · /paper 모의 전환\n/update 업데이트 · /rollback 이전 버전 · /restart 재시작\n"
            "/rebase 자본 기준점(바닥선) 재설정", keyboard=KEYBOARD)

    def cmd_status(self, args):
        eq = self.state.get("equity") or 0.0
        avail = self.state.get("available") or 0.0
        st = "⏸ 일시정지" if self.state.get("paused") else ("⚠️ 차단: " + self.state["blocked"] if self.state.get("blocked") else "정상 운영")
        L = self.ledger or {}
        lines = [f"📊 상태 — {self.mode_ko()} | {st}", self.total_line(),
                 f"[선물] 잔고 {usd(eq, False)} (가용 {usd(avail, False)}) | 오늘 실현 {usd(float(L.get('day_realized', 0)))}",
                 f"보유 {len(self.positions)}/{self.cfg['max_open_positions']}"]
        lines.extend(self.positions_lines())
        lines.append(self.guard_line())
        tf = TF_SECONDS[self.cfg["signal_tf"]]
        nxt = (int(self.now() // tf) + 1) * tf
        lines.append(f"다음 판단: {kst(nxt).strftime('%H:%M')} KST ({self.cfg['signal_tf']} 마감)")
        if self.cfg.get("spot_enabled", True):
            if self.spot_ready:
                lines.append("[현물] 매일 09:05 KST 리밸런싱 — 자세히: /spot")
                lines.extend(self.spot.lines(detail=False))
            else:
                lines.append("[현물] ⚠️ 준비 안 됨 — /selftest 로 점검")
        self.send("\n".join(lines), keyboard=KEYBOARD)

    def journal(self):
        return [t for t in self.store.read_all("journal.jsonl") if t.get("mode") == self.state["mode"]]

    def cmd_pnl(self, args):
        js = self.journal()
        now = self.now()

        def block(rows, label):
            if not rows:
                return f"{label}: 거래 없음"
            pnl = sum(t["pnl"] for t in rows)
            wins = [t for t in rows if t["pnl"] > 0]
            return f"{label}: {len(rows)}건, 승률 {len(wins) / len(rows) * 100:.0f}%, {usd(pnl)}, 평균 {sum(t['r'] for t in rows) / len(rows):+.2f}R"

        L = self.ledger or {}
        day_start = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        lines = [f"💰 성과 — {self.mode_ko()}"]
        if self.cfg.get("spot_enabled", True) and self.spot_ready:
            T = self.state.get("total") or {}
            tot, sv, fv = self.total_equity()
            base = float(T.get("base") or 0)
            if base:
                since = kst(float(T.get("since") or now)).strftime("%Y-%m-%d")
                lines.append(f"[전체] {usd(tot, False)} — {since} 기준 {usd(base, False)} 대비 {(tot / base - 1) * 100:+.1f}% "
                             f"| 최대낙폭 {float(T.get('mdd') or 0):.1f}%")
            net_in = float(self.state["spot"].get("net_in") or 0)
            n_sp = len([t for t in self.store.read_all("spot_trades.jsonl") if t.get("mode") == self.state["mode"]])
            if net_in > 0:
                lines.append(f"[현물] 평가 {usd(sv, False)} / 투입 {usd(net_in, False)} → {usd(sv - net_in)} "
                             f"({(sv / net_in - 1) * 100:+.1f}%) | 리밸런싱 체결 {n_sp}건")
            lines.append("[선물] 돌파 전략 성과:")
        lines += [block([t for t in js if t["ts"] >= day_start], "오늘(09시~)"),
                 block([t for t in js if t["ts"] >= now - 7 * 86400], "최근 7일"),
                 block(js, "전체")]
        if js:
            gw = sum(t["pnl"] for t in js if t["pnl"] > 0)
            gl = -sum(t["pnl"] for t in js if t["pnl"] < 0)
            best = max(js, key=lambda t: t["pnl"])
            worst = min(js, key=lambda t: t["pnl"])
            lines.append(f"수익팩터 {gw / gl:.2f}" if gl > 0 else "수익팩터 -")
            lines.append(f"최고 {usd(best['pnl'])} ({short(best['sym'])}) / 최저 {usd(worst['pnl'])} ({short(worst['sym'])})")
        start = float(L.get("start_equity") or 0)
        eq = self.state.get("equity") or 0
        if start:
            lines.append(f"선물 잔고 {usd(eq, False)} (기준 {usd(start, False)} 대비 {(eq / start - 1) * 100:+.1f}%) | 최대낙폭 {self.state.get('max_dd', 0):.1f}%")
        self.send("\n".join(lines))

    # --- spot sleeve / allocation
    def cmd_spot(self, args):
        if not self.cfg.get("spot_enabled", True):
            self.send("현물 전략이 꺼져 있습니다. 켜기: /set spot_enabled true")
            return
        if not self.spot_ready:
            self.send("⚠️ 현물 모듈이 준비되지 않았습니다. /selftest 로 API 권한(현물 거래·범용 이체)을 확인하세요.")
            return
        self.spot.value(refresh=True)
        st = self.state["spot"]
        uni = ", ".join(short(x) for x in st.get("universe") or []) or "(다음 리밸런싱 때 선정)"
        last = st.get("last_run")
        lines = ["🪙 현물 — 추세 모멘텀 + 변동성 맞춤 비중 (레버리지 없음)",
                 f"운용 종목(거래대금 상위 {self.cfg.get('spot_universe_size', 6)}): {uni}",
                 f"마지막 리밸런싱: {hhmm(last) if last else '아직 없음'} | 다음: 매일 09:05 KST"]
        lines += self.spot.lines(detail=True)
        lines.append("신호 n/4 = 20·60·120·250일 전보다 오른 기간 수. 4/4면 강한 상승, 0/4면 현금(USDT) 대피.")
        lines.append("목표 비중 = 신호 × 변동성 역비례(변동성 큰 코인은 적게). 합계가 100%를 넘지 않습니다.")
        self.send("\n".join(lines))

    def cmd_alloc(self, args):
        if not args:
            tot, sv, fv = self.total_equity()
            lines = ["⚖️ 배분 — " + self.total_line(),
                     f"조정 규칙: 목표에서 {float(self.cfg.get('alloc_band_pct', 10)):g}%p 이상 벗어나면 이체, "
                     "매월 1일에는 2%p 이상이면 목표로 맞춤 (09:05 KST)"]
            rows = [t for t in self.store.read_all("transfers.jsonl") if t.get("mode") == self.state["mode"]][-5:]
            for t in rows:
                lines.append(f"  {kst(t['ts']).strftime('%m/%d %H:%M')} {'현물→선물' if t['to'] == 'futures' else '선물→현물'} "
                             f"{usd(t['amount'], False)}")
            lines.append("변경: /alloc 60 (현물 60% : 선물 40%) · 위험 단계: /preset")
            self.send("\n".join(lines))
            return
        try:
            pct = float(args[0].rstrip("%"))
            if not 0 <= pct <= 100:
                raise ValueError
        except ValueError:
            self.send("사용법: /alloc 50  (현물 비중 %, 0~100)")
            return
        self.confirm(f"목표 배분을 현물 {pct:g}% : 선물 {100 - pct:g}% 로 바꾸고 바로 맞출까요?", "alloc", pct)

    def act_alloc(self, pct):
        self._save_setting("alloc_spot_pct", float(pct))
        self.force_rebalance()
        self.send(f"✅ 목표 배분 현물 {pct:g}% : 선물 {100 - pct:g}% — 다음 점검(1분 안)에 이체·리밸런싱합니다.")

    def cmd_preset(self, args):
        cur = (float(self.cfg.get("alloc_spot_pct", 50)), float(self.cfg["risk_per_trade_pct"]),
               float(self.cfg.get("spot_vol_target", 0.6)))
        if not args or args[0] not in PRESETS:
            lines = ["🎚 위험 단계 (백테스트 2020~2026, 수수료·실제 펀딩비 포함)"]
            for k, (sp, rk, vt) in PRESETS.items():
                mark = " ← 현재" if cur == (sp, rk, vt) else ""
                lines.append(f"{k}: 현물 {sp}% / 선물 {100 - sp}% · 선물 1회 위험 {rk:g}% — {PRESET_NOTE[k]}{mark}")
            lines.append("변경: /preset 기본   (과거 수치는 미래를 보장하지 않습니다)")
            self.send("\n".join(lines))
            return
        name = args[0]
        sp, rk, vt = PRESETS[name]
        self.confirm(f"'{name}' 단계로 바꿀까요?\n현물 {sp}% : 선물 {100 - sp}% · 선물 1회 위험 {rk:g}% · 현물 변동성 목표 {vt:g}\n"
                     f"{PRESET_NOTE[name]}", "preset", name)

    def act_preset(self, name):
        sp, rk, vt = PRESETS[name]
        self._save_setting("alloc_spot_pct", float(sp))
        self._save_setting("risk_per_trade_pct", float(rk))
        self._save_setting("spot_vol_target", float(vt))
        self.force_rebalance()
        self.send(f"✅ '{name}' 단계 적용 — 현물 {sp}% : 선물 {100 - sp}%, 선물 1회 위험 {rk:g}%. 곧 배분을 맞춥니다.")

    def _save_setting(self, key, val):
        settings = self.store.load_settings()
        settings[key] = val
        self.store.save_settings(settings)
        self.cfg[key] = val
        self.event("SET", key=key, msg=str(val))

    def force_rebalance(self):
        st = self.state.setdefault("spot", {})
        st["last_day"] = None
        st["retry_at"] = 0.0
        self.state["force_alloc"] = True
        self.save()

    def cmd_export(self, args):
        year = args[0] if args and args[0].isdigit() else None
        rows = [["구분", "시각(KST)", "모드", "종목", "방향", "수량", "평균가", "금액(USDT)", "손익(USDT)", "수수료", "비고"]]

        def keep(ts):
            return year is None or kst(ts).strftime("%Y") == year

        for t in self.store.read_all("journal.jsonl"):
            if keep(t.get("ts", 0)):
                rows.append(["선물청산", kst(t["ts"]).strftime("%Y-%m-%d %H:%M"), t.get("mode"), t.get("sym"), t.get("side"),
                             t.get("qty"), t.get("exit"), round(float(t.get("qty") or 0) * float(t.get("exit") or 0), 4),
                             t.get("pnl"), t.get("fee"), f"진입 {t.get('entry')} · 펀딩 {t.get('funding')} · {t.get('reason')}"])
        for t in self.store.read_all("spot_trades.jsonl"):
            if keep(t.get("ts", 0)):
                fee = ";".join(f"{a}:{v:.8f}" for a, v in (t.get("fee") or {}).items())
                rows.append(["현물체결", kst(t["ts"]).strftime("%Y-%m-%d %H:%M"), t.get("mode"), t.get("sym"), t.get("side"),
                             t.get("qty"), t.get("avg"), t.get("quote"), "", fee, f"목표비중 {t.get('target_w')}"])
        for t in self.store.read_all("transfers.jsonl"):
            if keep(t.get("ts", 0)):
                rows.append(["지갑이체", kst(t["ts"]).strftime("%Y-%m-%d %H:%M"), t.get("mode"), "USDT",
                             "현물→선물" if t.get("to") == "futures" else "선물→현물", "", "", t.get("amount"), "", "", t.get("why")])
        if len(rows) == 1:
            self.send("내보낼 기록이 없습니다.")
            return
        import csv
        import io
        buf = io.StringIO()
        csv.writer(buf).writerows(rows)
        data = ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM so Excel/Numbers open Korean correctly
        name = f"trendhunter_{year or 'all'}.csv"
        ok = self.tg.send_document(name, data, caption=f"거래기록 {len(rows) - 1}건 (선물 청산·현물 체결·지갑 이체)") \
            if self.tg and hasattr(self.tg, "send_document") else None
        if not ok:
            self.send("⚠️ 파일 전송에 실패했습니다. 잠시 뒤 다시 시도하세요.")

    def cmd_report(self, args):
        now = self.now()
        if args and args[0] in ("이번달", "this", "now"):
            start, end, label = RP.month_start(now), now, RP.month_label(now) + " (지금까지)"
        else:
            start, end, label = RP.prev_month(now)
        self.send(self.monthly_text(start, end, label))

    def cmd_scan(self, args):
        lines = ["🔎 스캔 (마지막 마감봉 기준)"]
        for sym in self.symbols():
            d = self.data.get(sym)
            if not d:
                lines.append(f"{short(sym)}: 데이터 없음")
                continue
            sig = d["sig"]
            reg = S.REGIME_KO.get(sig["regime"], sig["regime"])
            dist = ""
            if sig["regime"] == S.LONG and "dist_long_pct" in sig:
                dist = f" | 돌파선까지 {sig['dist_long_pct']:+.2f}%"
            elif sig["regime"] == S.SHORT and "dist_short_pct" in sig:
                dist = f" | 이탈선까지 {-sig['dist_short_pct']:+.2f}%"
            adxs = f" | ADX {sig['adx']:.0f}" if sig.get("adx") is not None else ""
            if sym in self.positions:
                state = f"보유 중({SIDE_KO[self.positions[sym]['side']]})"
            elif sig.get("side"):
                state = "✅ " + S.REASON_KO.get(sig["code"], sig["code"])
            else:
                state = "대기: " + S.REASON_KO.get(sig["code"], sig["code"])
            lines.append(f"{short(sym)} — {reg}{dist}{adxs}\n   {state}")
        self.send("\n".join(lines))

    def cmd_log(self, args):
        n = 15
        if args and args[0].isdigit():
            n = min(int(args[0]), 40)
        rows = self.store.tail("events.jsonl", n)
        if not rows:
            self.send("기록 없음")
            return
        out = ["📜 최근 판단 기록"]
        for e in rows:
            t = kst(e.get("ts", 0)).strftime("%m/%d %H:%M")
            k = e.get("kind")
            sym = short(e.get("sym", "")) if e.get("sym") else ""
            if k == "ENTRY":
                out.append(f"{t} 진입 {sym} {SIDE_KO.get(e.get('side'), '')} @{px(e.get('entry'))} 손절 {px(e.get('stop'))}")
            elif k == "EXIT":
                out.append(f"{t} 청산 {sym} {usd(e.get('pnl', 0))} ({e.get('r', 0):+.1f}R) {EXIT_KO.get(e.get('reason'), e.get('reason'))}")
            elif k == "SKIP":
                out.append(f"{t} 패스 {sym} {SIDE_KO.get(e.get('side'), '')}: {R.GUARD_KO.get(e.get('code'), e.get('code'))}")
            elif k == "TRAIL":
                out.append(f"{t} 손절이동 {sym} {px(e.get('old'))}→{px(e.get('new'))}")
            elif k == "GUARD":
                out.append(f"{t} 가드 {R.GUARD_KO.get(e.get('code'), e.get('code'))}")
            elif k == "ERROR":
                out.append(f"{t} 오류 {e.get('where')} {sym}: {str(e.get('msg'))[:80]}")
            else:
                out.append(f"{t} {k} {sym} {e.get('msg', '')}")
        self.send("\n".join(out))

    def cmd_pause(self, args):
        self.state["paused"] = True
        self.save()
        self.send("⏸ 신규 진입을 멈췄습니다. 보유 포지션 관리(손절·추적)는 계속합니다. 재개: /resume")

    def cmd_resume(self, args):
        self.state["paused"] = False
        self.save()
        self.send("▶️ 신규 진입을 재개합니다.")

    def _find_sym(self, word):
        w = word.upper()
        for s in list(self.positions) + list(self.cfg["symbols"]):
            if s == w or short(s) == w:
                return s
        return w if w.endswith("USDT") else w + "USDT"

    def cmd_close(self, args):
        if not args:
            self.send("사용법: /close BTC")
            return
        sym = self._find_sym(args[0])
        if sym not in self.positions:
            self.send(f"{short(sym)} 보유 포지션이 없습니다.")
            return
        self.confirm(f"{short(sym)} 포지션을 시장가로 청산할까요?", "close", sym)

    def act_close(self, sym):
        if sym in self.positions:
            self.exit_position(sym, "MANUAL")

    def cmd_closeall(self, args):
        if not self.positions:
            self.send("보유 포지션이 없습니다.")
            return
        self.confirm(f"보유 포지션 {len(self.positions)}개를 모두 시장가로 청산할까요?", "closeall")

    def act_closeall(self):
        for sym in list(self.positions):
            self.exit_position(sym, "MANUAL")

    def cmd_risk(self, args):
        if not args:
            self.send(f"현재 1회 위험: {self.cfg['risk_per_trade_pct']}% — 변경: /risk 2.5")
            return
        self.cmd_set(["risk_per_trade_pct", args[0]])

    def cmd_settings(self, args):
        keys = ["spot_enabled", "alloc_spot_pct", "alloc_band_pct", "spot_candidates", "spot_universe_size",
                "spot_vol_target", "spot_band_pct",
                "symbols", "signal_tf", "trend_tf", "allow_long", "allow_short", "risk_per_trade_pct", "max_leverage", "max_open_positions", "max_same_direction",
                "max_entries_per_day", "daily_loss_limit_pct", "loss_streak_limit", "loss_streak_pause_hours",
                "cooldown_after_loss_hours", "equity_floor_pct", "time_stop_hours", "adx_min",
                "brief_morning_kst", "brief_evening_kst", "monthly_report", "update_url"]
        lines = ["⚙️ 설정 (변경: /set 키 값)"]
        for k in keys:
            v = self.cfg.get(k)
            if k in ("symbols", "spot_candidates"):
                v = ",".join(short(s) for s in (v or []))
            lines.append(f"{k} = {v if v not in ('', None) else '(없음)'}")
        self.send("\n".join(lines))

    def cmd_set(self, args):
        if len(args) < 2 or args[0] not in SETTABLE:
            self.send("사용법: /set 키 값\n바꿀 수 있는 키: " + ", ".join(SETTABLE))
            return
        key, raw = args[0], " ".join(args[1:])
        kind, lo, hi = SETTABLE[key]
        try:
            if kind in (float, int):
                val = kind(float(raw)) if kind is int else float(raw)
                if not lo <= val <= hi:
                    raise ValueError(f"{lo}~{hi} 사이여야 합니다")
            elif kind is bool:
                low = raw.strip().lower()
                if low in ("true", "on", "1", "yes", "켜기", "켬"):
                    val = True
                elif low in ("false", "off", "0", "no", "끄기", "끔"):
                    val = False
                else:
                    raise ValueError("true 또는 false")
            elif kind is list:
                val = []
                for w in raw.replace(" ", "").split(","):
                    if not w:
                        continue
                    s = w.upper() if w.upper().endswith("USDT") else w.upper() + "USDT"
                    if self.filters and (s not in self.filters or self.filters[s]["status"] != "TRADING"):
                        raise ValueError(f"{s} 는 바이낸스 USDⓈ-M 무기한에 없습니다")
                    val.append(s)
                if not val:
                    raise ValueError("종목이 비었습니다")
            elif kind == "spotlist":
                val = []
                for w in raw.replace(" ", "").split(","):
                    if not w:
                        continue
                    s_ = w.upper() if w.upper().endswith("USDT") else w.upper() + "USDT"
                    if self.spot_ready:
                        try:
                            self.spot.public.exchange_info([s_])
                        except (BinanceError, NetworkError):
                            raise ValueError(f"{s_} 는 바이낸스 현물에 없습니다")
                    val.append(s_)
                if not val:
                    raise ValueError("종목이 비었습니다")
            elif kind == "hhmm":
                hh, mm = [int(x) for x in raw.split(":")]
                if not (0 <= hh < 24 and 0 <= mm < 60):
                    raise ValueError("HH:MM 형식")
                val = f"{hh:02d}:{mm:02d}"
            else:
                val = raw.strip()
        except ValueError as e:
            self.send(f"값 오류: {e}")
            return
        settings = self.store.load_settings()
        settings[key] = val
        self.store.save_settings(settings)
        self.cfg[key] = val
        if key == "symbols":
            self.refresh_data()
        if key in ("spot_candidates", "spot_universe_size") and self.spot_ready:
            self.state["spot"]["universe_month"] = None
            if key == "spot_candidates":
                self.spot.load_filters()
        if key == "spot_enabled":
            if val:
                self.spot_next_try = 0.0  # built fresh for the current mode on the next step
            else:
                self.spot, self.spot_ready = None, False  # holdings stay as they are; no more spot trades
        if key in ("alloc_spot_pct",):
            self.state["force_alloc"] = True
        self.event("SET", key=key, msg=str(val))
        self.send(f"✅ {key} = {val} 로 바꿨습니다 (즉시 적용).")

    def cmd_rebase(self, args):
        eq = self.state.get("equity") or 0.0
        self.ledger["start_equity"] = eq
        self.ledger["peak_equity"] = eq
        self.state["floor_alerted"] = False
        self.state["max_dd"] = 0.0
        extra = ""
        if self.spot_ready:
            try:
                sv = self.spot.value(refresh=True)["equity"]
                self.state["spot"]["net_in"] = sv
                tot = eq + sv
                self.state["total"] = {"base": tot, "since": self.now(), "peak": tot, "mdd": 0.0, "last": tot}
                extra = f"\n총자산 기준점 {usd(tot, False)} (현물 {usd(sv, False)} + 선물 {usd(eq, False)}) — 입금·출금 뒤에 쓰세요."
            except (BinanceError, NetworkError) as e:
                extra = f"\n⚠️ 현물 평가 실패로 총자산 기준점은 그대로입니다: {e}"
        self.save()
        self.send(f"✅ 선물 자본 기준점을 {usd(eq, False)}로 재설정했습니다. 바닥선 = "
                  f"{usd(eq * self.cfg['equity_floor_pct'] / 100, False)}{extra}")

    # --- mode switching
    def cmd_live(self, args):
        if self.state["mode"] == "live":
            self.send("이미 실전 모드입니다.")
            return
        import selftest
        res = selftest.run_checks(self.api, self.cfg, self.cfg["symbols"], fix=False)
        if self.cfg.get("spot_enabled", True):
            res += selftest.run_spot_checks(self.spot_api_or_new(), self.cfg)
            self._merge_balance_checks(res)
        report = selftest.format_report(res)
        fails = [r for r in res if r["ok"] is False and r.get("blocking", True) and not r.get("fixable")]
        if fails:
            extra = ("\n(현물 전략 없이 선물만 쓰려면: /set spot_enabled false)" if any(r.get("spot") for r in fails) else "")
            self.send(report + "\n\n❌ 위 문제를 해결해야 실전으로 전환할 수 있습니다." + extra)
            return
        fut_eq = next((r.get("equity") for r in res if r.get("equity")), 0) or 0
        spot_eq = next((r.get("spot_equity") for r in res if r.get("spot_equity") is not None), 0) or 0
        tot = fut_eq + spot_eq
        if self.cfg.get("spot_enabled", True):
            sp = float(self.cfg.get("alloc_spot_pct", 50))
            plan = (f"총 {usd(tot, False)} → 현물 {sp:g}% ({usd(tot * sp / 100, False)}) : 선물 {100 - sp:g}% "
                    f"({usd(tot * (100 - sp) / 100, False)}) 로 자동 배분 후 바로 운용 시작.")
            risk_amt = tot * (100 - sp) / 100 * self.cfg["risk_per_trade_pct"] / 100
        else:
            plan = f"선물 {usd(fut_eq, False)} 로 운용."
            risk_amt = fut_eq * self.cfg["risk_per_trade_pct"] / 100
        coins = next((r for r in res if r.get("spot_coins") is not None), None)
        if self.cfg.get("spot_enabled", True) and coins and coins["spot_coins"] >= 1.0:
            plan += (f"\n⚠️ 현물 지갑에 이미 있는 코인({coins['spot_listing']})도 봇이 운용합니다. 목표 비중에 맞춰 일부를 팔 수 있어요. "
                     "따로 보관할 코인이면 먼저 다른 곳으로 옮기세요.")
        self.confirm(report + f"\n\n🔴 실전 모드로 전환할까요? 실제 돈으로 매매합니다.\n{plan}\n"
                     f"선물 1회 위험 {self.cfg['risk_per_trade_pct']}% ≈ {usd(risk_amt, False)}. 모의 포지션은 정리됩니다.", "go_live")

    @staticmethod
    def _merge_balance_checks(res):
        """With the spot sleeve the money may sit in either wallet: judge the TOTAL, not the futures wallet alone."""
        fut = next((r for r in res if "equity" in r), None)
        spot_eq = next((r.get("spot_equity") for r in res if r.get("spot_equity") is not None), 0.0) or 0.0
        move_ok = next((r["ok"] for r in res if r["name"].startswith("현물↔선물")), False)
        if fut is not None and fut["ok"] is False and (fut.get("equity") or 0) + spot_eq >= 10 and move_ok:
            fut["ok"] = None
            fut["detail"] += " — 현물 지갑 자금을 봇이 자동으로 배분합니다"

    def spot_api_or_new(self):
        if self.spot_api is None:
            self.spot_api = BinanceSpot(self.api.key, self.api.secret.decode(),
                                        base_url=os.environ.get("TH_SPOT_BASE", "https://api.binance.com"),
                                        clock=self.clock, sleep=self.sleep, log=self.log)
        return self.spot_api

    def reset_spot_mode(self, mode):
        """Start the spot sleeve over for a new mode: fresh state, fresh paper wallet, fresh back-end."""
        st = self.state.setdefault("spot", {})
        st.update(last_day=None, net_in=None, targets={}, diag={}, fails=0, retry_at=0.0, last_trades=[])
        self.state["total"] = {}
        self.state["hist"] = []
        self.state["hist_day"] = None
        self.state["force_alloc"] = True
        self.state["pending_transfer"] = None
        try:
            from spot import PaperSpot
            PaperSpot(None, {}, self.cfg, self.store.path("paper_spot.json"), self.log, self.clock).reset(0.0)
        except OSError as e:
            self.log.warning("paper spot reset: %s", e)
        self.spot_startup()  # builds nothing when spot is disabled; otherwise a clean sleeve for `mode`
        if self.cfg.get("spot_enabled", True) and not self.spot_ready:
            self.spot_next_try = self.now() + 60
            self.send("⚠️ 현물 모듈을 새 모드로 준비하지 못했습니다 — 1분 뒤 다시 시도합니다. 그동안 현물 매매·이체는 하지 않습니다.")

    def act_go_live(self):
        try:
            live = LiveBroker(self.api, self.filters, self.cfg, self.log)
            self.broker = live
            self.check_account_settings(fix=True)
            eq = live.equity()
        except (BinanceError, NetworkError) as e:
            self.broker = self.make_broker()
            self.send(f"❌ 실전 전환 실패: {e}")
            return
        if self.state.get("blocked"):
            self.broker = self.make_broker()
            self.send(f"❌ 실전 전환 중단: {self.state['blocked']}")
            return
        try:
            PaperBroker(self.api, self.filters, self.cfg, self.store.path("paper.json"), self.log, self.clock).reset()
        except Exception:
            pass
        self.state.update(mode="live", positions={}, ledger=R.new_ledger(eq["equity"], self.now()),
                          max_dd=0.0, floor_alerted=False)
        for s in self.symbols():
            live.prepare(s)
        self.state["equity"] = eq["equity"]
        self.reconcile()
        self.reset_spot_mode("live")
        self.save()
        self.event("MODE", msg="live")
        spot_note = ("\n현물/선물 배분과 현물 리밸런싱을 1분 안에 실행하고, 이후 매일 09:05 KST에 점검합니다."
                     if self.cfg.get("spot_enabled", True) else "")
        self.send(f"🔴 실전 모드 시작 — 선물 잔고 {usd(eq['equity'], False)}\n"
                  f"선물 1회 위험 {self.cfg['risk_per_trade_pct']}% (배분 후 선물 잔고 기준) | "
                  f"선물 자본 바닥선 {self.cfg['equity_floor_pct']:g}%\n"
                  f"선물 신호는 다음 {self.cfg['signal_tf']} 봉 마감부터 봅니다.{spot_note}", keyboard=KEYBOARD)

    def cmd_paper(self, args):
        if self.state["mode"] == "paper":
            self.send("이미 모의 모드입니다.")
            return
        if self.positions:
            self.send("실전 포지션이 남아 있습니다. 먼저 /closeall 로 정리한 뒤 전환하세요.")
            return
        self.confirm("모의 모드로 전환할까요? (가상 잔고로 새로 시작)", "go_paper")

    def act_go_paper(self):
        if self.positions:
            self.send("실전 포지션이 남아 있어 전환을 취소했습니다.")
            return
        self.state.update(mode="paper", positions={})
        self.broker = self.make_broker()
        self.broker.reset()
        eq = self.broker.equity()
        self.state.update(ledger=R.new_ledger(eq["equity"], self.now()), max_dd=0.0, floor_alerted=False,
                          equity=eq["equity"])
        self.reset_spot_mode("paper")
        self.save()
        self.event("MODE", msg="paper")
        note = (f"\n곧 현물 {float(self.cfg.get('alloc_spot_pct', 50)):g}% : 선물 나머지로 가상 배분합니다. "
                "실전 현물 보유분은 건드리지 않습니다." if self.cfg.get("spot_enabled", True) else "")
        self.send(f"🧪 모의 모드 시작 — 가상 잔고 {usd(eq['equity'], False)}{note}")

    # --- background jobs
    def _bg(self, name, fn):
        if self.bg_busy:
            self.send(f"이미 '{self.bg_busy}' 작업이 진행 중입니다. 끝나면 다시 시도하세요.")
            return

        def run():
            try:
                fn()
            except Exception as e:
                self.log.error("%s failed: %s\n%s", name, e, traceback.format_exc())
                self.send(f"⚠️ {name} 실패: {e}")
            finally:
                self.bg_busy = None

        self.bg_busy = name
        threading.Thread(target=run, daemon=True, name=name).start()

    def cmd_backtest(self, args):
        days = 730 if self.cfg.get("spot_enabled", True) else 365
        if args and args[0].isdigit():
            days = max(30, min(int(args[0]), 1460))
        self.send(f"⏳ 최근 {days}일 백테스트를 시작합니다(데이터 내려받기 포함 1~3분).")
        cfg = dict(self.cfg)
        only_fut = bool(args and args[-1].lower() in ("futures", "선물"))

        def job():
            if cfg.get("spot_enabled", True) and not only_fut:
                import backtest_mix
                text, _ = backtest_mix.report(cfg, days, fut_api=self.public_api_factory(),
                                              spot_api=self.spot_public_factory(),
                                              cache_dir=self.store.path("cache"), log=self.log)
            else:
                import backtest
                text, _ = backtest.report(cfg, days, risks=None, cache_dir=self.store.path("cache"),
                                          api=self.public_api_factory(), log=self.log)
            self.send(text)

        self._bg("백테스트", job)

    def cmd_selftest(self, args):
        import selftest
        if args and args[0].lower() == "live":
            self.confirm("실주문 왕복 테스트를 할까요?\n[선물] 최소 수량(약 6달러어치)을 시장가로 사고, 손절 주문을 걸었다 취소한 뒤 바로 팝니다.\n"
                         "[현물] 필요하면 선물→현물 7 USDT 이체, 6 USDT어치 사고 바로 판 뒤 되돌려 놓습니다.\n"
                         "비용은 수수료·호가차이로 보통 0.02달러 안팎입니다.", "selftest_live")
            return
        res = selftest.run_checks(self.api, self.cfg, self.cfg["symbols"], fix=False)
        if self.cfg.get("spot_enabled", True):
            res += selftest.run_spot_checks(self.spot_api_or_new(), self.cfg)
            self._merge_balance_checks(res)
        self.send(selftest.format_report(res))

    def act_selftest_live(self):
        import selftest

        def job():
            out = selftest.live_roundtrip(self.api, self.cfg, self.filters, self.log,
                                          exclude=set(self.positions))
            self.send(out["text"])
            if self.cfg.get("spot_enabled", True):
                if self.spot_ready and self.spot.due(self.now()):
                    self.send("ℹ️ 곧 현물 리밸런싱이 있어 현물 실주문 테스트는 건너뜁니다. 10분 뒤 다시 해 주세요.")
                    return
                self.spot_busy = True
                try:
                    out2 = selftest.spot_roundtrip(self.spot_api_or_new(), self.api, self.cfg, self.log)
                finally:
                    self.spot_busy = False
                    if self.spot is not None:
                        self.spot.cache = None
                self.send(out2["text"])

        self._bg("실주문 테스트", job)

    def cmd_update(self, args):
        url = self.cfg.get("update_url")
        if not url:
            self.send("업데이트 주소가 없습니다. /set update_url https://github.com/아이디/저장소/raw/main/trendhunter.zip")
            return
        self.confirm(f"아래 주소의 최신 코드로 업데이트할까요?\n{url}\n(검증에 실패하면 기존 버전을 유지합니다)", "update")

    def act_update(self):
        import updater

        def job():
            old, new = updater.update(self.cfg["update_url"], APP_DIR, self.store.dir)
            self.send(f"✅ 업데이트 v{old} → v{new} 설치 완료. 재시작합니다(포지션·손절은 유지).")
            self.exit_code = 0

        self._bg("업데이트", job)

    def cmd_rollback(self, args):
        self.confirm("직전 버전으로 되돌릴까요?", "rollback")

    def act_rollback(self):
        import updater
        ver = updater.rollback(APP_DIR, self.store.dir)
        self.send(f"↩️ v{ver} 로 복구했습니다. 재시작합니다.")
        self.exit_code = 0

    def cmd_restart(self, args):
        self.confirm("봇을 재시작할까요? (포지션·손절은 유지됩니다)", "restart")

    def act_restart(self):
        self.send("🔄 재시작합니다.")
        self.exit_code = 0

    def cmd_version(self, args):
        self.send(f"{self.cfg['bot_name']} v{self.cfg['version']}")


# ---------------------------------------------------------------- runner

def run(data_dir):
    store = Store(data_dir)
    log = setup_logging(store)
    cfg = load_config(APP_DIR, store)
    sec = store.load_secrets()
    if not sec.get("telegram_token"):
        log.error("secrets.json 이 없습니다. install.sh 를 먼저 실행하세요.")
        return 2
    base = os.environ.get("TH_BINANCE_BASE", "https://fapi.binance.com")
    api = BinanceFutures(sec.get("binance_key", ""), sec.get("binance_secret", ""), base_url=base, log=log)
    spot_base = os.environ.get("TH_SPOT_BASE", "https://api.binance.com")
    spot_api = BinanceSpot(sec.get("binance_key", ""), sec.get("binance_secret", ""), base_url=spot_base, log=log)
    tg = Telegram(sec["telegram_token"], sec.get("telegram_chat_id"), log=log)
    trader = Trader(cfg, store, api, tg, log, spot_api=spot_api,
                    spot_public_factory=lambda: BinanceSpot(base_url=spot_base, timeout=8))
    q = queue.Queue()
    poller = Poller(tg, q, log)
    poller.start()

    stopping = {"flag": False}

    def on_term(signum, frame):
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    # start-up with retries (e.g. network down, wrong key) while still answering commands
    while not stopping["flag"]:
        try:
            trader.startup()
            break
        except Exception as e:
            log.error("startup failed: %s\n%s", e, traceback.format_exc())
            hint = ""
            if isinstance(e, BinanceError) and (e.status == 451 or e.code == -2015):
                hint = ("\n힌트: -2015 = API 키/IP 제한/선물 권한 문제, 451 = 서버 국가 차단(미국 리전 등).")
            trader.alert("startup", f"⚠️ 봇 시작 실패: {e}{hint}\n1분 뒤 다시 시도합니다. /selftest 로 점검할 수 있어요.", every=900)
            end = time.time() + 60
            while time.time() < end and not stopping["flag"]:
                try:
                    u = q.get(timeout=1)
                    if trader.broker is None:
                        trader.broker = trader.make_broker()
                    trader.handle_update(u)
                except queue.Empty:
                    pass
                except Exception as ex:
                    log.warning("command during startup failed: %s", ex)

    next_step = 0.0
    loop = float(cfg.get("loop_seconds", 20))
    while not stopping["flag"] and trader.exit_code is None:
        now = time.time()
        if now >= next_step:
            try:
                trader.step()
                if trader.err_alerted:
                    trader.send("✅ 거래소 연결이 복구됐습니다.")
                trader.err_count = 0
                trader.err_alerted = False
            except Exception as e:
                trader.err_count += 1
                log.error("step failed (%d): %s\n%s", trader.err_count, e, traceback.format_exc())
                trader.event("ERROR", where="step", msg=str(e)[:300])
                if trader.err_count >= 3 and not trader.err_alerted:
                    trader.err_alerted = True
                    trader.send(f"⚠️ 연속 오류 {trader.err_count}회: {e}\n거래소 손절 주문은 유지됩니다. 계속 재시도합니다.")
            next_step = now + loop
        try:
            u = q.get(timeout=1.0)
            trader.handle_update(u)
        except queue.Empty:
            pass
        except Exception as e:
            log.error("update handling failed: %s", e)
    poller.stop()
    trader.save()
    if trader.exit_code is None:
        trader.send("⏹ 봇이 정지됐습니다. 거래소에 걸린 손절 주문은 그대로 유지됩니다.")
        return 0
    return trader.exit_code


def check(data_dir):
    """Used by the updater: imports everything and validates the config."""
    import backtest  # noqa: F401
    import backtest_mix  # noqa: F401
    import report  # noqa: F401
    import selftest  # noqa: F401
    import spot  # noqa: F401
    import spot_strategy  # noqa: F401
    import updater  # noqa: F401
    store = Store(data_dir)
    cfg = load_config(APP_DIR, store)
    for key in ("symbols", "signal_tf", "trend_tf", "risk_per_trade_pct", "ma_periods",
                "spot_candidates", "spot_lookbacks", "spot_vol_target", "alloc_spot_pct", "monthly_report"):
        if key not in cfg:
            raise SystemExit(f"config missing {key}")
    if not 0 <= float(cfg["alloc_spot_pct"]) <= 100:
        raise SystemExit("bad alloc_spot_pct")
    if not isinstance(cfg["monthly_report"], bool):
        raise SystemExit("bad monthly_report (true/false)")
    if cfg["signal_tf"] not in TF_SECONDS or cfg["trend_tf"] not in TF_SECONDS:
        raise SystemExit("bad timeframe")
    print(f"OK {cfg['bot_name']} {cfg['version']}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.environ.get("TH_DATA_DIR", os.path.expanduser("~/trendhunter-data")))
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        return check(a.data)
    return run(a.data)


if __name__ == "__main__":
    sys.exit(main())
