"""대화·융합·알림 어댑터 (채혜원 모듈): 서버 프로세스 안에서 DialogueEngine을 돌린다.

버스 → 대화 입력 (DialogueInput.from_dict 규격)
    pose_pred(fall)            → pose=fall, pose_conf
    pose_pred(no_fall)+posture → pose=lying|normal (person_status의 posture로 결정), uncertain은 보내지 않음
    emotion                    → emotion, emotion_conf  (surprised는 혜원 모듈 ALIASES가 neutral로 받음)
    audio_fall                 → sound_event=fall_sound, sound_conf
    speech                     → stt=text, sound_event=speech
    voice_unclear              → sound_event=voice_unclear
    radar                      → motion=moving|still|none (레이더 없으면 비전 motion 이벤트가 대신)
대화 출력 → 버스
    dialogue_output  (DialogueOutput.to_dict())
    alert            (AlertMessage.to_dict(), 혜원 알림 채널 Notifier 구현으로 받음)
버스 → 대화 제어
    alert_cleared    보호자가 대시보드에서 해제 → 안전 확인 세션 종료
                     (혜원 모듈에 공개 API가 없어 _end_safety()를 호출. 공개 API 추가를 요청해 둠)
"""
from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
from typing import Callable

from server.config import MODULES

log = logging.getLogger("daon.dialogue")
DIALOGUE_SRC = MODULES / "dialogue" / "src"


def _import_dialogue():
    if str(DIALOGUE_SRC) not in sys.path:
        sys.path.insert(0, str(DIALOGUE_SRC))
    from dialogue_algorithm import DialogueEngine  # noqa: E402
    from notifier import (AlertDispatcher, FileNotifier, Notifier, TelegramNotifier,  # noqa: E402
                          WebhookChannel, load_env)
    from pipeline import RealtimeRunner, SilverCarePipeline  # noqa: E402
    from tts_adapter import CallbackTTS  # noqa: E402
    return DialogueEngine, AlertDispatcher, FileNotifier, Notifier, TelegramNotifier, WebhookChannel, load_env, \
        RealtimeRunner, SilverCarePipeline, CallbackTTS


