"""시나리오 통합 테스트 — 하드웨어 없이 대화 모듈(실물) + 조정자 + 가짜 브로커로 끝까지 돌린다.

    .venv\Scripts\activate && python -m pytest server\tests -q

가짜 시계로 시간을 흘려보내므로 10초 카운트다운도 즉시 검증된다.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from server import config as cfgmod
from server.adapters.dialogue import DialogueAdapter
from server.core.broker import FakeBroker
from server.core.context import Context
from server.core.coordinator import Coordinator
from server.core.home import HomeStore
from server.db import DB


class Clock:
    def __init__(self, t: float = 1_760_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Rig:
    """버스 + 조정자 + 대화 어댑터를 묶고, 시간을 흘리는 헬퍼."""

    def __init__(self) -> None:
        tmp = tempfile.mkdtemp()
        cfg = cfgmod.load(None, mock=True, overrides={"logs": {"dir": tmp}})
        self.clock = Clock()
        self.ctx = Context(cfg, home_id=1, home_code="test")
        self.bus = self.ctx.bus
        self.broker = FakeBroker()
        self.db = DB(os.path.join(tmp, "test.db"))
        user = self.db.create_user("t@t.t", "pass", "t")
        self.home = self.db.create_home("집", user["id"])
        self.coord = Coordinator(self.ctx, self.broker, clock=self.clock, store=HomeStore(self.db, self.home["id"]))
        self.dialogue = DialogueAdapter(self.ctx, clock=self.clock, background=False)
        self.bus.subscribe("mic_mute", self._mute)
        self.muted = False
        # 사람이 보이는 상태로 시작
        self.bus.publish("person_status", ts=self.clock(), present=True, posture="sitting")

    def _mute(self, ev) -> None:
        self.muted = ev.muted

    def step(self) -> None:
        """밀린 입력을 전부 처리하고(없으면 tick 한 번) 조정자 타이머를 돌린다."""
        if self.dialogue.runner.pending() == 0:
            self.dialogue.step(timeout=0)
        while self.dialogue.runner.pending() > 0:
            self.dialogue.step(timeout=0)
        self.coord.tick()

    def advance(self, seconds: float, pose: str | None = "no_fall", posture: str = "sitting") -> None:
        """1초씩 시간을 흘리며 센서 주기 이벤트를 흉내 낸다. TTS는 바로 끝난 것으로 응답."""
        for _ in range(int(seconds)):
            self.clock.t += 1
            if pose:
                self.bus.publish("person_status", ts=self.clock(), present=True, posture=posture)
                self.bus.publish("pose_pred", ts=self.clock(), pred=pose, conf=0.9)
            self.step()
            self.finish_tts()

    def finish_tts(self) -> None:
        if self.coord.speaking:
            self.bus.publish("tts_done", utterance_id=self.coord.utterance_id, ok=True)

    def fall(self, audio: bool = True) -> None:
        self.bus.publish("pose_pred", ts=self.clock(), pred="fall", conf=0.9)
        if audio:
            self.bus.publish("audio_fall", ts=self.clock(), confidence=0.88)
        self.step()

    def say(self, text: str) -> None:
        self.bus.publish("speech", ts=self.clock(), text=text, duration=1.0)
        self.step()

    def states(self) -> list[str]:
        return [m["state"] for m in self.broker.robot if m["type"] == "state"]

    def guardian(self, type: str) -> list[dict]:
        return [m for m in self.broker.guardian if m["type"] == type]


@pytest.fixture
def rig() -> Rig:
    return Rig()


def test_scenario_b_fall_no_response_alerts_guardian(rig: Rig):
    rig.fall()
    # 1차 확인: alert 상태 + "괜찮으세요?" 발화 + 카운트다운
    alerts = [m for m in rig.broker.robot if m.get("state") == "alert"]
    assert alerts and alerts[0]["speak"] and "괜찮" in alerts[0]["text"]
    assert alerts[0]["countdown"] and alerts[0]["countdown"] <= 10
    assert rig.muted, "말하는 동안 마이크는 음소거"
    rig.finish_tts()
    assert not rig.muted

    # 포즈 낙상 + 낙상음이 한 입력으로 합쳐져 HIGH → 6초 × 2 = 12초 뒤 보호자 팝업 (체크리스트 15초 이내)
    rig.advance(16, pose="fall", posture="lying")
    created = rig.guardian("alert_created")
    assert created, "보호자 alert_created 가 와야 함"
    assert created[0]["ts"] - 1_760_000_000 <= 15
    assert created[0]["sensors"]["audio"] == "fall", "에피소드 안의 낙상음은 알림 근거에 남아야 함"
    a = created[0]
    assert a["alert_id"] == "a-001" and a["sensors"]["pose"] == "fall" and a["fused"]["pred"] == "fall"
    assert a["sensors"]["audio"] in ("fall", "no_fall")
    assert rig.coord.current_alert_id == "a-001"


def test_pose_only_fall_without_radar_takes_20s(rig: Rig):
    """카메라만 낙상(소리·레이더 없음) → MEDIUM → 10초 × 2 = 20초. 레이더/소리가 있어야 15초 목표를 맞춘다."""
    rig.fall(audio=False)
    rig.finish_tts()
    rig.advance(16, pose="fall", posture="lying")
    assert not rig.guardian("alert_created")
    rig.advance(5, pose="fall", posture="lying")
    assert rig.guardian("alert_created")


def test_scenario_b_with_radar_still_alerts_within_15s(rig: Rig):
    """레이더 '정지' 신호가 있으면 HIGH → 대기 6초 × 2 = 12초 안에 보호자 팝업 (체크리스트 15초 이내)."""
    rig.bus.publish("radar", ts=rig.clock(), motion="still", distance_cm=120)
    rig.fall()
    rig.finish_tts()
    for _ in range(15):
        rig.clock.t += 1
        rig.bus.publish("radar", ts=rig.clock(), motion="still", distance_cm=120)
        rig.bus.publish("person_status", ts=rig.clock(), present=True, posture="lying")
        rig.bus.publish("pose_pred", ts=rig.clock(), pred="fall", conf=0.9)
        rig.step()
        rig.finish_tts()
        if rig.guardian("alert_created"):
            break
    created = rig.guardian("alert_created")
    assert created and created[0]["ts"] - 1_760_000_000 <= 15
    assert created[0]["sensors"]["radar"] == "no_motion"


def test_false_alarm_elder_ok_no_guardian_alert(rig: Rig):
    rig.fall()
    rig.finish_tts()
    rig.clock.t += 3
    rig.say("응 괜찮아, 그냥 앉은 거야")
    assert "recover" in rig.states(), "오탐 해제 → recover 상태"
    rig.advance(20)
    assert not rig.guardian("alert_created"), "보호자 알림이 가면 안 됨"
    assert rig.coord.ui_state in ("listening", "wake", "recover")


def test_guardian_resolve_clears_dialogue_session(rig: Rig):
    rig.fall()
    rig.finish_tts()
    rig.advance(21, pose="fall", posture="lying")
    assert rig.guardian("alert_created")
    rig.bus.publish("guardian_resolve", alert_id="a-001", resolution="guardian_confirmed", handled_by="guardian_1")
    rig.step()
    resolved = rig.guardian("alert_resolved")
    assert resolved and resolved[-1]["handled_by"] == "guardian_1"
    assert rig.coord.current_alert_id is None
    assert rig.dialogue.engine.session is None, "대화 모듈 안전 확인 세션 종료"
    assert rig.coord.ui_state == "recover"


def test_scenario_a_emotion_and_chat(rig: Rig):
    rig.bus.publish("emotion", ts=rig.clock(), label="sad", raw="Sadness", confidence=0.8)
    rig.step()
    assert rig.broker.robot[-1]["emotion"] == "sad"
    rig.say("안녕")
    spoken = [m for m in rig.broker.robot if m.get("speak")]
    assert spoken and spoken[-1]["state"] == "speaking" and spoken[-1]["text"]
    rig.finish_tts()
    assert rig.coord.ui_state == "listening"


def test_eval_log_written(rig: Rig):
    rig.advance(3)
    with open(rig.ctx.log_path("eval_log"), encoding="utf-8") as f:
        lines = [ln for ln in f if ln.strip()]
    assert len(lines) >= 3
    import json
    rec = json.loads(lines[-1])
    assert set(rec) >= {"ts", "scenario_id", "pose", "audio", "radar", "fused"}
    assert rec["pose"]["pred"] in ("fall", "no_fall", "uncertain")


def test_db_records_alert_and_report(rig: Rig):
    rig.fall()
    rig.finish_tts()
    rig.advance(16, pose="fall", posture="lying")
    alerts = rig.db.alerts(rig.home["id"])
    assert alerts and alerts[0]["alert_id"] == "a-001" and alerts[0]["resolved_ts"] is None
    rig.bus.publish("guardian_resolve", alert_id="a-001", resolution="false_alarm", handled_by="g")
    rig.step()
    alerts = rig.db.alerts(rig.home["id"])
    assert alerts[0]["resolution"] == "false_alarm" and alerts[0]["handled_by"] == "g"
    rig.bus.publish("emotion", ts=rig.clock(), label="happy", raw="Happiness", confidence=0.9)
    rep = rig.db.report(rig.home["id"], days=365 * 10)
    assert rep["emotion_week"] and any(c["data"].get("who") == "robot" for c in rep["conversations"])


def test_accounts_homes_pairing():
    db = DB(os.path.join(tempfile.mkdtemp(), "a.db"))
    u = db.create_user("A@x.com", "1234", "A")
    assert db.verify_login("a@x.com", "1234")["id"] == u["id"] and db.verify_login("a@x.com", "no") is None
    h = db.create_home("A의 집", u["id"])
    assert len(h["code"]) == 6
    tok = db.create_session(u["id"])
    assert db.session_user(tok)["id"] == u["id"]
    g = db.create_user("g@x.com", "1234", "G")
    assert not db.is_member(g["id"], h["id"])
    db.join_home(g["id"], h["code"])
    assert db.is_member(g["id"], h["id"])
    r = db.pair_robot(h["code"], "phone")
    assert db.robot_by_token(r["token"])["home_id"] == h["id"]
    with pytest.raises(ValueError):
        db.pair_robot("000000")


def test_device_status_and_sleep(rig: Rig):
    rig.advance(1)
    assert rig.guardian("device_status")
    rig.bus.publish("person_status", ts=rig.clock(), present=False, posture="unknown")
    rig.clock.t += 40
    rig.coord.tick()
    assert rig.coord.ui_state == "sleep"


# ---------------- AI 요약 (보호자가 버튼을 누를 때만 LLM) ----------------
class FakeLLM:
    """외부 호출 없이 요약 로직만 검증. 호출 횟수와 받은 입력을 기록한다."""

    def __init__(self, reply: str | None = "■ 주요 화제: 안부") -> None:
        self.reply, self.calls, self.last_user, self.last_error = reply, 0, "", None

    def generate(self, system, messages, timeout=None, max_tokens=None):
        self.calls += 1
        self.last_user = messages[-1]["content"]
        self.last_error = None if self.reply else "timeout"
        return self.reply


def _summ_rig(n_msgs: int, llm: FakeLLM | None, **over):
    from server.core.summarizer import Summarizer
    db = DB(os.path.join(tempfile.mkdtemp(), "s.db"))
    user = db.create_user("s@s.s", "pass", "s")
    home = db.create_home("집", user["id"])
    clock = Clock(1_760_000_000.0 + 12 * 3600)         # 하루 중간 (로컬 자정 직후 경계 회피)
    for i in range(n_msgs):
        who = "robot" if i % 2 else "user"
        db.record_event(home["id"], "dialogue", {"who": who, "text": f"{'긴말' * 30 if who == 'robot' else '안녕'} {i}"},
                        ts=clock() - (n_msgs - i))
    cfg = {**cfgmod.DEFAULTS["summary"], **over}
    return Summarizer(db, cfg, client_factory=lambda: llm, clock=clock), db, home["id"], clock


def test_summary_too_few_messages_no_llm_call():
    llm = FakeLLM()
    s, db, hid, _ = _summ_rig(3, llm)
    r = s.summarize(hid, "today")["days"][0]
    assert r["status"] == "too_few" and r["count"] == 3 and llm.calls == 0


def test_summary_cached_until_new_dialogue():
    llm = FakeLLM()
    s, db, hid, clock = _summ_rig(6, llm)
    assert s.summarize(hid, "today")["days"][0]["status"] == "ok" and llm.calls == 1
    again = s.summarize(hid, "today")["days"][0]
    assert again["status"] == "cached" and again["text"] and llm.calls == 1      # 새 대화 없음 → LLM 안 부름
    db.record_event(hid, "dialogue", {"who": "user", "text": "새 대화"}, ts=clock() - 0.5)
    cool = s.summarize(hid, "today")["days"][0]
    assert cool["status"] == "cooldown" and cool["text"] and llm.calls == 1      # 새 대화 있어도 쿨다운 중
    clock.t += 301
    assert s.summarize(hid, "today")["days"][0]["status"] == "ok" and llm.calls == 2


def test_summary_limits_and_trimming():
    llm = FakeLLM()
    s, db, hid, _ = _summ_rig(40, llm, max_messages=10, max_chars=60, robot_text_chars=10)
    s.summarize(hid, "today")
    lines = llm.last_user.splitlines()
    assert len(llm.last_user) <= 60                                              # 글자 수 상한
    assert "안녕 36" in llm.last_user and "안녕 30" not in llm.last_user          # 오래된 것부터 버리고 최신 유지
    assert lines[-1].startswith("로봇")                                           # 마지막(39번)은 로봇 발화
    assert all(len(ln) <= len("로봇: ") + 11 for ln in lines if ln.startswith("로봇"))   # 로봇 발화는 줄임
    s2, _, hid2, _ = _summ_rig(40, FakeLLM(), max_messages=10)
    assert s2.summarize(hid2, "today")["days"][0]["count"] == 10


def test_dialogues_by_day_and_day_bounds():
    from server.core.summarizer import day_bounds
    s, db, hid, clock = _summ_rig(6, None)
    day, start, end = day_bounds(None, clock())
    assert end - start in (86400, 82800, 90000) and start <= clock() < end            # 오늘 하루 (DST 허용)
    assert len(db.dialogues_between(hid, start, end, 100)) == 6
    assert day_bounds(day)[1] == start                                                # 날짜 문자열로도 같은 범위
    assert db.dialogues_between(hid, start - 86400, start, 100) == []                 # 전날은 비어 있음
    with pytest.raises(ValueError):
        day_bounds("2026/11/06")


def test_summary_past_days_reuse_cache():
    """3일 요약 뒤 7일 요약: 이미 요약된 날은 LLM 을 다시 부르지 않고, 새 날짜만 부른다."""
    llm = FakeLLM()
    s, db, hid, clock = _summ_rig(6, llm)
    for off in range(1, 7):                                                           # 어제~6일 전에 각각 대화 6건
        for i in range(6):
            db.record_event(hid, "dialogue", {"who": "user", "text": f"{off}일 전 {i}"}, ts=clock() - off * 86400 - i)
    s.summarize(hid, "3")
    assert llm.calls == 3
    s.summarize(hid, "7")
    assert llm.calls == 7                                                             # 3 + 새로 4 (기존 3일은 재호출 없음)


def test_summary_daily_limit_and_failure_and_unavailable():
    s, db, hid, clock = _summ_rig(6, FakeLLM(reply=None))
    assert s.summarize(hid, "today")["days"][0]["status"] == "failed"            # 실패 → 저장 안 함
    assert db.summary_get(hid, s.summarize(hid, "today")["days"][0]["day"]) is None
    s_lim, _, hid3, _ = _summ_rig(6, FakeLLM(), daily_limit=0)
    assert s_lim.summarize(hid3, "today")["days"][0]["status"] == "limit"
    s_off, _, hid4, _ = _summ_rig(6, None)
    assert s_off.summarize(hid4, "today")["days"][0]["status"] == "unavailable" and not s_off.available()
    with pytest.raises(ValueError):
        s.summarize(hid, "30")

