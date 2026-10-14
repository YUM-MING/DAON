"""비전 어댑터 (이유민 모듈): workers/vision_worker.py 를 띄우고 이벤트를 받는다.

입력: 폰 브라우저가 보낸 JPEG 프레임 (source: phone) 또는 PC 웹캠 (source: 0)
비전 → 버스: pose_pred, person_status, emotion, fall_detected, fall_recovered, fall_alert, device(camera)
버스 → 비전: speech(어르신 발화) / alert_cleared → notify_response (비전 내부 낙상 상태 리셋)
"""
from __future__ import annotations

import base64

from server.adapters.worker import WorkerAdapter
from server.config import MODULES, WORKERS


class VisionAdapter(WorkerAdapter):
    def __init__(self, bus_for, config: dict) -> None:
        m = config["modules"]["vision"]
        args = ["--source", str(m.get("source", "phone")), "--fps", str(m["fps"]), "--log-dir", config["logs"]["dir"]]
        if m.get("debug", True):
            args.append("--debug")
        super().__init__(bus_for, "vision", WORKERS / "vision_worker.py", MODULES / "vision", args)

    def attach_home(self, rt) -> None:
        hid = rt.id
        rt.ctx.bus.subscribe("speech", lambda ev: self.send({"cmd": "notify_response", "home": hid}))
        rt.ctx.bus.subscribe("alert_cleared", lambda ev: self.send({"cmd": "notify_response", "home": hid}))

    def send_frame(self, home_id: int, jpeg: bytes, ts: float) -> None:
        self.send({"cmd": "frame", "home": home_id, "ts": ts, "jpg": base64.b64encode(jpeg).decode("ascii")})
