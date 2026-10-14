"""DAON 통합 서버 진입점.

    python -m server.main                 # config.yaml 대로 (폰 카메라·마이크 스트림, 워커)
    python -m server.main --mock          # 센서 워커 없이 가짜 이벤트
    python -m server.main --port 8000

구성
    계정(이메일+비밀번호) · 집(6자리 코드) · 로봇 토큰     → server/db.py, server/auth.py
    집 하나 = 버스 + 브로커 + 조정자 + 대화 엔진           → server/core/home.py
    비전/음성/레이더 워커는 서버 전체 공유, home 으로 라우팅   → server/adapters/*

HTTP
    POST /api/auth/signup|login|logout|demo   GET /api/me
    POST /api/homes {name}   POST /api/homes/join {code}   POST /api/pair {code,name}(로봇)
    GET  /api/homes/{id}/status|events|eval_log|alerts|report
    POST /api/homes/{id}/dev/{fall|speech|unclear|emotion|present|radar|scenario}   (개발자 도구)
WS
    /ws/robot?token=...      텍스트(JSON) + 바이너리(1바이트 종류: 1=JPEG 프레임, 2=PCM16 16kHz)
    /ws/guardian?home=ID     세션 쿠키
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from server import config as cfgmod
from server.auth import COOKIE, current_user, ensure_demo_account, require_member, require_user
from server.core.home import HomeManager
from server.db import DB

log = logging.getLogger("daon")


class App:
    """조립: 설정 → DB → 집 관리자 → 공유 워커."""

    def __init__(self, config: dict) -> None:
        self.config = config
        os.makedirs(config["logs"]["dir"], exist_ok=True)
        self.db = DB(os.path.join(config["logs"]["dir"], config["logs"]["db"]))
        self.demo = ensure_demo_account(self.db, config.get("demo"))
        self.homes = HomeManager(config, self.db, on_create=self._attach_workers)
        self.workers: dict = {}
        self._tick_task: asyncio.Task | None = None

    def _attach_workers(self, rt) -> None:
        for w in self.workers.values():
            w.attach_home(rt)

    async def start(self) -> None:
        m = self.config["modules"]
        if m["vision"]["mode"] == "worker":
            from server.adapters.vision import VisionAdapter
            self.workers["vision"] = VisionAdapter(self.homes.bus_for, self.config).start()
        if m["audio"]["mode"] == "worker":
            from server.adapters.audio import AudioAdapter
            self.workers["audio"] = AudioAdapter(self.homes.bus_for, self.config).start()
        if m["radar"]["mode"] == "worker":
            from server.adapters.radar import RadarAdapter
            code = m["radar"].get("home_code")
            radar_home = self.db.home_by_code(str(code)) if code else None
            rid = radar_home["id"] if radar_home else None
            self.workers["radar"] = RadarAdapter(lambda h, rid=rid: self.homes.bus_for(rid), self.config).start()
        if self.demo:                                   # 데모 집은 미리 띄워 둔다 (워커 기본 목적지)
            await self.homes.get(self.demo["home"]["id"])
        self._tick_task = asyncio.ensure_future(self._tick_loop())
        log.info("modules: %s  demo: %s / 집코드 %s", {k: v["mode"] for k, v in m.items()},
                 self.demo["user"]["email"] if self.demo else None, self.demo["home"]["code"] if self.demo else None)

    async def _tick_loop(self) -> None:
        while True:
            self.homes.tick_all()
            await asyncio.sleep(0.5)

    async def stop(self) -> None:
        if self._tick_task:
            self._tick_task.cancel()
        await self.homes.stop_all()
        for w in self.workers.values():
            w.stop()


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
    db, homes = core.db, core.homes

    def set_session(resp: Response, user: dict) -> None:
        resp.set_cookie(COOKIE, db.create_session(user["id"]), httponly=True, samesite="lax",
                        max_age=30 * 24 * 3600, secure=bool(config["server"].get("secure_cookie")))

    def me_payload(user: dict) -> dict:
        return {"user": user, "homes": db.homes_for_user(user["id"])}

    # ---------------- 인증 ----------------
    @app.post("/api/auth/signup")
    async def signup(resp: Response, body: dict = Body(...)):
        try:
            user = db.create_user(body.get("email", ""), body.get("password", ""), body.get("name", ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        db.create_home(body.get("home_name") or f"{user['name']}의 집", user["id"])
        set_session(resp, user)
        return me_payload(user)

    @app.post("/api/auth/login")
    async def login(resp: Response, body: dict = Body(...)):
        user = db.verify_login(body.get("email", ""), body.get("password", ""))
        if not user:
            raise HTTPException(401, "이메일 또는 비밀번호가 틀립니다")
        set_session(resp, user)
        return me_payload(user)

    @app.post("/api/auth/demo")
    async def login_demo(resp: Response):
        if not core.demo:
            raise HTTPException(404, "데모 계정이 설정되지 않았습니다")
        user = core.demo["user"]
        set_session(resp, user)
        return me_payload(user)

    @app.post("/api/auth/logout")
    async def logout(request: Request, resp: Response):
        db.delete_session(request.cookies.get(COOKIE))
        resp.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/api/me")
    async def me(request: Request):
        return me_payload(require_user(request))

    @app.get("/api/demo")
    async def demo_info():
        if not core.demo:
            return {}
        return {"email": core.demo["user"]["email"], "home_code": core.demo["home"]["code"],
                "home_name": core.demo["home"]["name"]}

    # ---------------- 집 ----------------
    @app.post("/api/homes")
    async def create_home(request: Request, body: dict = Body(default={})):
        u = require_user(request)
        return db.create_home(body.get("name", ""), u["id"])

    @app.post("/api/homes/join")
    async def join_home(request: Request, body: dict = Body(...)):
        u = require_user(request)
        try:
            return db.join_home(u["id"], str(body.get("code", "")))
        except ValueError as e:
            raise HTTPException(404, str(e))

    @app.post("/api/pair")
    async def pair(body: dict = Body(...)):
        """로봇 폰: 집 코드 → 로봇 토큰."""
        try:
            return db.pair_robot(str(body.get("code", "")), body.get("name", "robot"))
        except ValueError as e:
            raise HTTPException(404, str(e))

    @app.get("/api/robot/me")
    async def robot_me(token: str = Query(...)):
        r = db.robot_by_token(token)
        if not r:
            raise HTTPException(401, "유효하지 않은 로봇 토큰")
        return {"robot": {"id": r["id"], "name": r["name"]}, "home": db.get_home(r["home_id"]),
                "fps": config["modules"]["vision"]["fps"]}

    @app.get("/api/homes/{home_id}/status")
    async def home_status(request: Request, home_id: int):
        require_member(request, home_id)
        rt = await homes.get(home_id)
        return {"home": rt.home, "modules": {k: v["mode"] for k, v in config["modules"].items()},
                "workers": {k: {"alive": w.alive(), "restarts": w.restarts} for k, w in core.workers.items()},
                "clients": {"robot": len(rt.broker.robots), "guardian": len(rt.broker.guardians)},
                **rt.coord.snapshot()}

    @app.get("/api/homes/{home_id}/events")
    async def home_events(request: Request, home_id: int, limit: int = 100):
        require_member(request, home_id)
        rt = await homes.get(home_id)
        return [{"type": e.type, "ts": e.ts, **e.data} for e in rt.ctx.bus.history[-limit:]]

    @app.get("/api/homes/{home_id}/eval_log")
    async def home_eval_log(request: Request, home_id: int, limit: int = 400):
        require_member(request, home_id)
        rt = await homes.get(home_id)
        path = rt.ctx.log_path("eval_log")
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

    @app.get("/api/homes/{home_id}/alerts")
    async def home_alerts(request: Request, home_id: int, limit: int = 100):
        require_member(request, home_id)
        return db.alerts(home_id, limit)

    @app.get("/api/homes/{home_id}/report")
    async def home_report(request: Request, home_id: int, days: int = 7):
        require_member(request, home_id)
        return db.report(home_id, days)

    @app.post("/api/homes/{home_id}/dev/{action}")
    async def home_dev(request: Request, home_id: int, action: str, body: dict = Body(default={})):
        """개발자 도구: 가짜 센서 이벤트 주입 (집의 보호자면 누구나)."""
        require_member(request, home_id)
        rt = await homes.get(home_id)
        mk = rt.mock
        if action == "fall":
            return mk.fall(with_audio=bool(body.get("audio", True)))
        if action == "speech":
            return mk.speech(str(body.get("text", "")))
        if action == "unclear":
            return mk.unclear()
        if action == "emotion":
            return mk.emotion(str(body.get("label", "neutral")))
        if action == "present":
            return mk.set_present(bool(body.get("present", True)))
        if action == "radar":
            return mk.set_radar(str(body.get("motion", "still")))
        if action == "scenario":
            return await mk.scenario(str(body.get("name", "B")))
        raise HTTPException(404, f"unknown action {action}")

    @app.get("/api/eval_summary")
    async def eval_summary():
        p = cfgmod.MODULES / "vision" / "data" / "urfall" / "eval_results.json"
        if not p.exists():
            return {}
        with open(p, encoding="utf-8") as f:
            r = json.load(f)
        return {"camera": {"precision": r.get("precision"), "recall": r.get("recall"), "f1": r.get("f1"),
                           "confusion": r.get("confusion"), "n_videos": r.get("n_videos")}}

    # ---------------- WebSocket ----------------
    @app.websocket("/ws/robot")
    async def ws_robot(ws: WebSocket, token: str = Query(default="")):
        robot = db.robot_by_token(token)
        if not robot:
            await ws.accept()
            await ws.close(code=4401)
            return
        rt = await homes.get(robot["home_id"])
        vision, audio = core.workers.get("vision"), core.workers.get("audio")
        await rt.broker.connect(ws, "robot")
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                data = msg.get("bytes")
                if data:
                    kind, payload, now = data[0], data[1:], time.time()
                    if kind == 1:
                        rt.coord.note_stream("camera")
                        if vision:
                            vision.send_frame(rt.id, payload, now)
                    elif kind == 2:
                        rt.coord.note_stream("mic")
                        if audio:
                            audio.send_pcm(rt.id, payload, now)
                    continue
                text = msg.get("text")
                if text:
                    try:
                        m = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(m, dict):
                        rt.broker.on_message("robot", m)
        except WebSocketDisconnect:
            pass
        finally:
            rt.broker.disconnect(ws, "robot")

    @app.websocket("/ws/guardian")
    async def ws_guardian(ws: WebSocket, home: int = Query(...)):
        user = db.session_user(ws.cookies.get(COOKIE))
        if not user or not db.is_member(user["id"], home):
            await ws.accept()
            await ws.close(code=4401)
            return
        rt = await homes.get(home)
        await rt.broker.connect(ws, "guardian")
        try:
            while True:
                text = await ws.receive_text()
                try:
                    m = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(m, dict):
                    m.setdefault("handled_by", user.get("name") or "guardian")
                    rt.broker.on_message("guardian", m)
        except WebSocketDisconnect:
            pass
        finally:
            rt.broker.disconnect(ws, "guardian")

    @app.exception_handler(HTTPException)
    async def http_exc(_, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    # 정적 웹앱 (마지막에 마운트해야 /api, /ws 가 우선)
    app.mount("/", StaticFiles(directory=str(cfgmod.WEB), html=True), name="web")
    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="DAON 통합 서버")
    ap.add_argument("--config", default=None)
    ap.add_argument("--mock", action="store_true", help="비전/음성/레이더 워커 대신 가짜 이벤트")
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
