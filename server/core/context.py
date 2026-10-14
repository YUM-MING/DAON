"""서버 전역 런타임 컨텍스트: 설정 + 버스 + 공유 플래그."""
from __future__ import annotations

import os
import threading
import time

from server.config import ROOT, MODULES, WEB
from server.core.bus import Bus


class Context:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.bus = Bus()
        self.root, self.modules, self.web = ROOT, MODULES, WEB
        self.started = time.time()
        self.mic_muted = threading.Event()   # TTS 재생 중(에코 방지). 조정자가 켜고 끈다
        os.makedirs(config["logs"]["dir"], exist_ok=True)

    def log_path(self, key: str) -> str:
        return os.path.join(self.config["logs"]["dir"], self.config["logs"][key])

    def mode(self, module: str) -> str:
        return self.config["modules"][module]["mode"]
