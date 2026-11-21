"""차트 페이지(docs/chart.html) 점검: 가짜 바이낸스 응답(REST·웹소켓)으로 Chromium에서 열어 본다.

봇 테스트(tests/)와 달리 Playwright 가 필요해 서버에서는 돌리지 않는다. 페이지를 고치면 이것도 돌린다.

    python3 tools/check_chart_page.py docs [스크린샷 폴더]
"""
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from functools import partial
from playwright.sync_api import sync_playwright

DOCS = sys.argv[1] if len(sys.argv) > 1 else "docs"
OUT = sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp()
TFS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
NOW = int(time.time())

def rows(interval, limit, end_ms=None, spot=False):
    s = TFS[interval]
    last_open = (NOW // s) * s if end_ms is None else ((end_ms // 1000) // s) * s
    out = []
    for i in range(limit):
        t = last_open - (limit - 1 - i) * s
        p = 65000 + 1500 * math.sin(t / (s * 40.0)) + 300 * math.sin(t / (s * 7.0))
        o, c = p, p + 120 * math.sin(t / (s * 3.0))
        h, l = max(o, c) + 60, min(o, c) - 60
        f = (lambda x: f"{round(x, 1):.8f}") if spot else (lambda x: f"{x:.1f}")
        out.append([t * 1000, f(o), f(h), f(l), f(c), "12.345", t * 1000 + s * 1000 - 1, "1", 10, "1", "1", "0"])
    return out

def serve():
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=DOCS))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

srv = serve()
base = f"http://127.0.0.1:{srv.server_address[1]}/chart.html"
results = []

def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)