class DialogueAdapter:
    STRATEGY_SILENT = "NONE"

    def __init__(self, ctx, clock: Callable[[], float] = time.time, background: bool = True) -> None:
        """background=True: 알림 발송(웹훅 등)을 별도 스레드에서 — 외부 채널이 느려도 판단 루프가 안 멈춤.
        테스트는 False로 두고 동기 처리."""
        self.ctx, self.clock = ctx, clock
        (DialogueEngine, AlertDispatcher, FileNotifier, Notifier, TelegramNotifier, WebhookChannel, load_env,
         RealtimeRunner, SilverCarePipeline, CallbackTTS) = _import_dialogue()

        bus = ctx.bus

        class BusNotifier(Notifier):
            name = "bus"

            def send(self, msg) -> None:
                bus.publish("alert", ts=msg.timestamp or clock(), **msg.to_dict())

        load_env(str(MODULES / "dialogue" / ".env"))
        notifiers = [BusNotifier(), FileNotifier(ctx.log_path("alerts"))]
        token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if token and chat:
            notifiers.append(TelegramNotifier(token, chat))
        if os.environ.get("ALERT_WEBHOOK_URL"):
            notifiers.append(WebhookChannel(os.environ["ALERT_WEBHOOK_URL"]))

        self.engine = DialogueEngine()
        # TTS는 조정자가 폰으로 보내므로 여기서는 아무것도 재생하지 않는다 (CallbackTTS no-op)
        self.pipeline = SilverCarePipeline(self.engine, CallbackTTS(lambda text: None),
                                           AlertDispatcher(notifiers, retry_delay=0.0),
                                           on_error=lambda m: log.warning("[dialogue] %s", m),
                                           background=background)
        self.runner = RealtimeRunner(self.pipeline, clock=clock, tick_interval=1.0)
        self._cmds: queue.Queue = queue.Queue()
        self._pending: dict = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.posture: str | None = None
        self._radar_seen: float | None = None
        self.last_output: dict | None = None
        self._last_state = "NORMAL"

        for t, fn in {"pose_pred": self._pose, "person_status": self._status, "emotion": self._emotion,
                      "audio_fall": self._audio_fall, "speech": self._speech, "voice_unclear": self._unclear,
                      "radar": self._radar, "motion": self._motion, "alert_cleared": self._cleared}.items():
            bus.subscribe(t, fn)

    # ---------------- 입력 변환 ----------------
    # 혜원 모듈은 "약 1초에 한 건, 그 시점의 모든 신호를 담은 입력"을 기대한다. 센서 이벤트는 따로따로
    # 도착하므로(포즈 낙상 → 0.0초 뒤 낙상음) 짧은 창(COALESCE_SEC) 안의 이벤트를 하나로 합쳐 넣는다.
    # 따로 넣으면 두 번째 이벤트가 '확인 중 새 위험 근거'로 보여 "괜찮으세요?"를 두 번 묻는다.
    COALESCE_SEC = 0.25

    def _feed(self, ts: float, **d) -> None:
        with self._lock:
            p = self._pending
            p["timestamp"] = max(ts, p.get("timestamp", 0))
            for k, v in d.items():
                if k == "stt" and p.get("stt"):
                    p["stt"] = f"{p['stt']} {v}"                   # 같은 창의 발화는 이어 붙인다
                elif k == "sound_event" and p.get("sound_event") == "fall_sound":
                    continue                                       # 낙상음은 같은 창에서 덮어쓰지 않는다
                else:
                    p[k] = v

    def flush(self) -> None:
        with self._lock:
            batch, self._pending = self._pending, {}
        if batch:
            self.runner.feed(batch)

    def _pose(self, ev) -> None:
        if ev.pred == "fall":
            self._feed(ev.ts, pose="fall", pose_conf=ev.conf)
        elif ev.pred == "no_fall":
            if self.posture == "lying":
                self._feed(ev.ts, pose="lying", pose_conf=ev.conf)
            elif self.posture in ("standing", "sitting"):
                self._feed(ev.ts, pose="normal", pose_conf=ev.conf)
        # uncertain: 카메라만으로 애매 → 보내지 않음 (다른 센서가 결정)

    def _status(self, ev) -> None:
        self.posture = ev.posture if ev.present else None

    def _emotion(self, ev) -> None:
        self._feed(ev.ts, emotion=ev.label, emotion_conf=ev.get("confidence", 1.0))

    def _audio_fall(self, ev) -> None:
        self._feed(ev.ts, sound_event="fall_sound", sound_conf=ev.get("confidence", 1.0))

    def _speech(self, ev) -> None:
        self._feed(ev.ts, stt=ev.text, sound_event="speech")

    def _unclear(self, ev) -> None:
        self._feed(ev.ts, sound_event="voice_unclear")

    def _radar(self, ev) -> None:
        self._radar_seen = ev.ts
        self._feed(ev.ts, motion=ev.motion)

    def _motion(self, ev) -> None:
        """비전 기반 움직임. 레이더가 최근 5초 안에 값을 줬으면 레이더를 우선한다."""
        if self._radar_seen is not None and ev.ts - self._radar_seen < 5.0:
            return
        self._feed(ev.ts, motion=ev.motion)

    def _cleared(self, ev) -> None:
        self._cmds.put(("end_safety", ev.get("reason")))

    # ---------------- 실행 ----------------
    def start(self) -> "DialogueAdapter":
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="dialogue", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                time.sleep(self.COALESCE_SEC)      # 이벤트 합치기 창 = 처리 주기 (입력 없으면 tick)
                self.step(timeout=0)
            except Exception:
                log.exception("dialogue step error")
                time.sleep(0.5)

    def step(self, timeout: float | None = None):
        """모아 둔 입력을 한 건으로 처리하거나(없으면 tick) 결과를 버스에 발행한다. 테스트는 timeout=0으로 직접 호출."""
        self._drain_cmds()
        self.flush()
        out = self.runner.poll_once(timeout=timeout)
        d = out.to_dict()
        self.last_output = d
        meaningful = (d.get("response_text") or d.get("alert_status") or d.get("emergency")
                      or d.get("notice") or d.get("strategy") != self.STRATEGY_SILENT
                      or d.get("state") != self._last_state)
        self._last_state = d.get("state")
        if meaningful:
            self.ctx.bus.publish("dialogue_output", ts=self.clock(), **d)
        return out

    def _drain_cmds(self) -> None:
        while True:
            try:
                cmd, arg = self._cmds.get_nowait()
            except queue.Empty:
                return
            if cmd == "end_safety" and self.engine.session is not None:
                log.info("보호자 해제(%s) → 안전 확인 세션 종료", arg)
                self.engine._end_safety(self.clock())   # 공개 API 없음 — 혜원 파트에 추가 요청
                self._last_state = "NORMAL"
