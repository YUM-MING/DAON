"""DAON 통합 서버 진입점.

    python -m server.main                 # config.yaml 대로 (실제 카메라/마이크 워커)
    python -m server.main --mock          # 센서 없이 가짜 이벤트
    python -m server.main --port 8000 --config config.yaml

접속
    http://<PC IP>:8000/                     웹앱 (index → 사용자용/보호자용)
    ws://<PC IP>:8000/ws/robot               로봇 얼굴
    ws://<PC IP>:8000/ws/guardian            보호자 대시보드
    GET  /api/status                         현재 상태 스냅샷
    GET  /api/events?limit=100               최근 내부 이벤트 (디버그)
    GET  /api/eval_log?limit=400             평가 로그 (분석 탭)
    GET  /api/eval_summary                   카메라 단독 평가 수치 (비전 zip 동봉)
    POST /mock/...                           가짜 이벤트 (mock 모듈이 하나라도 있을 때)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from server import config as cfgmod
from server.core.broker import Broker
from server.core.context import Context
from server.core.coordinator import Coordinator

log = logging.getLogger("daon")


class App:
    """조립: 설정 → 컨텍스트 → 브로커/조정자 → 어댑터·워커·mock."""

    def __init__(self, config: dict) -> None:
        self.config = config
        self.ctx = Context(config)
        self.broker = Broker(self.ctx)
        self.coord = Coordinator(self.ctx, self.broker)
        self.dialogue = None
        self.workers: dict = {}
        self.mock = None
        self._tick_task: asyncio.Task | None = None

    async def start(self) -> None:
        self.ctx.bus.attach(asyncio.get_running_loop())
        m = self.config["modules"]
        if m["dialogue"]["mode"] == "real":
            from server.adapters.dialogue import DialogueAdapter
            self.dialogue = DialogueAdapter(self.ctx).start()
        if m["vision"]["mode"] == "worker":
            from server.adapters.vision import VisionAdapter
            self.workers["vision"] = VisionAdapter(self.ctx).start()
        if m["audio"]["mode"] == "worker":
            from server.adapters.audio import AudioAdapter
            self.workers["audio"] = AudioAdapter(self.ctx).start()
        if m["radar"]["mode"] == "worker":
            from server.adapters.radar import RadarAdapter
            self.workers["radar"] = RadarAdapter(self.ctx).start()
        if any(m[k]["mode"] == "mock" for k in ("vision", "audio", "radar")):
            from server.mock.generator import MockSensors
            self.mock = MockSensors(self.ctx).start()
        self._tick_task = asyncio.ensure_future(self._tick_loop())
        log.info("modules: %s", {k: v["mode"] for k, v in m.items()})

    async def _tick_loop(self) -> None:
        while True:
            try:
                self.coord.tick()
            except Exception:
                log.exception("tick error")
            await asyncio.sleep(0.5)

    async def stop(self) -> None:
        if self._tick_task:
            self._tick_task.cancel()
        if self.mock:
            self.mock.stop()
        for w in self.workers.values():
            w.stop()
        if self.dialogue:
            self.dialogue.stop()


def create_app(config: dict) -> FastAPI:
    core = App(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await core.start()
        try:
            yield
        finally:
            await core.stop()

    app = FastAPI(title="DAON", lifespan=lifespan)
    app.state.core = core

    # ---------------- WebSocket ----------------
    @app.websocket("/ws/{role}")
    async def ws_endpoint(ws: WebSocket, role: str):
        if role not in ("robot", "guardian"):
            await ws.close(code=4000)
            return
        await core.broker.connect(ws, role)
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict):
                    core.broker.on_message(role, msg)
        except WebSocketDisconnect:
            pass
        finally:
            core.broker.disconnect(ws, role)

    # ---------------- API ----------------
    @app.get("/api/status")
    async def status():
        return {"modules": {k: v["mode"] for k, v in config["modules"].items()},
                "workers": {k: {"alive": w.alive(), "restarts": w.restarts} for k, w in core.workers.items()},
                "clients": {"robot": len(core.broker.robots), "guardian": len(core.broker.guardians)},
                **core.coord.snapshot()}

    @app.get("/api/events")
    async def events(limit: int = 100):
        return [{"type": e.type, "ts": e.ts, **e.data} for e in core.ctx.bus.history[-limit:]]

    @app.get("/api/eval_log")
    async def eval_log(limit: int = 400):
        path = core.ctx.log_path("eval_log")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
        return out

    @app.get("/api/eval_summary")
    async def eval_summary():
        p = cfgmod.MODULES / "vision" / "data" / "urfall" / "eval_results.json"
        if not p.exists():
            return {}
        with open(p, encoding="utf-8") as f:
            r = json.load(f)
        return {"camera": {"precision": r.get("precision"), "recall": r.get("recall"), "f1": r.get("f1"),
                           "confusion": r.get("confusion"), "n_videos": r.get("n_videos")}}

    # ---------------- Mock ----------------
    def need_mock():
        if core.mock is None:
            raise HTTPException(400, "mock 모듈이 없습니다 (--mock 으로 실행하거나 config.yaml 에서 mode: mock)")
        return core.mock

    @app.post("/mock/fall")
    async def mock_fall(audio: bool = True):
        return need_mock().fall(with_audio=audio)

    @app.post("/mock/speech")
    async def mock_speech(text: str):
        return need_mock().speech(text)

    @app.post("/mock/unclear")
    async def mock_unclear():
        return need_mock().unclear()

    @app.post("/mock/emotion/{label}")
    async def mock_emotion(label: str):
        return need_mock().emotion(label)

    @app.post("/mock/present/{flag}")
    async def mock_present(flag: int):
        return need_mock().set_present(bool(flag))

    @app.post("/mock/radar/{motion}")
    async def mock_radar(motion: str):
        return need_mock().set_radar(motion)

    @app.post("/mock/scenario/{name}")
    async def mock_scenario(name: str):
        return await need_mock().scenario(name)

    @app.exception_handler(HTTPException)
    async def http_exc(_, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    # 정적 웹앱 (마지막에 마운트해야 /api, /ws 가 우선)
    app.mount("/", StaticFiles(directory=str(cfgmod.WEB), html=True), name="web")
    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="DAON 통합 서버")
    ap.add_argument("--config", default=None)
    ap.add_argument("--mock", action="store_true", help="비전/음성/레이더를 가짜 이벤트로 대체")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-dialogue", action="store_true")
    ap.add_argument("--scenario-id", default=None, help="평가 로그 scenario_id")
    args = ap.parse_args()

    over: dict = {"server": {}, "modules": {}, "logs": {}}
    if args.host:
        over["server"]["host"] = args.host
    if args.port:
        over["server"]["port"] = args.port
    if args.no_dialogue:
        over["modules"]["dialogue"] = {"mode": "off"}
    if args.scenario_id:
        over["logs"]["scenario_id"] = args.scenario_id
    config = cfgmod.load(args.config, mock=args.mock, overrides=over)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    import uvicorn
    uvicorn.run(create_app(config), host=config["server"]["host"], port=config["server"]["port"], log_level="info")


if __name__ == "__main__":
    main()
