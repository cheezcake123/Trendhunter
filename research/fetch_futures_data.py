#!/usr/bin/env python3
"""연구용 데이터 받기: 바이낸스 USDⓈ-M 선물 캔들(4시간봉·일봉)과 펀딩비 이력.

바이낸스 공개 데이터 저장소(data.binance.vision)의 월별·일별 파일을 받아 gzip JSON 으로 저장한다.
API 키가 필요 없고, 바이낸스 API 가 막힌 곳(미국 IP 의 GitHub Actions 등)에서도 받을 수 있다.
봇과 함께 배포되지 않는다(zip 에 들어가지 않음). 사용법은 research/lab.py 머리말.

    python3 research/fetch_futures_data.py research-data [BTCUSDT,ETHUSDT,...]

결과
    research-data/futures-klines/<SYMBOL>/<tf>.json.gz   바이낸스 klines 응답과 같은 행 목록
    research-data/funding/<SYMBOL>.json.gz              [[펀딩 시각(ms), 펀딩비], ...]
"""
import csv
import gzip
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, timedelta

BASE = "https://data.binance.vision/data/futures/um"
SYMBOLS = sys.argv[2].split(",") if len(sys.argv) > 2 else \
    ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT"]  # the bot's futures symbols
TFS = ["4h", "1d"]
FIRST = date(2019, 9, 1)


def get(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            err = e
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url}: {err}")


def csv_rows(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for name in z.namelist():
            with z.open(name) as fh:
                yield from csv.reader(io.TextIOWrapper(fh, encoding="utf-8"))


def months(until):
    d = FIRST
    while d < until:
        yield d
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def ms(x):
    x = int(float(x))
    return x // 1000 if x > 10 ** 14 else x  # some files are in microseconds


def klines(sym, tf, today):
    rows = {}
    this_month = today.replace(day=1)

    def add(blob):
        for r in csv_rows(blob):
            if not r or not r[0].strip().isdigit():
                continue  # header
            t = ms(r[0])
            rows[t] = [t, r[1], r[2], r[3], r[4], r[5], ms(r[6]), r[7], int(float(r[8])), r[9], r[10], "0"]

    for m in months(this_month):
        blob = get(f"{BASE}/monthly/klines/{sym}/{tf}/{sym}-{tf}-{m:%Y-%m}.zip")
        if blob:
            add(blob)
    d = this_month
    while d < today:
        blob = get(f"{BASE}/daily/klines/{sym}/{tf}/{sym}-{tf}-{d:%Y-%m-%d}.zip")
        if blob:
            add(blob)
        d += timedelta(days=1)
    return [rows[t] for t in sorted(rows)]


def funding(sym, today):
    out = {}
    for m in months(today.replace(day=1)):
        blob = get(f"{BASE}/monthly/fundingRate/{sym}/{sym}-fundingRate-{m:%Y-%m}.zip")
        if not blob:
            continue
        for r in csv_rows(blob):
            if not r or not r[0].strip().isdigit():
                continue
            out[ms(r[0])] = float(r[-1])
    # the current month has no file yet: try the API (blocked from some places; then it simply stays out)
    start = max(out) + 1 if out else 0
    try:
        while True:
            raw = get(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={sym}&startTime={start}&limit=1000", tries=1)
            batch = json.loads(raw or b"[]")
            if not isinstance(batch, list) or not batch:
                break
            for b in batch:
                out[int(b["fundingTime"])] = float(b["fundingRate"])
            start = int(batch[-1]["fundingTime"]) + 1
            if len(batch) < 1000:
                break
    except (RuntimeError, ValueError, KeyError) as e:
        print(f"  {sym}: funding API not reachable ({e}); data ends with the last monthly file", flush=True)
    return [[t, out[t]] for t in sorted(out)]


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(obj, fh, separators=(",", ":"))


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "research-data"
    today = date.today()
    for sym in SYMBOLS:
        for tf in TFS:
            rows = klines(sym, tf, today)
            save(os.path.join(root, "futures-klines", sym, f"{tf}.json.gz"), rows)
            print(f"{sym} {tf}: {len(rows)} candles", flush=True)
        f = funding(sym, today)
        save(os.path.join(root, "funding", f"{sym}.json.gz"), f)
        print(f"{sym} funding: {len(f)} rows", flush=True)


if __name__ == "__main__":
    main()
