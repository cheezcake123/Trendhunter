#!/usr/bin/env python3
"""Self-update from a zip URL (e.g. https://github.com/<id>/<repo>/raw/main/trendhunter.zip).

The new code is downloaded, unpacked, compiled and checked with `bot.py --check`
BEFORE it replaces anything. The previous version is kept in <data>/backups for /rollback.
"""
from __future__ import annotations

import argparse
import json
import os
import py_compile
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

REQUIRED = ["bot.py", "strategy.py", "risk.py", "brokers.py", "binance_api.py", "telegram_client.py",
            "store.py", "indicators.py", "backtest.py", "selftest.py", "updater.py", "setup_helper.py",
            "config.json", "install.sh", "th", "spot.py", "spot_strategy.py", "binance_spot.py", "backtest_mix.py",
            "report.py"]
SKIP_NAMES = {"__pycache__", ".git", ".DS_Store"}


class UpdateError(Exception):
    pass


def _version(app_dir):
    try:
        with open(os.path.join(app_dir, "config.json"), encoding="utf-8") as fh:
            return json.load(fh).get("version", "?")
    except Exception:
        return "?"


def download(url, dest, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "TrendHunter-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as fh:
        shutil.copyfileobj(r, fh)
    if os.path.getsize(dest) < 1000:
        raise UpdateError("받은 파일이 너무 작습니다(주소가 zip 파일을 가리키는지 확인)")


def _safe_extract(zpath, out_dir):
    with zipfile.ZipFile(zpath) as z:
        for name in z.namelist():
            target = os.path.realpath(os.path.join(out_dir, name))
            if not target.startswith(os.path.realpath(out_dir) + os.sep) and target != os.path.realpath(out_dir):
                raise UpdateError(f"zip 안에 허용되지 않는 경로: {name}")
        z.extractall(out_dir)


def _find_root(out_dir):
    if os.path.exists(os.path.join(out_dir, "bot.py")):
        return out_dir
    for name in sorted(os.listdir(out_dir)):
        cand = os.path.join(out_dir, name)
        if os.path.isdir(cand) and os.path.exists(os.path.join(cand, "bot.py")):
            return cand
    raise UpdateError("zip 안에서 bot.py 를 찾지 못했습니다")


def validate(root, data_dir):
    missing = [f for f in REQUIRED if not os.path.exists(os.path.join(root, f))]
    if missing:
        raise UpdateError("필수 파일 누락: " + ", ".join(missing))
    for f in os.listdir(root):
        if f.endswith(".py"):
            try:
                py_compile.compile(os.path.join(root, f), doraise=True)
            except py_compile.PyCompileError as e:
                raise UpdateError(f"문법 오류 {f}: {e.msg}") from e
    r = subprocess.run([sys.executable, os.path.join(root, "bot.py"), "--check", "--data", data_dir],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise UpdateError("검사 실패: " + (r.stderr or r.stdout).strip()[-400:])


def _copy_tree(src, dst):
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        if name in SKIP_NAMES:
            continue
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if os.path.isdir(s):
            _copy_tree(s, d)
        else:
            shutil.copy2(s, d)


def _fix_modes(app_dir):
    for f in ("install.sh", "th", "bot.py", "backtest.py", "backtest_mix.py", "selftest.py", "updater.py", "setup_helper.py"):
        p = os.path.join(app_dir, f)
        if os.path.exists(p):
            os.chmod(p, 0o755)


def backup(app_dir, data_dir):
    bdir = os.path.join(data_dir, "backups")
    os.makedirs(bdir, exist_ok=True)
    dest = os.path.join(bdir, f"{time.strftime('%Y%m%d-%H%M%S')}_v{_version(app_dir)}")
    _copy_tree(app_dir, dest)
    olds = sorted(d for d in os.listdir(bdir) if os.path.isdir(os.path.join(bdir, d)))
    for d in olds[:-5]:
        shutil.rmtree(os.path.join(bdir, d), ignore_errors=True)
    return dest


def update(url, app_dir, data_dir):
    work = os.path.join(data_dir, "update_tmp")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    try:
        zpath = os.path.join(work, "new.zip")
        download(url, zpath)
        out = os.path.join(work, "x")
        os.makedirs(out)
        _safe_extract(zpath, out)
        root = _find_root(out)
        validate(root, data_dir)
        old, new = _version(app_dir), _version(root)
        backup(app_dir, data_dir)
        _copy_tree(root, app_dir)
        _fix_modes(app_dir)
        return old, new
    finally:
        shutil.rmtree(work, ignore_errors=True)


def rollback(app_dir, data_dir):
    bdir = os.path.join(data_dir, "backups")
    olds = sorted(d for d in os.listdir(bdir) if os.path.isdir(os.path.join(bdir, d))) if os.path.isdir(bdir) else []
    if not olds:
        raise UpdateError("백업이 없습니다")
    src = os.path.join(bdir, olds[-1])
    _copy_tree(src, app_dir)
    _fix_modes(app_dir)
    shutil.rmtree(src, ignore_errors=True)
    return _version(app_dir)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.environ.get("TH_DATA_DIR", os.path.expanduser("~/trendhunter-data")))
    ap.add_argument("--app", default=here)
    ap.add_argument("--url", default="")
    ap.add_argument("--rollback", action="store_true")
    a = ap.parse_args()
    try:
        if a.rollback:
            print(f"복구 완료: v{rollback(a.app, a.data)}")
            return 0
        url = a.url
        if not url:
            sys.path.insert(0, a.app)
            from store import Store, load_config
            url = load_config(a.app, Store(a.data)).get("update_url", "")
        if not url:
            print("업데이트 주소가 없습니다. --url 로 주거나 텔레그램 /set update_url 로 저장하세요.")
            return 1
        old, new = update(url, a.app, a.data)
        print(f"업데이트 완료: v{old} -> v{new}")
        return 0
    except (UpdateError, OSError, subprocess.SubprocessError) as e:
        print(f"업데이트 실패(기존 버전 유지): {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
