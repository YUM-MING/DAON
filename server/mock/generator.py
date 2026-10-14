"""가짜 센서 주입기: 센서 없이 UI와 대화 모듈을 끝까지 돌려 보기 위한 장치 (개발자 도구).

- 주기 루프(0.5초마다 pose_pred, 2초마다 person_status)는 vision mode 가 mock 일 때만 돈다.
  폰이 실제 프레임을 보내는 상황에서는 돌지 않는다 (실제 판정과 충돌 방지).
- 트리거(fall/speech/emotion/…)는 언제나 쓸 수 있다. 보호자 대시보드의 개발자 도구와 /api/homes/{id}/dev/* 가 호출한다.
"""
from __future__ import annotations

import asyncio
import time


class MockSensors:
    def __init__(self, ctx) -> None:
        self.ctx, self.bus = ctx, ctx.bus
        self.vision = ctx.mode("vision") == "mock"
        self.audio = ctx.mode("audio") in ("mock", "worker")
        self.radar = ctx.mode("radar") == "mock"
        self.present, self.posture = True, "sitting"
        self.fall_active = False
        self.radar_motion: str | None = None
        self._task: asyncio.Task | None = None
        self.bus.subscribe("speech", lambda ev: self.recover())
        self.bus.subscribe("alert_cleared", lambda ev: self.recover())

    # ---------------- 주기 루프 (mock 모드) ----------------
    async def run(self) -> None:
        for mod, key in (("vision", "camera"), ("audio", "mic"), ("radar", "radar")):
            if self.ctx.mode(mod) == "mock":
                self.bus.publish("device", **{key: "mock"})
        if not (self.vision or self.radar):
            return
        i = 0
        while True:
            now = time.time()
            if self.vision:
                pred = "fall" if self.fall_active else "no_fall"
                self.bus.publish("pose_pred", ts=now, pred=pred, conf=0.9,
                                 fall_state="fallen" if self.fall_active else "monitoring")
                if i % 4 == 0:
                    self.bus.publish("person_status", ts=now, present=self.present,
                                     posture=self.posture if self.present else "unknown")
            if self.radar and self.radar_motion:
                self.bus.publish("radar", ts=now, motion=self.radar_motion, distance_cm=150)
            i += 1
            await asyncio.sleep(0.5)

    def start(self) -> "MockSensors":
        try:
            self._task = asyncio.ensure_future(self.run())
        except RuntimeError:          # 루프 없음 (테스트)
            self._task = None
        return self

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    # ---------------- 트리거 ----------------
    def fall(self, with_audio: bool = True) -> dict:
        now = time.time()
        self.fall_active, self.posture = True, "lying"
        self.bus.publish("fall_detected", ts=now, confidence=0.9)
        self.bus.publish("pose_pred", ts=now, pred="fall", conf=0.9, fall_state="fallen")
        if with_audio:
            self.bus.publish("audio_fall", ts=now, confidence=0.88,
                             probs={"speech": 0.05, "fall": 0.88, "ambient": 0.07})
        if self.radar:
            self.radar_motion = "still"
        return {"ok": True, "fall_active": self.fall_active}

    def recover(self) -> None:
        if self.fall_active:
            self.fall_active, self.posture = False, "sitting"
            self.bus.publish("fall_recovered", ts=time.time())
        if self.radar and self.radar_motion == "still":
            self.radar_motion = "moving"

    def speech(self, text: str) -> dict:
        self.bus.publish("speech", ts=time.time(), text=text, duration=round(0.3 * len(text), 1), source="dev")
        return {"ok": True, "text": text}

    def unclear(self) -> dict:
        self.bus.publish("voice_unclear", ts=time.time(), duration=1.4)
        return {"ok": True}

    def emotion(self, label: str) -> dict:
        self.bus.publish("emotion", ts=time.time(), label=label, raw=label.capitalize(), confidence=0.8)
        return {"ok": True, "label": label}

    def set_present(self, present: bool) -> dict:
        self.present = present
        self.bus.publish("person_status", ts=time.time(), present=present,
                         posture=self.posture if present else "unknown")
        return {"ok": True, "present": present}

    def set_radar(self, motion: str) -> dict:
        self.radar_motion = motion
        self.bus.publish("radar", ts=time.time(), motion=motion, distance_cm=150)
        return {"ok": True, "motion": motion}

    async def scenario(self, name: str) -> dict:
        """시나리오 A(정서 케어) / B(낙상 무응답) / false_alarm(낙상 후 '괜찮아')."""
        if name == "A":
            self.emotion("sad")
            await asyncio.sleep(2)
            self.speech("요즘 너무 외롭고 힘들어")
        elif name == "B":
            self.fall()
        elif name == "false_alarm":
            self.fall()
            await asyncio.sleep(4)
            self.speech("응 괜찮아, 그냥 앉은 거야")
        else:
            return {"ok": False, "error": f"unknown scenario {name}"}
        return {"ok": True, "scenario": name}
