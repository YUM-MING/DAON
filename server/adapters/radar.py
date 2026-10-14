"""레이더 어댑터 (HLK-LD2410B): workers/radar_worker.py 를 띄운다. 담당자 미정 — 통합자가 임시 구현."""
from __future__ import annotations

from server.adapters.worker import WorkerAdapter
from server.config import ROOT, WORKERS


class RadarAdapter(WorkerAdapter):
    def __init__(self, ctx) -> None:
        m = ctx.config["modules"]["radar"]
        super().__init__(ctx, "radar", WORKERS / "radar_worker.py", ROOT,
                         ["--port", str(m["port"]), "--baud", str(m["baud"])])
