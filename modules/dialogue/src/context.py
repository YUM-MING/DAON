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
        self.prev_topic: Optional[str] = None  # 그 전 발화의 화제 (LLM 문맥용)
        self.resume_topic: Optional[str] = None  # STT 재질문 직전의 화제: 다시 알아들으면 이 화제로 돌아간다
        self.topic_counts: Counter = Counter() # 부정 발화에서 반복된 주제
        self.notice_sent = False
        self.followup_at_streak = 0            # 마지막 되묻기를 한 시점의 연속 횟수
        self.last_ai_ts: Optional[float] = None
        self.lying_still_since: Optional[float] = None
        # 이번 대화(엔진 하나)에서 들은 증상. 장기 기억(memory.py)과 별개이며 저장하지 않는다
        self.symptoms: list = []               # SymptomNote, 오래된 것부터
        self.user_turns = 0                    # 어르신 발화 수 (증상을 말한 턴과 이후 턴 구분)
        self.pain_question: Optional[tuple] = None   # (SymptomNote, ts): "견딜 만하세요?"를 물은 증상과 시각
        self.dangers: list = []                # DangerNote: 이번 대화에서 들은 위험 증상 (가슴·숨·어지럼 등)
        # 마지막 낙상 근거 시각 (낙상 포즈·낙상음·낙상 중 누움). 안전 확인을 시작해도 지우지 않는다
        # (events는 consume_events로 비워지므로, '최근 낙상 근거' 판단은 이 값으로 한다)
        self.fall_evidence_ts: Optional[float] = None
        self.normal_seen_ts: Optional[float] = None   # 정상 자세(normal)를 마지막으로 실제로 받은 시각
        self.floor_seen_ts: Optional[float] = None    # 바닥 자세(fall/lying)를 마지막으로 받은 시각
        # 소리 단독 확인에 쓴 낙상음 (ts, conf, until): 확인 중·종료 뒤 일정 시간 안에 바닥 자세가 오면 함께 근거로 쓴다
        # (until=None이면 확인 진행 중). events는 확인 시작 때 비워지므로 따로 보관한다
        self.sound_hold: Optional[tuple] = None
        self.sound_cooldown_until: float = float("-inf")   # 정상 자세가 보이는 소리 단독 재확인을 보류하는 끝 시각
        self.lying_recheck_at: Optional[float] = None      # 누움 확인에 괜찮다고 한 뒤, 이 시각 전에는 누움 무동작 재확인 안 함
        self.urgent_prompt: Optional[float] = None   # "급하시면 바로 말씀해 주세요"라고 물은 시각 (다음 발화의 '급해'를 구조 요청으로)
        self.unclear_streak = 0                # 연속된 STT 오류 발화 수
        self.last_unclear_key: Optional[str] = None   # 직전 STT 오류 종류 (shout | garbled), 정상 발화면 None

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
        if inp.pose in FLOOR_POSES:
            self.floor_seen_ts = now
        if inp.pose == "normal":
            self.normal_seen_ts = now
            self.lying_recheck_at = None           # 일어났으면 누움 재확인 보류도 끝 (다시 누우면 기존 기준)
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
        if ((inp.pose == "fall" and pc >= self.cfg.min_confidence)
                or (inp.sound_event == "fall_sound" and sc >= self.cfg.min_confidence)
                or (inp.pose == "lying" and self.fall_active)):
            self.fall_evidence_ts = now
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

    def last_sound(self, now: float) -> Optional[tuple]:
        """융합 창 안의 가장 최근 낙상음 (ts, conf). 없으면 None"""
        snd = [(t, c) for k, t, c in self.events if k == "fall_sound" and 0 <= now - t <= self.cfg.fusion_window]
        return max(snd) if snd else None

    def held_sound(self, now: float) -> Optional[tuple]:
        """보류 중인 소리 단독 확인의 낙상음 (ts, conf). 확인 진행 중이거나 종료 뒤 보류 시간 안일 때만"""
        h = self.sound_hold
        if h is None or (h[2] is not None and now > h[2]):
            return None
        return h[0], h[1]

    def normal_around(self, ts: float, now: float) -> bool:
        """낙상음 시각 ts 전후 sound_pose_fresh_secs 안에 정상 자세를 실제로 받았고, 지금 마지막 자세가 정상인지.
        소리보다 오래 전의 정상 관측, 받은 적 없는 기본값(normal)은 인정하지 않는다"""
        n, fresh = self.normal_seen_ts, self.cfg.sound_pose_fresh_secs
        return (n is not None and self.last["pose"] == "normal" and ts - fresh <= n <= ts + fresh
                and (self.floor_seen_ts is None or self.floor_seen_ts < n))

    def normal_kept(self, sound_ts: float, now: float) -> bool:
        """지금도 신선한 정상 자세: 마지막 정상 관측이 sound_pose_fresh_secs 안이고, 소리 뒤로 바닥 자세가 없음"""
        n = self.normal_seen_ts
        return (n is not None and self.last["pose"] == "normal" and now - n <= self.cfg.sound_pose_fresh_secs
                and (self.floor_seen_ts is None or self.floor_seen_ts < sound_ts))

    def lying_due(self, now: float) -> bool:
        """누운 채 무동작 재확인 시점이 됐는지 (누움 확인에 괜찮다고 한 뒤면 lying_recheck_at까지 미룸)"""
        return (self.lying_still_secs(now) >= self.cfg.lying_still_secs
                and (self.lying_recheck_at is None or now >= self.lying_recheck_at))

    def recent(self, kind: str, now: float) -> bool:
        return self.best(kind, now) is not None

    def motion(self, now: float) -> Optional[str]:
        """유효시간 안에 받은 mmWave 값. 받은 적 없거나 오래됐으면 None(정보 없음)"""
        if self.motion_ts is None or now - self.motion_ts > self.cfg.motion_ttl:
            return None
        return self.last["motion"]

    def _recovered(self, now: float) -> bool:
        return self.recovered_since is not None and now - self.recovered_since >= self.cfg.recover_secs

    def recovered(self, now: float) -> bool:
        """정상 자세(normal)를 실제로 recover_secs 이상 '받았는지' (처음 받은 시각 ~ 마지막으로 받은 시각).
        자세 정보가 끊긴 시간(카메라 이탈·uncertain)은 회복으로 세지 않는다: 서버는 uncertain을 보내지 않으므로
        시간이 흘렀다는 것만으로 회복이라고 보면 안 된다."""
        return (self.last["pose"] == "normal" and self.recovered_since is not None
                and self.normal_seen_ts is not None
                and self.normal_seen_ts - self.recovered_since >= self.cfg.recover_secs)

    def recent_fall_evidence(self, now: float) -> bool:
        """최근 낙상 근거: fusion_window(15초) 안의 낙상 포즈·낙상음, 또는 일어나지 않은 채 바닥 자세가 이어짐"""
        if self.fall_evidence_ts is not None and 0 <= now - self.fall_evidence_ts <= self.cfg.fusion_window:
            return True
        return self.fall_active and self.last["pose"] in FLOOR_POSES and not self.recovered(now)

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

    # --- 이번 대화의 증상 기억 ---
    def symptom_repeated(self, parts: list, now: float) -> bool:
        """같은 증상을 symptom_repeat_secs 안에 이미 말했는지. 부위를 말하지 않았으면 최근 증상이 있기만 해도 반복"""
        recent = [s for s in self.symptoms if 0 <= now - s.ts <= self.cfg.symptom_repeat_secs]
        if not parts:
            return bool(recent)
        return any(s.part in parts or s.part is None for s in recent)

    def note_symptom(self, parts: list, said: str, now: float, followed: bool = False) -> None:
        for p in (parts or [None]):
            self.symptoms.append(SymptomNote(p, said, now, self.user_turns, followed))
        del self.symptoms[:-10]

    def pending_checkin(self, now: float):
        """이전 턴에 들었고 아직 다시 묻지 않은 최근 증상 (10분 이내). 없으면 None"""
        for s in reversed(self.symptoms):
            if (not s.followed and s.turn < self.user_turns
                    and 0 <= now - s.ts <= self.cfg.symptom_repeat_secs):
                return s
        return None

    def mark_symptoms_followed(self) -> None:
        for s in self.symptoms:
            s.followed = True

    # --- 이번 대화의 위험 증상 기록 (회상 질문 판단용) ---
    def note_dangers(self, kinds: list, now: float) -> None:
        for k in kinds:
            self.dangers.append(DangerNote(k, now))
        del self.dangers[:-10]

    def danger_record(self, kind: str):
        """그 위험 증상의 가장 최근 기록 (없으면 None)"""
        return next((d for d in reversed(self.dangers) if d.kind == kind), None)

    def resolve_dangers(self) -> None:
        """어르신이 괜찮다고 분명히 말해 안전 확인·비상이 해제됐을 때만 부른다 (움직임만 보고 복귀한 경우는 아님)"""
        for d in self.dangers:
            d.resolved = True


@dataclass
class DangerNote:
    kind: str                  # chest | breath | collapse | dizzy | bleed | injury | immobile
    ts: float
    resolved: bool = False     # 이후 명확한 안전 응답("괜찮아")으로 해제됐는지


@dataclass
class SymptomNote:
    part: Optional[str]        # "머리" 등, 부위를 말하지 않았으면 None
    said: str                  # "아프다고", "쑤신다고"
    ts: float
    turn: int                  # 말한 턴 (user_turns 기준)
    followed: bool = False     # 이후에 "좀 어떠세요?"로 다시 물었는지