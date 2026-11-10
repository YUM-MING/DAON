"""레이더 어댑터 (HLK-LD2410B, PC USB-TTL): workers/radar_worker.py 를 띄운다.

레이더는 서버 PC에 꽂히므로 어느 집의 센서인지 config.yaml `radar.home_code` 로 지정한다 (없으면 첫 번째 집).
저장소 기본값은 off — 레이더가 꽂힌 PC 만 config.local.yaml 에서 `modules.radar.mode: worker` 로 켠다.
2026-11-10 실기기(COM3, 펌웨어 V2.44) 수신·서버 연동 확인.
"""
from __future__ import annotations

from server.adapters.worker import WorkerAdapter
from server.config import ROOT, WORKERS


class RadarAdapter(WorkerAdapter):
    def __init__(self, bus_for, config: dict) -> None:
        m = config["modules"]["radar"]
        super().__init__(bus_for, "radar", WORKERS / "radar_worker.py", ROOT,
                         ["--port", str(m["port"]), "--baud", str(m["baud"])])
