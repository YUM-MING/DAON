"""설정 로딩: config.yaml + 기본값 + CLI 오버라이드."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / "modules"
WEB = ROOT / "web"
WORKERS = ROOT / "server" / "workers"

DEFAULTS: dict[str, Any] = {
    "server": {"host": "0.0.0.0", "port": 8000, "secure_cookie": False},
    "modules": {
        "vision": {"mode": "worker", "source": "phone", "fps": 10},
        "audio": {"mode": "worker", "source": "phone", "input_device": None, "stt_model": "base", "stt": True},
        "radar": {"mode": "off", "port": "", "baud": 256000, "home_code": None},
        "dialogue": {"mode": "real"},
    },
    "robot": {"sleep_after_absent_sec": 30, "tts_timeout_sec": 12, "wake_to_listen_sec": 2},
    "logs": {"dir": "logs", "eval_log": "eval_log.jsonl", "alerts": "alerts.jsonl", "scenario_id": "live",
             "db": "daon.db"},
    "demo": {"email": "demo@daon.local", "password": "daon1234", "name": "개발자", "home_name": "데모 집",
             "home_code": "123456"},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(path: str | Path | None = None, *, mock: bool = False, overrides: dict | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    p = Path(path) if path else ROOT / "config.yaml"
    if p.exists():
        with open(p, encoding="utf-8") as f:
            cfg = _merge(cfg, yaml.safe_load(f) or {})
    if mock:
        for m in ("vision", "audio", "radar"):
            cfg["modules"][m]["mode"] = "mock"
    if overrides:
        cfg = _merge(cfg, overrides)
    cfg["logs"]["dir"] = str((ROOT / cfg["logs"]["dir"]).resolve())
    for m in cfg["modules"].values():                 # YAML 은 off/on/yes/no 를 불리언으로 읽는다 → 문자열로 정규화
        v = m.get("mode")
        if v is False or v is None:
            m["mode"] = "off"
        elif v is True:
            m["mode"] = "worker"
        else:
            m["mode"] = str(v)
    return cfg
