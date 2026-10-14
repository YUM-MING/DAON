"""비전 어댑터 (이유민 모듈): workers/vision_worker.py 를 띄우고 이벤트를 받는다.

비전 → 버스: pose_pred, person_status, emotion, fall_detected, fall_recovered, fall_alert, device(camera)
버스 → 비전: speech(어르신 발화) / alert_cleared → notify_response (비전 내부 낙상 상태 리셋)
"""
from __future__ import annotations

from server.adapters.worker import WorkerAdapter
from server.config import MODULES, WORKERS


class VisionAdapter(WorkerAdapter):
    def __init__(self, ctx) -> None:
        m = ctx.config["modules"]["vision"]
        super().__init__(ctx, "vision", WORKERS / "vision_worker.py", MODULES / "vision",
                         ["--source", str(m["camera"]), "--fps", str(m["fps"])])
        ctx.bus.subscribe("speech", lambda ev: self.send({"cmd": "notify_response"}))
        ctx.bus.subscribe("alert_cleared", lambda ev: self.send({"cmd": "notify_response"}))
