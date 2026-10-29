"""Runs install.sh end-to-end with stubbed sudo/systemctl, a fake Telegram API and the fake Binance API."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
from fake_binance import FakeExchange, SimClock, serve_http  # noqa: E402

TOKEN = "123456789:" + "A" * 35


def fake_telegram():
    sent = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            method = self.path.rsplit("/", 1)[-1]
            if f"/bot{TOKEN}/" not in self.path:
                res = {"ok": False, "description": "Unauthorized"}
            elif method == "getMe":
                res = {"ok": True, "result": {"username": "my_th_bot"}}
            elif method == "getUpdates":
                res = {"ok": True, "result": [{"update_id": 1, "message": {"date": 0, "text": "안녕",
                                                                          "chat": {"id": 4242, "type": "private"}}}]}
            elif method == "sendMessage":
                sent.append(body)
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


class InstallTest(unittest.TestCase):
    def test_install_script(self):
        tmp = tempfile.mkdtemp()
        tg_srv, tg_base, sent = fake_telegram()
        import time as _t
        fake = FakeExchange(SimClock(_t.time()), history_days=30, test_days=1)
        bn_srv, bn_base = serve_http(fake)
        try:
            app = os.path.join(tmp, "trendhunter")
            shutil.copytree(ROOT, app, ignore=shutil.ignore_patterns("__pycache__", "tests"))
            stubs = os.path.join(tmp, "stubs")
            os.makedirs(stubs)
            log = os.path.join(tmp, "calls.log")
            for name, body in {
                "sudo": '#!/bin/bash\n"$@"\n',
                "systemctl": f'#!/bin/bash\necho "systemctl $*" >> {log}\nexit 0\n',
                "timedatectl": '#!/bin/bash\necho yes\n',
            }.items():
                p = os.path.join(stubs, name)
                with open(p, "w") as fh:
                    fh.write(body)
                os.chmod(p, 0o755)
            env = dict(os.environ, PATH=stubs + ":" + os.environ["PATH"], HOME=tmp,
                       TH_TELEGRAM_BASE=tg_base, TH_BINANCE_BASE=bn_base,
                       TH_SYSTEMD_DIR=os.path.join(tmp, "unit"), TH_BIN_DIR=os.path.join(tmp, "bin"))
            os.makedirs(env["TH_SYSTEMD_DIR"])
            os.makedirs(env["TH_BIN_DIR"])
            answers = "\n".join(["short-bad-key", fake.key, fake.secret, "not-a-token", TOKEN, ""]) + "\n"
            url = "https://github.com/me/th/raw/main/trendhunter.zip"
            r = subprocess.run(["bash", os.path.join(app, "install.sh"), url], input=answers, text=True,
                               capture_output=True, env=env, timeout=180)
            out = r.stdout + r.stderr
            self.assertEqual(r.returncode, 0, out)
            self.assertIn("설치 완료", out)
            self.assertIn("64자리", out)              # bad key was rejected and re-asked
            data = os.path.join(tmp, "trendhunter-data")
            sec = json.load(open(os.path.join(data, "secrets.json")))
            self.assertEqual(sec["binance_key"], fake.key)
            self.assertEqual(sec["binance_secret"], fake.secret)
            self.assertEqual(str(sec["telegram_chat_id"]), "4242")
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(data, "secrets.json")).st_mode), 0o600)
            self.assertEqual(json.load(open(os.path.join(data, "settings.json")))["update_url"], url)
            unit = open(os.path.join(env["TH_SYSTEMD_DIR"], "trendhunter.service")).read()
            self.assertIn(f"ExecStart={shutil.which('python3')} -u {app}/bot.py --data {data}", unit)
            self.assertIn("Restart=always", unit)
            th = open(os.path.join(env["TH_BIN_DIR"], "th")).read()
            self.assertIn(f'APP="{app}"', th)
            self.assertIn(f'DATA="{data}"', th)
            calls = open(log).read()
            self.assertIn("systemctl enable trendhunter", calls)
            self.assertIn("systemctl restart trendhunter", calls)
            self.assertTrue(any("연결 테스트 성공" in m.get("text", "") for m in sent))
            self.assertIn("API 키·선물 권한: 읽기 성공", out)   # selftest ran against the fake exchange
            self.assertNotIn(fake.secret, out)                   # secret never echoed
        finally:
            tg_srv.shutdown()
            bn_srv.shutdown()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