with sync_playwright() as pw:
    br = pw.chromium.launch()

    def page_for(url, ws_mode="ok", rest_mode="ok", scheme="dark", width=390, height=760):
        ctx = br.new_context(viewport={"width": width, "height": height}, color_scheme=scheme, device_scale_factor=2)
        pg = ctx.new_page()
        log = {"rest": [], "ws": [], "console": [], "errors": []}
        pg.on("console", lambda m: log["console"].append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        pg.on("pageerror", lambda e: log["errors"].append(str(e)))
        pg.route("https://telegram.org/**", lambda r: r.abort())

        def rest(route):
            u = urllib.parse.urlparse(route.request.url)
            q = dict(urllib.parse.parse_qsl(u.query))
            log["rest"].append((u.netloc, q))
            if rest_mode == "fail":
                return route.fulfill(status=500, body="no")
            if rest_mode == "first451" and u.netloc == "api.binance.com":
                return route.fulfill(status=451, body="{}")
            spot = "api/v3" in u.path
            data = rows(q["interval"], min(int(q.get("limit", 500)), 1000), int(q["endTime"]) if "endTime" in q else None, spot)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(data),
                          headers={"Access-Control-Allow-Origin": "*"})
        for host in ("https://fapi.binance.com/**", "https://api.binance.com/**", "https://data-api.binance.vision/**"):
            pg.route(host, rest)

        def on_ws(ws):
            log["ws"].append(ws.url)
            if ws_mode == "refuse":
                ws.close()
                return
            s = TFS[ws.url.rsplit("_", 1)[-1]]
            t = (NOW // s) * s
            for n in range(3):
                k = {"t": t * 1000, "o": "65000.0", "h": "66000.0", "l": "64000.0", "c": f"{65500 + n:.1f}", "v": "3.0"}
                ws.send(json.dumps({"e": "kline", "s": "X", "k": k}))
        if ws_mode != 'refuse':  # refused: the sandbox has no network, so the real connection fails
            pg.route_web_socket(re.compile(r"^wss://"), on_ws)
        pg.goto(url, wait_until='domcontentloaded')
        return ctx, pg, log

    # 1) futures long: lines, live updates
    url = base + "?" + urllib.parse.urlencode({"s": "BTCUSDT", "m": "f", "side": "L", "e": "64000", "sl": "62000",
                                              "isl": "61000", "liq": "52000", "t": NOW - 30 * 3600, "at": NOW})
    ctx, pg, log = page_for(url)
    pg.wait_for_function("window.__th && window.__th.rows() > 0", timeout=10000)
    pg.wait_for_timeout(800)
    check("futures: 1000 candles", pg.evaluate("__th.rows()") == 1000, str(pg.evaluate("__th.rows()")))
    check("futures: 4 lines (entry, stop, first stop, liq)", pg.evaluate("__th.lines()") == 4, str(pg.evaluate("__th.lines()")))
    check("futures REST host", all(h == "fapi.binance.com" for h, _ in log["rest"]), str(log["rest"][:2]))
    check("live price from WebSocket", abs(pg.evaluate("__th.price()") - 65502.0) < 1e-9 and pg.evaluate("__th.live()") == "실시간", f'{pg.evaluate("__th.price()")} {pg.evaluate("__th.live()")}')
    check("futures WS on /market/ws", log["ws"] and log["ws"][0].startswith("wss://fstream.binance.com/market/ws/btcusdt@kline_15m"), str(log["ws"]))
    legend = pg.inner_text("#legend")
    check("legend shows entry/stop/liq", all(w in legend for w in ("평균 진입", "손절", "청산가")), legend)
    check("header", "BTC 무기한" in pg.inner_text("header") and "롱 보유" in pg.inner_text("header"), pg.inner_text("header"))
    check("no order buttons", pg.locator("text=/매수|매도|주문하기|Buy|Sell/").count() == 0)
    pg.screenshot(path=os.path.join(OUT, "fut_dark.png"))
    check("fit button only for positions", pg.locator("nav button", has_text="선 맞춤").count() == 1)
    check("no page errors", not log["errors"], str(log["errors"]))
    # 2) timeframe switch
    pg.click("nav button[data-tf='1m']")
    pg.wait_for_timeout(800)
    check("tf switch asks 1m", any(q.get("interval") == "1m" for _, q in log["rest"]))
    check("tf switch WS 1m", any(u.endswith("@kline_1m") for u in log["ws"]), str(log["ws"][-1:]))
    check("tf remembered", pg.evaluate("localStorage.getItem('th_tf')") == "1m")
    # 3) scroll to the left edge loads older candles
    n_before = pg.evaluate("__th.rows()")
    box = pg.locator("#chart").bounding_box()
    for _ in range(40):
        pg.mouse.move(box["x"] + 60, box["y"] + box["height"] / 2)
        pg.mouse.down(); pg.mouse.move(box["x"] + box["width"] - 10, box["y"] + box["height"] / 2, steps=8); pg.mouse.up()
        pg.wait_for_timeout(40)
        if pg.evaluate("__th.rows()") > n_before:
            break
    pg.wait_for_timeout(600)
    check("older candles loaded on scroll", pg.evaluate("__th.rows()") > n_before, f"{n_before} -> {pg.evaluate('__th.rows()')}")
    check("older request has endTime", any("endTime" in q for _, q in log["rest"]))
    ctx.close()

    # 4) WS refused -> polling
    ctx, pg, log = page_for(url, ws_mode="refuse")
    pg.wait_for_function("window.__th && window.__th.rows() > 0", timeout=10000)
    pg.wait_for_timeout(9000)
    check("falls back to polling", "3초마다" in pg.evaluate("__th.live()") or "갱신" in pg.evaluate("__th.live()"), pg.evaluate("__th.live()"))
    check("polling asks limit=2", any(q.get("limit") == "2" for _, q in log["rest"]))
    ctx.close()

    # 5) spot, first host blocked (451) -> data-api mirror; light theme; no lines
    url_s = base + "?" + urllib.parse.urlencode({"s": "ETHUSDT", "m": "s", "at": NOW})
    ctx, pg, log = page_for(url_s, rest_mode="first451", scheme="light")
    pg.wait_for_function("window.__th && window.__th.rows() > 0", timeout=10000)
    pg.wait_for_timeout(600)
    check("spot: mirror used after 451", any(h == "data-api.binance.vision" for h, _ in log["rest"]), str(log["rest"][:3]))
    check("spot: no lines", pg.evaluate("__th.lines()") == 0)
    check("spot: no fit button, no line note", pg.locator("nav button", has_text="선 맞춤").count() == 0 and "선 위치" not in pg.inner_text("footer"))
    check("spot: price shown with 1 decimal (padding zeros dropped)", pg.inner_text("#price").count(".") == 1 and len(pg.inner_text("#price").split(".")[-1]) == 1, pg.inner_text("#price"))
    check("spot WS host", log["ws"] and log["ws"][0].startswith("wss://stream.binance.com:9443/ws/ethusdt@kline_"), str(log["ws"]))
    pg.screenshot(path=os.path.join(OUT, "spot_light.png"))
    check("no page errors (spot)", not log["errors"], str(log["errors"]))
    ctx.close()

    # 6) REST down -> message + retry
    ctx, pg, log = page_for(url, rest_mode="fail")
    pg.wait_for_timeout(1500)
    check("error message", pg.is_visible("#msg") and "불러오지 못했습니다" in pg.inner_text("#msg"))
    ctx.close()

    # 7) short position, stop past entry -> profit-protect wording
    url_p = base + "?" + urllib.parse.urlencode({"s": "SOLUSDT", "m": "f", "side": "S", "e": "70000", "sl": "66000", "t": NOW - 3600, "at": NOW})
    ctx, pg, log = page_for(url_p)
    pg.wait_for_function("window.__th && window.__th.rows() > 0", timeout=10000)
    pg.wait_for_timeout(500)
    check("short with locked profit says 이익 보호", "이익 보호" in pg.inner_text("#legend"), pg.inner_text("#legend"))
    check("short header", "숏 보유" in pg.inner_text("header"))
    check("entry change sign for short", "진입 대비 +" in pg.inner_text("#chg"), pg.inner_text("#chg"))
    pg.screenshot(path=os.path.join(OUT, "short_dark.png"))
    ctx.close()

    # 8) bad symbol param does not reach the DOM as HTML
    ctx, pg, log = page_for(base + "?s=%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E&m=f")
    pg.wait_for_timeout(800)
    check("bad symbol replaced", "BTC" in pg.inner_text("#title"), pg.inner_text("#title"))
    ctx.close()
    br.close()

fails = [r for r in results if not r[1]]
print(f"\n{len(results) - len(fails)}/{len(results)} passed · screenshots: {OUT}")
sys.exit(1 if fails else 0)
