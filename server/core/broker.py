"""WebSocket 브로커: 로봇 얼굴(/ws/robot)과 보호자 대시보드(/ws/guardian) 연결 관리.

바깥으로 나가는 메시지 규약은 TEAM_INTEGRATION_GUIDE.md 3절(유진 기획안 v2 6장).
안으로 들어오는 메시지(폰/보호자 → 서버)는 내부 버스 이벤트로 바꾼다.

서버 → 로봇
    {"type":"state", "state":..., "emotion":..., "text":..., "countdown":..., "speak":bool, "utterance_id":n}
서버 → 보호자
    alert_created / alert_resolved / device_status / notice / robot_state(얼굴 상태 미러)
로봇 → 서버
    {"type":"hello"} {"type":"tts_done","utterance_id":n,"ok":true} {"type":"stt","text":...}(폰 STT를 쓸 경우)
    {"type":"device","battery":82}
보호자 → 서버
    {"type":"alert_resolve","alert_id":"a-001","resolution":"guardian_confirmed|false_alarm|called_119"}
    {"type":"message_to_robot","text":"..."}
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("daon.broker")


class Broker:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.robots: set = set()
        self.guardians: set = set()
        # 늦게 접속한 클라이언트에게 보낼 스냅샷
        self.last_state: dict | None = None
        self.last_device: dict | None = None
        self.active_alert: dict | None = None
        self.robot_info: dict = {}

    # ---------------- 연결 ----------------
    @property
    def robot_online(self) -> bool:
        return bool(self.robots)

    async def connect(self, ws, role: str) -> None:
        await ws.accept()
        (self.robots if role == "robot" else self.guardians).add(ws)
        log.info("%s connected (%d robot / %d guardian)", role, len(self.robots), len(self.guardians))
        if role == "robot" and self.last_state:
            await self._safe_send(ws, self.last_state)
        if role == "guardian":
            if self.last_device:
                await self._safe_send(ws, self.last_device)
            if self.active_alert:
                await self._safe_send(ws, self.active_alert)
            if self.last_state:
                await self._safe_send(ws, {**self.last_state, "type": "robot_state"})
        self.ctx.bus.publish("client", role=role, connected=True)

    def disconnect(self, ws, role: str) -> None:
        (self.robots if role == "robot" else self.guardians).discard(ws)
        log.info("%s disconnected", role)
        self.ctx.bus.publish("client", role=role, connected=False)

    # ---------------- 수신 ----------------
    def on_message(self, role: str, msg: dict) -> None:
        bus, t = self.ctx.bus, msg.get("type")
        if role == "robot":
            if t == "tts_done":
                bus.publish("tts_done", utterance_id=msg.get("utterance_id"), ok=bool(msg.get("ok", True)))
            elif t == "stt" and msg.get("text"):
                bus.publish("speech", text=str(msg["text"]), duration=0.0, source="robot")
            elif t == "device":
                self.robot_info.update({k: v for k, v in msg.items() if k != "type"})
            elif t == "hello":
                self.robot_info.update(msg.get("info") or {})
        else:
            if t == "alert_resolve":
                bus.publish("guardian_resolve", alert_id=msg.get("alert_id"),
                            resolution=msg.get("resolution", "guardian_confirmed"),
                            handled_by=msg.get("handled_by", "guardian_1"))
            elif t == "message_to_robot" and msg.get("text"):
                bus.publish("guardian_message", text=str(msg["text"]))

    # ---------------- 송신 (동기 API, 루프 스레드에서 호출) ----------------
    def send_robot(self, msg: dict) -> None:
        if msg.get("type") == "state":
            self.last_state = {k: v for k, v in msg.items() if k not in ("speak", "utterance_id")}
        self._broadcast(self.robots, msg)

    def send_guardian(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "device_status":
            self.last_device = msg
        elif t == "alert_created":
            self.active_alert = msg
        elif t == "alert_resolved":
            self.active_alert = None
        self._broadcast(self.guardians, msg)

    def _broadcast(self, targets: set, msg: dict) -> None:
        for ws in list(targets):
            try:
                asyncio.ensure_future(self._safe_send(ws, msg))
            except RuntimeError:  # 루프 밖(테스트)
                pass

    @staticmethod
    async def _safe_send(ws, msg: dict) -> None:
        try:
            await ws.send_json(msg)
        except Exception:
            pass


class FakeBroker:
    """테스트용: 보낸 메시지를 리스트에 모은다."""

    def __init__(self) -> None:
        self.robot: list[dict] = []
        self.guardian: list[dict] = []
        self.robot_online = True
        self.robot_info: dict = {}

    def send_robot(self, msg: dict) -> None:
        self.robot.append(msg)

    def send_guardian(self, msg: dict) -> None:
        self.guardian.append(msg)
