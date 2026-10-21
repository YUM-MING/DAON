"""조정자(coordinator): 내부 이벤트 → 로봇 얼굴 상태 / 보호자 알림 / 평가 로그.

낙상 판정과 대화 전략은 대화 모듈(혜원)이 단일 기준이다. 여기서는 그 출력을
UI 규약(state 7종, alert_created/alert_resolved)으로 바꾸고, 센서 스냅샷과 TTS/에코 게이팅을 관리한다.

로봇 얼굴 상태 매핑 (대화 전략 → UI state)
    ASK_CHECK / REASK / RETRY_LISTEN / CLARIFY → alert (countdown = response_deadline_sec)
    ESCALATE → alert (countdown 0, 보호자 알림 발생)
    REASSURE → alert (카운트다운 없음, 비상 유지 중)
    RESUME → recover → (TTS 끝) wake
    그 외 발화 → speaking → (TTS 끝) listening
    어르신 발화 수신 → thinking (대화 응답이 없으면 3초 뒤 listening)
    사람 없음 30초 → sleep, 사람 다시 보임 → wake → listening
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

log = logging.getLogger("daon.coord")

SAFETY_ASK = {"ASK_CHECK", "REASK", "RETRY_LISTEN", "CLARIFY"}
GUARDIAN_RESOLVE_TEXT = {
    "guardian_confirmed": "보호자분이 확인하셨어요. 곧 도움이 갈 거예요.",
    "called_119": "보호자분이 119에 연락하셨어요. 조금만 기다려 주세요.",
    "false_alarm": "다행이에요. 다시 평소처럼 지낼게요.",
}


class Coordinator:
    def __init__(self, ctx, broker, clock: Callable[[], float] = time.time, store=None) -> None:
        self.ctx, self.broker, self.clock, self.store = ctx, broker, clock, store
        self.cfg = ctx.config
        # 폰 스트림 수신 시각 (카메라/마이크가 폰에서 올 때 기기 상태 판단용)
        self._last_frame_at: float | None = None
        self._last_pcm_at: float | None = None
        bus = ctx.bus
        # 로봇 얼굴 상태
        self.ui_state, self.emotion, self.text, self.countdown = "sleep", "neutral", "", None
        self.emotion_raw, self.emotion_conf = None, None
        self.mood, self.mood_conf = None, None          # 비전 mood 8종 (LABELS.md 4.2)
        self._last_debug_pose = 0.0
        self.speaking, self.utterance_id, self._speak_deadline = False, 0, None
        self._thinking_until = self._recover_until = self._wake_until = None
        self._absent_since: float | None = None
        self.present: bool | None = None
        # 센서 스냅샷 (알림 근거 + 평가 로그)
        self.pose = {"pred": "no_fall", "conf": 0.0}
        self.audio_fall_at: float | None = None
        self.audio_fall_conf = 0.0
        self.fall_episode_start: float | None = None
        self.radar_motion: str | None = None
        self.vision_motion: str | None = None
        self.fused = {"pred": "no_fall", "conf": 0.0}
        self.dialogue_state = "NORMAL"
        self.risk_score = 0
        # 알림
        self._alert_counter = 0
        self.current_alert_id: str | None = None
        # 기기 상태
        self.device = {"camera": self._initial_dev("vision"), "mic": self._initial_dev("audio"),
                       "radar": self._initial_dev("radar")}
        self._last_pose_at: float | None = None
        self._last_device_push = 0.0
        self._eval = open(ctx.log_path("eval_log"), "a", encoding="utf-8")

        for t, fn in {
            "pose_pred": self.on_pose_pred, "person_status": self.on_person_status, "emotion": self.on_emotion,
            "audio_fall": self.on_audio_fall, "speech": self.on_speech, "radar": self.on_radar,
            "dialogue_output": self.on_dialogue_output, "alert": self.on_alert, "tts_done": self.on_tts_done,
            "guardian_resolve": self.on_guardian_resolve, "guardian_message": self.on_guardian_message,
            "device": self.on_device, "client": self.on_client, "motion": self.on_motion,
        }.items():
            bus.subscribe(t, fn)
        # 실시간 센서 로그 (보호자 대시보드 '실시간 센서 로그' 카드): 워커/모듈 디버그 이벤트를 그대로 중계
        for t in ("vision_debug", "emotion_debug", "audio_debug", "audio_fall", "speech", "voice_unclear",
                  "fall_detected", "radar", "dialogue_output", "mood_pred", "mood_changed"):
            bus.subscribe(t, self._relay_debug)
        bus.subscribe("mood_pred", self.on_mood)

    def _initial_dev(self, module: str) -> str:
        m = self.ctx.mode(module)
        return {"off": "off", "mock": "mock"}.get(m, "starting")

    def _phone_source(self, module: str) -> bool:
        return self.ctx.mode(module) == "worker" and str(self.cfg["modules"][module].get("source", "phone")) == "phone"

    def note_stream(self, kind: str) -> None:
        """폰에서 카메라 프레임('camera') / 마이크 PCM('mic') 이 도착했을 때 서버가 호출."""
        now = self.clock()
        if kind == "camera":
            self._last_frame_at = now
        else:
            self._last_pcm_at = now

    def _record(self, type: str, data: dict | None = None, ts: float | None = None) -> None:
        if self.store is not None:
            self.store.record(type, data, ts)

    # ---------------- 로봇 얼굴 ----------------
    def _push_state(self, speak: bool = False) -> None:
        msg = {"type": "state", "state": self.ui_state, "emotion": self.emotion,
               "emotion_raw": self.emotion_raw, "emotion_conf": self.emotion_conf,
               "mood": self.mood, "mood_conf": self.mood_conf,
               "text": self.text, "countdown": self.countdown}
        if speak:
            self.utterance_id += 1
            msg.update(speak=True, utterance_id=self.utterance_id)
        self.broker.send_robot(msg)
        self.broker.send_guardian({**{k: v for k, v in msg.items() if k != "speak"}, "type": "robot_state",
                                   "emergency_active": self.current_alert_id is not None})

    def _set_state(self, state: str, text: str | None = None, countdown: float | None = None,
                   speak: bool = False) -> None:
        self.ui_state, self.countdown = state, countdown
        if text is not None:
            self.text = text
        self._push_state(speak=speak)

    def _speak(self, state: str, text: str, countdown: float | None = None) -> None:
        self.speaking = True
        self._speak_deadline = self.clock() + max(3.0, 0.12 * len(text)) + self.cfg["robot"]["tts_timeout_sec"]
        self.ctx.mic_muted.set()
        self.ctx.bus.publish("mic_mute", muted=True)
        self._set_state(state, text, countdown, speak=True)

    def on_tts_done(self, ev) -> None:
        self._finish_speaking()

    def _finish_speaking(self) -> None:
        if not self.speaking:
            return
        self.speaking, self._speak_deadline = False, None
        self.ctx.mic_muted.clear()
        self.ctx.bus.publish("mic_mute", muted=False)
        if self.ui_state == "speaking":
            self._set_state("listening", "")
        elif self.ui_state == "recover":
            self._recover_until = self.clock() + 3.0

    # ---------------- 센서 ----------------
    def on_pose_pred(self, ev) -> None:
        if ev.pred == "fall" and self.pose["pred"] != "fall":
            self.fall_episode_start = ev.ts          # 낙상 에피소드 시작 (알림 근거는 이 시점 이후 신호로)
        elif ev.pred == "no_fall":
            self.fall_episode_start = None
        self.pose = {"pred": ev.pred, "conf": float(ev.conf)}
        self._last_pose_at = self.clock()
        if not self._phone_source("vision") and self.device["camera"] in ("starting", "stale"):
            self.device["camera"] = "ok"
        self._write_eval(ev.ts)

    def on_person_status(self, ev) -> None:
        now = self.clock()
        self.present = bool(ev.present)
        if self.present:
            self._absent_since = None
            if self.ui_state == "sleep":
                self._set_state("wake", "")
                self._wake_until = now + self.cfg["robot"]["wake_to_listen_sec"]
        elif self._absent_since is None:
            self._absent_since = now

    def on_emotion(self, ev) -> None:
        """비전 `emotion` (구 규약 5종 라벨; 비전 v2 부터는 mood 에서 파생돼 들어온다)."""
        label = ev.label
        self.emotion_raw, self.emotion_conf = ev.get("raw"), ev.get("confidence")
        if ev.get("mood"):
            self.mood = ev.get("mood")
        if label != self.emotion:
            self.emotion = label
            self._push_state()
            self._record("emotion", {"label": label, "confidence": ev.get("confidence"), "mood": ev.get("mood")}, ev.ts)

    def on_mood(self, ev) -> None:
        """비전 `mood_pred` (1초 주기, LABELS.md 4.2) — 상태 메시지에 실어 UI 가 배지로 쓴다."""
        self.mood, self.mood_conf = ev.pred, ev.get("conf")

    def on_audio_fall(self, ev) -> None:
        self.audio_fall_at, self.audio_fall_conf = ev.ts, float(ev.get("confidence", 0.0))
        if not self._phone_source("audio") and self.device["mic"] in ("starting",):
            self.device["mic"] = "ok"

    def on_speech(self, ev) -> None:
        if not self._phone_source("audio") and self.device["mic"] in ("starting",):
            self.device["mic"] = "ok"
        self._record("dialogue", {"who": "user", "text": ev.text}, ev.ts)
        if self.ui_state in ("sleep", "wake", "listening") and not self.speaking:
            self._set_state("thinking", "")
            self._thinking_until = self.clock() + 3.0

    def on_radar(self, ev) -> None:
        self.radar_motion = ev.motion
        if self.device["radar"] in ("starting",):
            self.device["radar"] = "ok"

    def on_motion(self, ev) -> None:
        self.vision_motion = ev.motion

    def on_device(self, ev) -> None:
        for k in ("camera", "mic", "radar"):
            if k in ev.data:
                self.device[k] = ev.data[k]
        self._push_device()

    def on_client(self, ev) -> None:
        if ev.get("role") == "robot" and ev.get("connected"):
            self._push_state()            # 막 접속한 폰에 현재 얼굴 상태를 바로 보여 준다
        self._push_device()

    # ---------------- 대화 모듈 출력 ----------------
    def on_dialogue_output(self, ev) -> None:
        d = ev.data
        self.dialogue_state = d.get("state", self.dialogue_state)
        a = d.get("assessment") or {}
        if "risk_score" in a:
            self.risk_score = int(a["risk_score"])
        self._update_fused(d)
        strategy, text = d.get("strategy"), d.get("response_text")
        if text:
            self._record("dialogue", {"who": "robot", "text": text, "strategy": strategy, "state": d.get("state")}, ev.ts)
            if strategy in SAFETY_ASK:
                self._speak("alert", text, d.get("response_deadline_sec"))
            elif strategy == "ESCALATE":
                self._speak("alert", text, 0)
            elif strategy == "REASSURE":
                self._speak("alert", text, None)
            elif strategy == "RESUME":
                self._speak("recover", text)
            else:
                self._speak("speaking", text)
        elif self.ui_state == "thinking" and strategy == "NONE":
            self._thinking_until = None
            self._set_state("listening", "")

    def _update_fused(self, d: dict) -> None:
        score = self.risk_score
        if d.get("state") == "SAFETY_CHECK" or d.get("emergency_active"):
            pred = "fall"
        elif score >= 20:
            pred = "uncertain"
        else:
            pred = "no_fall"
        self.fused = {"pred": pred, "conf": round(min(1.0, score / 100.0), 2)}

    # ---------------- 보호자 알림 ----------------
    def on_alert(self, ev) -> None:
        """혜원 모듈 AlertMessage.to_dict() → 규약 alert_created / alert_resolved / notice."""
        m, now = ev.data, ev.ts
        kind, status = m.get("kind"), m.get("status")
        if kind == "emergency":
            if status == "raised" or self.current_alert_id is None:
                self._alert_counter += 1
                self.current_alert_id = f"a-{self._alert_counter:03d}"
            sensors, fused = self._sensor_labels(now), dict(self.fused)
            self.broker.send_guardian({
                "type": "alert_created", "alert_id": self.current_alert_id, "ts": now,
                "status": status, "seq": m.get("seq"), "cause": m.get("cause"),
                "title": m.get("title"), "body": m.get("body"), "sensors": sensors, "fused": fused,
            })
            if self.store is not None:
                self.store.alert_created(self.current_alert_id, now, m.get("cause", ""), m.get("title", ""), sensors, fused)
            self._record("alert", {"alert_id": self.current_alert_id, "status": status, "cause": m.get("cause")}, now)
        elif kind == "resolved" and self.current_alert_id:
            self.broker.send_guardian({"type": "alert_resolved", "alert_id": self.current_alert_id,
                                       "resolution": "elder_ok", "handled_by": "robot", "ts": now,
                                       "title": m.get("title"), "body": m.get("body")})
            if self.store is not None:
                self.store.alert_resolved(self.current_alert_id, now, "elder_ok", "robot")
            self.current_alert_id = None
        elif kind == "notice":
            self.broker.send_guardian({"type": "notice", "ts": now, "title": m.get("title"), "body": m.get("body")})
            self._record("notice", {"title": m.get("title"), "body": m.get("body")}, now)

    def on_guardian_resolve(self, ev) -> None:
        alert_id = ev.get("alert_id") or self.current_alert_id
        if not alert_id:
            return
        resolution, who = ev.get("resolution", "guardian_confirmed"), ev.get("handled_by", "guardian_1")
        self.broker.send_guardian({"type": "alert_resolved", "alert_id": alert_id, "resolution": resolution,
                                   "handled_by": who, "ts": self.clock()})
        if self.store is not None:
            self.store.alert_resolved(alert_id, self.clock(), resolution, who)
        if alert_id == self.current_alert_id:
            self.current_alert_id = None
        self.ctx.bus.publish("alert_cleared", alert_id=alert_id, reason=resolution)
        self._speak("recover", GUARDIAN_RESOLVE_TEXT.get(resolution, GUARDIAN_RESOLVE_TEXT["guardian_confirmed"]))

    def on_guardian_message(self, ev) -> None:
        state = "alert" if self.ui_state == "alert" else "speaking"
        self._speak(state, ev.text, self.countdown if state == "alert" else None)

    def _relay_debug(self, ev) -> None:
        kind = {"vision_debug": "vision", "emotion_debug": "emotion", "audio_debug": "audio",
                "dialogue_output": "dialogue", "mood_pred": "mood", "mood_changed": "mood_changed"}.get(ev.type, ev.type)
        if ev.type == "dialogue_output":
            if not ev.get("response_text") and ev.get("strategy") == "NONE":
                return
            data = {"strategy": ev.get("strategy"), "text": ev.get("response_text"), "reason": ev.get("reason"),
                    "state": ev.get("state")}
        else:
            data = {k: v for k, v in ev.data.items() if k not in ("probs",)}
        self.broker.send_guardian({"type": "debug", "kind": kind, "ts": ev.ts, **data})

    # ---------------- 주기 작업 ----------------
    def tick(self) -> None:
        now = self.clock()
        if self.speaking and self._speak_deadline and now >= self._speak_deadline:
            log.warning("tts_done 미수신 → 타임아웃으로 종료")
            self._finish_speaking()
        if self._thinking_until and now >= self._thinking_until and self.ui_state == "thinking":
            self._thinking_until = None
            self._set_state("listening", "")
        if self._recover_until and now >= self._recover_until:
            self._recover_until = None
            if self.ui_state == "recover":
                self._set_state("wake", "")
                self._wake_until = now + self.cfg["robot"]["wake_to_listen_sec"]
        if self._wake_until and now >= self._wake_until:
            self._wake_until = None
            if self.ui_state == "wake":
                self._set_state("listening", "")
        if (self._absent_since is not None and self.ui_state in ("listening", "wake")
                and now - self._absent_since >= self.cfg["robot"]["sleep_after_absent_sec"]):
            self._set_state("sleep", "")
        changed = self._refresh_stream_devices(now)
        if (not self._phone_source("vision") and self._last_pose_at and self.device["camera"] == "ok"
                and now - self._last_pose_at > 5):
            self.device["camera"] = "stale"
            changed = True
        if changed or now - self._last_device_push >= 5:
            self._push_device()

    def _refresh_stream_devices(self, now: float) -> bool:
        """카메라/마이크가 폰 스트림일 때: 최근 3초 안에 데이터가 왔으면 ok, 폰이 없으면 off, 끊기면 stale."""
        changed = False
        for module, key, last in (("vision", "camera", self._last_frame_at), ("audio", "mic", self._last_pcm_at)):
            if not self._phone_source(module):
                continue
            if not self.broker.robot_online:
                st = "off"
            elif last is None:
                st = "starting"
            elif now - last <= 3.0:
                st = "ok"
            else:
                st = "stale"
            if self.device[key] != st:
                self.device[key] = st
                changed = True
        return changed

    # ---------------- 기기 상태 / 평가 로그 ----------------
    def _push_device(self) -> None:
        self._last_device_push = self.clock()
        info = self.broker.robot_info
        self.broker.send_guardian({"type": "device_status", "robot_online": self.broker.robot_online,
                                   "battery": info.get("battery"), "wifi": "ok" if self.broker.robot_online else "unknown",
                                   "radar": self.device["radar"], "camera": self.device["camera"],
                                   "mic": self.device["mic"], "ts": self.clock()})

    def _audio_pred(self, now: float) -> dict:
        if self.ctx.mode("audio") == "off":
            return {"pred": "uncertain", "conf": 0.0}
        if self.audio_fall_at is not None and now - self.audio_fall_at <= 5.0:
            return {"pred": "fall", "conf": round(self.audio_fall_conf, 2)}
        return {"pred": "no_fall", "conf": 0.9}

    def _radar_pred(self) -> dict:
        m = self.radar_motion
        if m == "still":
            return {"pred": "uncertain", "conf": 0.5, "motion": m}
        if m == "moving":
            return {"pred": "no_fall", "conf": 0.7, "motion": m}
        return {"pred": "uncertain", "conf": 0.0, "motion": m or "unknown"}

    def _sensor_labels(self, now: float) -> dict:
        """alert_created 의 판정 근거: 평가 로그와 달리 '이번 낙상 에피소드 안에' 낙상음이 있었으면 audio=fall.
        레이더가 없으면 비전 움직임(motion)으로 대신하고 '(camera)' 를 붙인다."""
        radar = {"still": "no_motion", "moving": "motion", "none": "absent"}.get(self.radar_motion or "", "unknown")
        if radar == "unknown" and self.vision_motion:
            radar = {"still": "no_motion", "moving": "motion"}[self.vision_motion] + "(camera)"
        audio = self._audio_pred(now)["pred"]
        if (self.audio_fall_at is not None and self.fall_episode_start is not None
                and self.audio_fall_at >= self.fall_episode_start - 5.0):
            audio = "fall"
        return {"pose": self.pose["pred"], "audio": audio, "radar": radar}

    def _write_eval(self, ts: float) -> None:
        line = {"ts": round(ts, 2), "scenario_id": self.cfg["logs"]["scenario_id"], "ground_truth": None,
                "pose": self.pose, "audio": self._audio_pred(ts), "radar": self._radar_pred(), "fused": self.fused}
        self._eval.write(json.dumps(line, ensure_ascii=False) + "\n")
        self._eval.flush()

    def snapshot(self) -> dict[str, Any]:
        return {"ui_state": self.ui_state, "emotion": self.emotion, "text": self.text, "countdown": self.countdown,
                "speaking": self.speaking, "present": self.present, "pose": self.pose,
                "audio": self._audio_pred(self.clock()), "radar": self._radar_pred(), "fused": self.fused,
                "dialogue_state": self.dialogue_state, "risk_score": self.risk_score,
                "alert_id": self.current_alert_id, "device": self.device}
