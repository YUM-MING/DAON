"""집(home) 하나의 런타임 컨텍스트: 설정 + 이벤트 버스 + 공유 플래그 + 로그 경로."""
from __future__ import annotations

import os
import threading
import time

from server.config import ROOT, MODULES, WEB
from server.core.bus import Bus


class Context:
    def __init__(self, config: dict, home_id: int = 0, home_code: str = "local") -> None:
        self.config = config
        self.home_id, self.home_code = home_id, home_code
        self.bus = Bus()
        self.root, self.modules, self.web = ROOT, MODULES, WEB
        self.started = time.time()
        self.mic_muted = threading.Event()   # TTS 재생 중(에코 방지). 조정자가 켜고 끈다
        self.log_dir = os.path.join(config["logs"]["dir"], home_code)
        os.makedirs(self.log_dir, exist_ok=True)

    def log_path(self, key: str) -> str:
        return os.path.join(self.log_dir, self.config["logs"][key])

    def mode(self, module: str) -> str:
        return self.config["modules"][module]["mode"]
