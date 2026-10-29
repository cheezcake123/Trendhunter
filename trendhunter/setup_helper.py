#!/usr/bin/env python3
"""Helpers called by install.sh (kept in Python so keys never pass through shell quoting).

Secrets are read from environment variables, never from the command line.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from store import Store  # noqa: E402
from telegram_client import Telegram, TelegramError  # noqa: E402

PATTERNS = {
    "key": r"^[A-Za-z0-9]{64}$",
    "secret": r"^[A-Za-z0-9]{64}$",
    "token": r"^\d{5,15}:[A-Za-z0-9_-]{30,}$",
}


def cmd_validate(kind, value):
    return 0 if re.match(PATTERNS[kind], value.strip()) else 1


def cmd_save_secrets(data_dir):
    store = Store(data_dir)
    sec = store.load_secrets()
    for env, key in (("TH_BK", "binance_key"), ("TH_BS", "binance_secret"),
                     ("TH_TT", "telegram_token"), ("TH_CID", "telegram_chat_id")):
        v = os.environ.get(env, "").strip()
        if v:
            sec[key] = v
    store.save_secrets(sec)
    print("저장 완료:", store.path("secrets.json"))
    return 0


def cmd_tg_detect(wait=60):
    tg = Telegram(os.environ["TH_TT"].strip())
    try:
        me = tg.call("getMe")
    except TelegramError as e:
        print(f"토큰 확인 실패: {e}", file=sys.stderr)
        return 2
    print(f"봇 확인: @{me.get('username')} — 텔레그램에서 이 봇에게 아무 메시지나 보내세요…", file=sys.stderr)
    end = time.time() + wait
    while time.time() < end:
        try:
            ups = tg.get_updates(timeout=10)
        except TelegramError as e:
            print(f"getUpdates 오류: {e}", file=sys.stderr)
            time.sleep(3)
            continue
        for u in reversed(ups):
            chat = (u.get("message") or {}).get("chat") or {}
            if chat.get("type") == "private" and chat.get("id"):
                print(chat["id"])
                return 0
    print("메시지를 받지 못했습니다.", file=sys.stderr)
    return 1


def cmd_tg_test():
    tg = Telegram(os.environ["TH_TT"].strip(), os.environ["TH_CID"].strip())
    r = tg.send("✅ TrendHunter 연결 테스트 성공! 설치가 끝나면 시작 메시지가 옵니다.")
    return 0 if r else 1


def cmd_set_setting(data_dir, key, value):
    store = Store(data_dir)
    s = store.load_settings()
    s[key] = value
    store.save_settings(s)
    return 0


def cmd_public_ip():
    try:
        with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=6) as r:
            print(r.read().decode().strip())
            return 0
    except Exception:
        return 1


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    v.add_argument("kind", choices=list(PATTERNS))
    s = sub.add_parser("save-secrets")
    s.add_argument("--data", required=True)
    sub.add_parser("tg-detect")
    sub.add_parser("tg-test")
    st = sub.add_parser("set-setting")
    st.add_argument("--data", required=True)
    st.add_argument("key")
    st.add_argument("value")
    sub.add_parser("public-ip")
    a = ap.parse_args()
    if a.cmd == "validate":
        return cmd_validate(a.kind, os.environ.get("TH_VALUE", ""))
    if a.cmd == "save-secrets":
        return cmd_save_secrets(a.data)
    if a.cmd == "tg-detect":
        return cmd_tg_detect()
    if a.cmd == "tg-test":
        return cmd_tg_test()
    if a.cmd == "set-setting":
        return cmd_set_setting(a.data, a.key, a.value)
    if a.cmd == "public-ip":
        return cmd_public_ip()
    return 1


if __name__ == "__main__":
    sys.exit(main())
