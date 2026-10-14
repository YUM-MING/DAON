"""⑥ 대화 맥락 관리: 대화 이력, 최근 이벤트(확신도 포함), 감정 흐름, 반복 주제, 누움 지속시간"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from typing import Optional


FLOOR_POSES = ("fall", "lying")   # 바닥에 있는 자세 (dialogue_types.VALID의 pose 라벨 중)


@dataclass
class Turn:
    role: str
    text: str
    ts: float
    state: str = ""
    strategy: str = ""


class ConversationContext:
    def __init__(self, cfg):
        self.cfg = cfg
        self.history: deque = deque(maxlen=20)
        self.last = {"pose": "normal", "emotion": "neutral", "motion": None}   # motion None = 받은 적 없음
        self.motion_ts: Optional[float] = None # 마지막으로 mmWave 값을 받은 시각
        self.fall_seen_ts: Optional[float] = None  # 낙상 사건 중 마지막으로 바닥 자세(fall/lying)를 받은 시각
        self.fall_active = False               # 낙상 사건 진행 중 (아직 일어난 것이 확인되지 않음)
        self.fall_handled = False              # 지금 이어지는 낙상 상태를 이미 안전 확인에 썼는지
        self.fall_handled_at: Optional[float] = None  # 마지막으로 그 낙상을 확인한 시각 (장시간 지속 재확인 기준)
        self.recovered_since: Optional[float] = None  # normal 자세가 이어지기 시작한 시각
        self.emotion_ts = -1e9
        self.emotion_conf = 1.0
        self.events: list = []                 # (kind, ts, conf): fall_pose / fall_sound / lying_pose
        self.negative_streak = 0               # 연속 부정 발화 횟수
        self.probe_streak = 0                  # 조심스러운 되묻기(GENTLE_PROBE) 연속 횟수
        self.last_topic: Optional[str] = None  # 직전 발화의 화제 (같은 화제면 이어가기 응답)
        self.topic_counts: Counter = Counter() # 부정 발화에서 반복된 주제
        self.notice_sent = False
        self.followup_at_streak = 0            # 마지막 되묻기를 한 시점의 연속 횟수
        self.last_ai_ts: Optional[float] = None
        self.lying_still_since: Optional[float] = None

    # --- 입력 관측 ---
    def observe(self, inp) -> bool:
        """새 입력을 반영하고, '표정 감정이 새로 바뀌었는지' 반환"""
        now = inp.timestamp
        changed = False
        if inp.emotion is not None:
            conf = 1.0 if inp.emotion_conf is None else inp.emotion_conf
            if conf >= self.cfg.min_confidence:      # 확신도 낮은 표정은 무시
                changed = (inp.emotion != self.last["emotion"]
                           or now - self.emotion_ts > self.cfg.emotion_ttl)
                self.last["emotion"], self.emotion_ts, self.emotion_conf = inp.emotion, now, conf
        for key in ("pose", "motion"):
            v = getattr(inp, key)
            if v is not None:
                self.last[key] = v
                if key == "motion":
                    self.motion_ts = now
        pc = 1.0 if inp.pose_conf is None else inp.pose_conf
        sc = 1.0 if inp.sound_conf is None else inp.sound_conf
        # 낙상 사건: 바닥 자세(fall/lying) 사이의 흔들림은 같은 사건, normal이 recover_secs 이상 이어지면 일어남.
        if inp.pose == "normal":
            if self.recovered_since is None:
                self.recovered_since = now
            if self._recovered(now):
                self.fall_active = False
        elif inp.pose in FLOOR_POSES:
            if self._recovered(now):
                self.fall_active = False               # 일어났다가 다시 바닥 → 이전 사건은 끝
            self.recovered_since = None
            if self.fall_active and inp.pose == "lying":
                self.fall_seen_ts = now                # 넘어진 뒤 누운 자세로 흔들려도 사건 유지
        if inp.pose == "fall":
            # 매 프레임 전송이든 변화 시 전송이든, 같은 낙상 상태가 이어지는 동안은 한 사건으로 본다.
            # 일어난 뒤이거나 바닥 자세가 한참 끊긴 뒤에 다시 들어오면 새 낙상.
            if (not self.fall_active or self.fall_seen_ts is None
                    or now - self.fall_seen_ts > self.cfg.fall_repeat_gap):
                self.fall_handled, self.fall_handled_at = False, None
            self.fall_active, self.fall_seen_ts = True, now
            if not self.fall_handled:
                self.events.append(("fall_pose", now, pc))
        if inp.pose == "lying":
            self.events.append(("lying_pose", now, pc))
        if inp.sound_event == "fall_sound":
            self.events.append(("fall_sound", now, sc))
        self.events = [e for e in self.events if now - e[1] <= self.cfg.fusion_window * 2]

        lying_still = self.last["pose"] == "lying" and self.last["motion"] in ("still", "none")
        if lying_still:
            if self.lying_still_since is None:
                self.lying_still_since = now
        else:
            self.lying_still_since = None
        return changed

    def best(self, kind: str, now: float) -> Optional[float]:
        """융합 창 안에서 해당 신호의 최대 확신도. 없으면 None"""
        confs = [c for k, t, c in self.events if k == kind and 0 <= now - t <= self.cfg.fusion_window]
        return max(confs) if confs else None

    def recent(self, kind: str, now: float) -> bool:
        return self.best(kind, now) is not None

    def motion(self, now: float) -> Optional[str]:
        """유효시간 안에 받은 mmWave 값. 받은 적 없거나 오래됐으면 None(정보 없음)"""
        if self.motion_ts is None or now - self.motion_ts > self.cfg.motion_ttl:
            return None
        return self.last["motion"]

    def _recovered(self, now: float) -> bool:
        return self.recovered_since is not None and now - self.recovered_since >= self.cfg.recover_secs

    def fall_persist_secs(self, now: float) -> float:
        """이미 확인한 낙상 사건에서, 일어나지 않고 바닥 자세가 이어진 시간 (움직임 여부와 무관)"""
        if not (self.fall_active and self.fall_handled and self.fall_handled_at is not None):
            return 0.0
        if self.last["pose"] not in FLOOR_POSES:
            return 0.0
        return now - self.fall_handled_at

    def lying_still_secs(self, now: float) -> float:
        return 0.0 if self.lying_still_since is None else now - self.lying_still_since

    def consume_events(self, now: float):
        """안전 확인에 사용된 이벤트는 비우고, 누움 타이머도 재시작 (같은 신호 재트리거 방지)"""
        self.events.clear()
        self.fall_handled = True               # 이어서 들어오는 같은 낙상 상태는 다시 쓰지 않는다
        self.fall_handled_at = now             # 대신 바닥 자세가 오래 이어지면 이 시각부터 재확인 시간을 잰다
        if self.lying_still_since is not None:
            self.lying_still_since = now

    # --- 대화 기록 ---
    def register_user_turn(self, negative: bool, topics: list):
        if negative:
            self.negative_streak += 1
            for t in topics:
                self.topic_counts[t] += 1
        else:
            self.negative_streak = 0
            self.topic_counts.clear()
            self.notice_sent = False
            self.followup_at_streak = 0

    def repeated_topic(self) -> Optional[str]:
        for topic, n in self.topic_counts.most_common():
            if n >= self.cfg.topic_repeat_for_followup:
                return topic
        return None

    def add_turn(self, role, text, ts, state="", strategy=""):
        self.history.append(Turn(role, text, ts, state, strategy))
        if role == "ai":
            self.last_ai_ts = ts

    def recent_ai(self, n=None) -> list:
        n = n or self.cfg.recent_window
        return [t.text for t in self.history if t.role == "ai"][-n:]

    def recent_texts(self, n=6) -> list:
        return [(t.role, t.text) for t in list(self.history)[-n:]]