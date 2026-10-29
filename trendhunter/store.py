"""Small persistence layer: JSON state (atomic writes) + JSONL logs. Standard library only."""
from __future__ import annotations

import json
import os
import time

CONFIG_FILE = "config.json"


class Store:
    def __init__(self, data_dir):
        self.dir = os.path.abspath(os.path.expanduser(data_dir))
        os.makedirs(self.dir, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass

    def path(self, name):
        return os.path.join(self.dir, name)

    # ------------------------------------------------------------ json files
    def read_json(self, name, default=None):
        p = self.path(name)
        if not os.path.exists(p):
            return default
        try:
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            bad = p + f".corrupt.{int(time.time())}"
            try:
                os.replace(p, bad)
            except OSError:
                pass
            return default

    def write_json(self, name, obj, mode=None):
        p = self.path(name)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, p)

    def load_state(self):
        return self.read_json("state.json", None)

    def save_state(self, state):
        self.write_json("state.json", state)

    def load_settings(self):
        return self.read_json("settings.json", {}) or {}

    def save_settings(self, settings):
        self.write_json("settings.json", settings)

    def load_secrets(self):
        return self.read_json("secrets.json", {}) or {}

    def save_secrets(self, secrets):
        self.write_json("secrets.json", secrets, mode=0o600)

    # ------------------------------------------------------------ jsonl logs
    def append(self, name, obj):
        p = self.path(name)
        if os.path.exists(p) and os.path.getsize(p) > 5_000_000:
            os.replace(p, p + ".1")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def tail(self, name, n=20):
        p = self.path(name)
        if not os.path.exists(p):
            return []
        with open(p, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = min(size, max(4096, n * 600))
            fh.seek(size - block)
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
        out = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    def read_all(self, name):
        p = self.path(name)
        out = []
        for fn in (p + ".1", p):
            if not os.path.exists(fn):
                continue
            with open(fn, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
        return out

    def event(self, kind, ts=None, **fields):
        rec = {"ts": round(ts if ts is not None else time.time(), 3), "kind": kind}
        rec.update(fields)
        self.append("events.jsonl", rec)
        return rec


def load_config(app_dir, store=None):
    with open(os.path.join(app_dir, CONFIG_FILE), encoding="utf-8") as fh:
        cfg = json.load(fh)
    if store is not None:
        overrides = store.load_settings()
        for k, v in overrides.items():
            if not k.startswith("_"):
                cfg[k] = v
    return cfg
