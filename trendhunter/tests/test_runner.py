"""Runs the real `bot.py` process (main loop, Telegram poller thread, SIGTERM handling) against fakes."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from fake_binance import FakeExchange, serve_http  # noqa: E402

TOKEN = "123456789:" + "B" * 35


def fake_telegram(commands):
    sent, served = [], {"n": 0}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            method = self.path.rsplit("/", 1)[-1]
            if method == "getUpdates":
                ups = []
                if body.get("timeout", 0) > 0 and served["n"] < len(commands):
                    i = served["n"]
                    served["n"] += 1
                    ups = [{"update_id": 100 + i, "message": {"date": int(time.time()), "text": commands[i],
                                                              "chat": {"id": 7, "type": "private"}}}]
                    time.sleep(0.5)
                elif body.get("timeout", 0) > 0:
                    time.sleep(1.0)
                res = {"ok": True, "result": ups}
            elif method == "sendMessage":
                sent.append(body.get("text", ""))
                res = {"ok": True, "result": {"message_id": len(sent)}}
            else:
                res = {"ok": True, "result": True}
            data = json.dumps(res).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", sent


class RunnerTest(unittest.TestCase):
    def test_process_lifecycle(self):
        tmp = tempfile.mkdtemp()
        fake = FakeExchange(time.time, history_days=260, test_days=1)
        bn, bn_base = serve_http(fake)
        tg, tg_base, sent = fake_telegram(["/status", "/scan", "/pnl"])
        try:
            data = os.path.join(tmp, "data")
            os.makedirs(data)
            with open(os.path.join(data, "secrets.json"), "w") as fh:
                json.dump({"binance_key": fake.key, "binance_secret": fake.secret,
                           "telegram_token": TOKEN, "telegram_chat_id": "7"}, fh)
            with open(os.path.join(data, "settings.json"), "w") as fh:
                json.dump({"symbols": list(fake.symbols), "loop_seconds": 2}, fh)
            env = dict(os.environ, TH_BINANCE_BASE=bn_base, TH_SPOT_BASE=bn_base, TH_TELEGRAM_BASE=tg_base)
            p = subprocess.Popen([sys.executable, os.path.join(ROOT, "bot.py"), "--data", data], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            deadline = time.time() + 40
            while time.time() < deadline and not any(t.startswith("💰 성과") for t in sent):
                time.sleep(0.5)
            p.send_signal(signal.SIGTERM)
            out, _ = p.communicate(timeout=20)
            self.assertEqual(p.returncode, 0, out[-2000:])
            self.assertTrue(any("TrendHunter v" in t and "시작" in t for t in sent), sent[:3])
            self.assertTrue(any(t.startswith("📊 상태") for t in sent))
            self.assertTrue(any(t.startswith("🔎 스캔") for t in sent))
            self.assertTrue(any(t.startswith("💰 성과") for t in sent))
            self.assertTrue(any("봇이 정지됐습니다" in t for t in sent))
            self.assertNotIn("Traceback", out)
            state = json.load(open(os.path.join(data, "state.json")))
            self.assertEqual(state["mode"], "paper")
            self.assertEqual(fake.trades, [])
        finally:
            bn.shutdown()
            tg.shutdown()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
