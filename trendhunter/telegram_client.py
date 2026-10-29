"""Tiny Telegram Bot API client (standard library only)."""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "https://api.telegram.org"


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token, chat_id=None, base=None, log=None):
        self.token = token
        self.chat_id = chat_id
        self.base = (base or os.environ.get("TH_TELEGRAM_BASE") or DEFAULT_BASE).rstrip("/")
        self.log = log

    def call(self, method, params=None, timeout=20):
        url = f"{self.base}/bot{self.token}/{method}"
        data = json.dumps(params or {}).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                j = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                j = json.loads(e.read())
            except Exception:
                raise TelegramError(f"HTTP {e.code}") from e
        except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError) as e:
            raise TelegramError(f"network: {e}") from e
        if not j.get("ok"):
            raise TelegramError(j.get("description", "unknown error"))
        return j.get("result")

    @staticmethod
    def _split(text, limit=3800):
        if len(text) <= limit:
            return [text]
        parts, cur = [], ""
        for line in text.split("\n"):
            if len(cur) + len(line) + 1 > limit:
                parts.append(cur)
                cur = line
            else:
                cur = f"{cur}\n{line}" if cur else line
        if cur:
            parts.append(cur)
        return parts

    def send(self, text, inline=None, keyboard=None, silent=False, chat_id=None):
        """Never raises: a failed notification must not stop trading."""
        cid = chat_id or self.chat_id
        if not cid:
            return None
        chunks = self._split(str(text))
        last = None
        for idx, chunk in enumerate(chunks):
            params = {"chat_id": cid, "text": chunk, "disable_web_page_preview": True,
                      "disable_notification": bool(silent)}
            if idx == len(chunks) - 1:
                if inline:
                    params["reply_markup"] = {"inline_keyboard": [
                        [{"text": t, "callback_data": d} for t, d in row] for row in inline]}
                elif keyboard:
                    params["reply_markup"] = {"keyboard": [[{"text": t} for t in row] for row in keyboard],
                                              "resize_keyboard": True, "is_persistent": True}
            for attempt in range(2):
                try:
                    last = self.call("sendMessage", params)
                    break
                except TelegramError as e:
                    if self.log:
                        self.log.warning("telegram send failed: %s", e)
                    time.sleep(1.5)
        return last

    def send_document(self, filename, data, caption=None, chat_id=None):
        """Upload a small file (multipart/form-data). Returns the message or None; never raises."""
        cid = chat_id or self.chat_id
        if not cid:
            return None
        boundary = "----th" + os.urandom(8).hex()
        parts = []
        for k, v in (("chat_id", str(cid)), ("caption", caption or "")):
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{filename}\"\r\n"
                      "Content-Type: text/csv\r\n\r\n").encode() + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = urllib.request.Request(f"{self.base}/bot{self.token}/sendDocument", data=body, method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                j = json.loads(resp.read())
            return j.get("result") if j.get("ok") else None
        except Exception as e:
            if self.log:
                self.log.warning("telegram sendDocument failed: %s", e)
            return None

    def answer_callback(self, cb_id, text=None):
        try:
            self.call("answerCallbackQuery", {"callback_query_id": cb_id, "text": text or ""})
        except TelegramError:
            pass

    def get_updates(self, offset=None, timeout=25):
        params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", params, timeout=timeout + 15) or []

    def set_commands(self, commands):
        try:
            self.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in commands]})
        except TelegramError as e:
            if self.log:
                self.log.warning("setMyCommands failed: %s", e)


class Poller(threading.Thread):
    """Long-polls Telegram in the background and hands updates to the main thread via a queue."""

    def __init__(self, tg, q, log, skip_older_than=90):
        super().__init__(daemon=True, name="telegram-poller")
        self.tg = tg
        self.q = q
        self.log = log
        self.stop_event = threading.Event()
        self.offset = None
        self.skip_older_than = skip_older_than

    def run(self):
        # drop the backlog that piled up while the bot was down (old commands are not replayed)
        try:
            pending = self.tg.get_updates(timeout=0)
            now = time.time()
            for u in pending:
                self.offset = u["update_id"] + 1
                msg = u.get("message") or {}
                if msg and now - msg.get("date", 0) <= self.skip_older_than:
                    self.q.put(u)
        except Exception as e:
            self.log.warning("telegram backlog: %s", e)
        backoff = 2
        while not self.stop_event.is_set():
            try:
                ups = self.tg.get_updates(self.offset, timeout=25)
                for u in ups:
                    self.offset = u["update_id"] + 1
                    self.q.put(u)
                backoff = 2
            except Exception as e:
                self.log.warning("telegram poll: %s", e)
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 60)

    def stop(self):
        self.stop_event.set()
