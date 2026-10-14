"""교감형 대화 알고리즘 (대화 로직·안전 판단·비상 알림 모듈)

처리 흐름
  ① 입력 분석      text_analyzer.analyze   : STT → 의미 신호 (부정문/단어 경계 처리)
  ② 상황 판단      situation.assess        : pose·소리·mmWave·발화·표정 → 위험도 점수 + 정서 평가
  ③ 상태 결정      DialogueEngine          : NORMAL / EMOTIONAL / SAFETY_CHECK
  ④ 상태 전환      DialogueEngine          : 안전 확인 세션(대기→재질문→비상/복귀)
  ⑤ 응답 전략 결정  DialogueEngine          : 맥락(연속 부정, 반복 주제, 표정-발화 불일치)을 반영
  ⑥ 대화 맥락 관리  context.ConversationContext
문장은 responses.py의 템플릿을 쓰고, LLM은 일상/공감 문장 생성에만 선택적으로 쓴다.
안전 문장은 항상 고정 템플릿이다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import responses as R
from context import ConversationContext
from dialogue_types import (SPEECH_EVENTS, Config, DialogueInput, DialogueOutput, RiskLevel,
                            State, Strategy)
from situation import Situation, assess
from text_analyzer import TextSignals, analyze

__all__ = ["DialogueEngine", "DialogueInput", "DialogueOutput", "Config", "State", "Strategy"]

SAFETY_STRATEGIES = {Strategy.ASK_CHECK, Strategy.REASK, Strategy.RETRY_LISTEN,
                     Strategy.CLARIFY, Strategy.RESUME, Strategy.ESCALATE, Strategy.REASSURE}
CAUSE_RANK = {"no_response": 1, "danger": 2, "crisis": 3}     # 비상 중 더 심각한 사유가 오면 갱신
CAUSE_NOTE = {"danger": "위험 발화", "crisis": "극단적 선택 암시"}
NO_TEXT = TextSignals()        # 센서 근거만 다시 평가할 때 쓰는 빈 발화 신호
MAX_GENTLE_PROBES = 2          # 조심스러운 되묻기 연속 허용 횟수 (그다음부터는 물러서는 말)
# 지난 화제를 꺼내도 되는 일상 응답 (시간·식사·약처럼 답할 내용이 있는 말에는 끼어들지 않는다)
RECALL_KINDS = {"greeting", "generic", "positive", "family", "health", "sleep", "weather"}
RECALL_WINDOW = 2              # 세션 첫 2턴 안에서만 지난 화제를 꺼낸다 (아래 답변 턴은 세지 않음)
INFO_KINDS = {"time", "weather_ask", "abilities", "about_ai", "meal", "meal_done", "meal_not", "meal_ask", "medicine"}
# 이 응답이 나오면 그 세션에서는 지난 화제를 꺼내지 않는다 (안전 전략은 SAFETY_STRATEGIES로 따로 확인)
HEALTH_NOT_MEDICINE = ("아프", "아파", "허리", "무릎", "다리", "쑤시", "쑤셔", "저려", "병원")   # 약 이야기여도 건강으로 기억
RECALL_BLOCKERS = {Strategy.EMPATHIZE, Strategy.FOLLOW_UP, Strategy.GENTLE_PROBE}


def korean_time(ts: float) -> str:
    """입력 시각(초) → '오후 3시 20분' / '오전 9시 정각' (실행 PC 시간대)"""
    t = time.localtime(ts)
    half = "오전" if t.tm_hour < 12 else "오후"
    hour = t.tm_hour % 12 or 12
    return f"{half} {hour}시 " + (f"{t.tm_min}분" if t.tm_min else "정각")


# ───────────── 응답 생성 (템플릿 + 선택적 LLM) ─────────────
class ResponseGenerator:
    def __init__(self, llm: Optional[Callable[[dict], str]] = None):
        self.llm = llm  # llm(context_dict) -> str. 실패하면 템플릿으로 fallback

    def generate(self, strategy: Strategy, c: dict) -> Optional[str]:
        if strategy == Strategy.NONE:
            return None
        if strategy == Strategy.REASSURE:
            pool = R.REASSURE_CRISIS if c.get("cause") == "crisis" else R.REASSURE
            return self._pick(pool, c.get("recent_ai", []))
        if strategy in SAFETY_STRATEGIES:             # 안전 문장은 LLM 없이 고정
            return self._safety_text(strategy, c)
        if self.llm:
            try:
                text = self.llm({"strategy": strategy.value, **c})
                if text:
                    return text
            except Exception:
                pass                                  # API 실패 시에도 데모는 계속 돌아가야 함
        text = self._pick(self._pool(strategy, c), c.get("recent_ai", []))
        if "{time}" in text:                          # 시간 질문 응답: 현재 시각 채우기
            text = text.replace("{time}", c.get("time_text", ""))
        return text

    @staticmethod
    def _safety_text(strategy, c) -> str:
        v = c.get("variant")
        if strategy == Strategy.ASK_CHECK:
            return R.ASK_CHECK.get(v, R.ASK_CHECK["fall"])
        if strategy == Strategy.RESUME:
            return R.RESUME.get(v, R.RESUME["default"])
        if strategy == Strategy.ESCALATE:
            return R.ESCALATE.get(c.get("cause"), R.ESCALATE["no_response"])
        return {Strategy.REASK: R.REASK, Strategy.RETRY_LISTEN: R.RETRY_LISTEN,
                Strategy.CLARIFY: R.CLARIFY}[strategy]

    @staticmethod
    def _pool(strategy, c) -> list:
        emo = c.get("emotion") if c.get("emotion") in R.EMPATHY_LIGHT else "default"
        topic = c.get("topic")
        if strategy == Strategy.GENTLE_PROBE:
            return R.GENTLE_PROBE
        if strategy == Strategy.FOLLOW_UP:
            return R.FOLLOW_UP.get(topic, R.FOLLOW_UP["default"])
        if strategy == Strategy.EMPATHIZE:
            if c.get("proactive"):
                return R.PROACTIVE.get(emo, R.PROACTIVE["default"])
            if topic in R.TOPIC_EMPATHY:                       # 말한 주제를 짚어서 공감
                return R.TOPIC_EMPATHY[topic]
            return (R.EMPATHY_DEEP if c.get("level") == "deep" else R.EMPATHY_LIGHT)[emo]
        kind = c.get("kind")
        if c.get("fixed_text"):                       # 장기 기억 언급: 엔진이 고른 문장 (회전)
            return [c["fixed_text"]]
        return R.SMALL_TALK.get(kind, R.SMALL_TALK["generic"])

    @staticmethod
    def _pick(pool: list, recent: list) -> str:
        """최근에 쓴 문장은 피해서, 안 쓴 첫 문장을 고른다. 다 썼으면 가장 오래전에 쓴 문장.
        (각 문장이 '마지막으로' 쓰인 위치를 기준으로 비교한다. 첫 위치를 쓰면 같은 문장이 계속 뽑힌다)"""
        fresh = [t for t in pool if t not in recent]
        if fresh:
            return fresh[0]
        last_used = {t: i for i, t in enumerate(recent)}       # 같은 문장은 뒤쪽(최근) 위치로 덮어씀
        return min(pool, key=lambda t: last_used[t])


# ───────────── 안전 확인 세션 ─────────────
@dataclass
class SafetySession:
    phase: str = "WAITING"          # WAITING | ESCALATED
    asked_at: float = 0.0
    ask_count: int = 1
    listen_retries: int = 0
    clarified: bool = False
    timeout: float = 10.0
    variant: str = "fall"           # fall | lying | pain | fell_report (복귀 문장 선택에도 사용)
    cause: str = ""                 # 현재 비상 사유 (no_response | danger | crisis) — 더 심각한 정보가 오면 갱신
    first_cause: str = ""           # 최초 비상 사유 (보존)
    escalated_at: float = 0.0
    last_prompt_at: float = 0.0
    last_alert_at: float = 0.0
    realerts: int = 0               # 시간 경과에 따른 재알림 횟수
    alert_seq: int = 0              # 이번 사건에서 보낸 비상 알림 수 (최초 + 갱신 + 재알림)
    responded: bool = False         # 비상 후 어르신이 말을 했는지 (응답 있음 ≠ 안전 확인)
    assessment: Optional[dict] = None              # 안전 확인을 시작한 상황 판단
    evidence: list = field(default_factory=list)   # 비상 근거 누적 (최초 근거 + 이후 변화)


# ───────────── 대화 엔진 ─────────────
class DialogueEngine:
    def __init__(self, config: Optional[Config] = None, llm=None, memory=None):
        self.cfg = config or Config()
        self.gen = ResponseGenerator(llm)     # llm: 선택적 LLM 보조 자리, 기본 OFF (안전 전략에는 쓰지 않음)
        self.ctx = ConversationContext(self.cfg)
        self.state = State.NORMAL
        self.session: Optional[SafetySession] = None
        # memory: 장기 기억(memory.UserMemory), 기본 OFF. 엔진 하나 = 대화 세션 하나
        self.memory = memory
        self._recall_topic = memory.begin_session() if memory is not None else None   # 이번 세션에 한 번 언급할 지난 화제
        self._recall_turns = 0        # 언급 없이 지나간 일상 턴 수 (RECALL_WINDOW까지)
        self._spoke = False           # 이번 세션에 어르신 발화가 있었는지 (인사 문장은 첫 발화에서만)
        self._just_recalled = None    # 직전 턴에 언급한 화제 (대답 해석용)

    @property
    def history(self):
        return self.ctx.recent_texts()

    def process(self, inp: DialogueInput) -> DialogueOutput:
        """입력이 있을 때마다 호출. 입력이 없어도 1초 간격으로 빈 입력을 넣어 타이머를 진행시킨다."""
        now = inp.timestamp
        text = (inp.stt or "").strip()
        sig = analyze(text)                                   # ① 입력 분석
        emotion_changed = self.ctx.observe(inp)

        if self.state == State.SAFETY_CHECK:                  # ④ 안전 확인 세션 진행
            out = self._safety(inp, sig, now)
        else:
            sit = assess(inp, sig, self.ctx, self.cfg)        # ② 상황 판단
            out = self._general(inp, sig, sit, emotion_changed, now)
            if out.assessment is None:
                out.assessment = sit.to_dict()
        out.warnings = list(inp.warnings)
        out.emergency_active = self.session is not None and self.session.phase == "ESCALATED"

        if text:
            self.ctx.add_turn("user", text, now)
        if out.response_text:
            self.ctx.add_turn("ai", out.response_text, now, out.state.value, out.strategy.value)
        # 장기 기억 갱신: 안전 확인·비상 중이거나 안전 전략 응답이면 기록하지 않는다 (화제·감정 라벨만, 원문 없음)
        if (self.memory is not None and self.state != State.SAFETY_CHECK
                and out.strategy not in SAFETY_STRATEGIES):
            self.memory.observe(self._memory_topics(sig, text), self.ctx.last["emotion"] if emotion_changed else None, now)
        if self.memory is not None:
            if (self.state == State.SAFETY_CHECK or out.strategy in SAFETY_STRATEGIES
                    or out.strategy in RECALL_BLOCKERS):
                self._recall_topic = None         # 안전·위기·공감 뒤에는 이번 세션에서 지난 화제를 꺼내지 않는다
            if text:
                self._spoke = True
        return out

    # ───── ③ 상태 결정 + ⑤ 전략 결정 (NORMAL / EMOTIONAL) ─────
    def _general(self, inp, sig, sit: Situation, emotion_changed, now) -> DialogueOutput:
        if sit.risk_level == RiskLevel.CRITICAL:
            cause = "crisis" if sig.self_harm else "danger"
            return self._escalate("위험 발화 감지: " + " / ".join(sit.evidence), cause, now, sit)
        if sit.risk_level >= RiskLevel.MEDIUM:
            return self._start_safety(now, sit, sig)

        if not sig.empty:
            return self._reply_to_utterance(sig, sit, now)

        # 발화 없음: 부정 감정이 새로 감지됐을 때만 먼저 말을 건다 (너무 자주는 X)
        gap_ok = self.ctx.last_ai_ts is None or now - self.ctx.last_ai_ts >= self.cfg.proactive_gap
        if emotion_changed and sit.valence == "negative" and gap_ok:
            self.ctx.probe_streak = 0
            return self._speak(State.EMOTIONAL, Strategy.EMPATHIZE,
                               {"emotion": self.ctx.last["emotion"], "proactive": True},
                               "표정에서 부정 감정 감지 → 먼저 말 걸기")
        return DialogueOutput(self.state, Strategy.NONE, None, reason="입력 없음")

    def _reply_to_utterance(self, sig: TextSignals, sit: Situation, now: float) -> DialogueOutput:
        c = self.ctx
        topic = sig.topics[0] if sig.topics else None

        if sit.mismatch:   # 표정은 어두운데 "괜찮아" → 정서 흐름(연속 횟수)은 건드리지 않고 조심스럽게 되묻기
            if c.probe_streak >= MAX_GENTLE_PROBES:      # 이미 두 번 되물었으면 더 캐묻지 않고 물러선다
                return self._speak(State.NORMAL, Strategy.SMALL_TALK, {"kind": "back_off"},
                                   f"표정-발화 불일치가 이어짐 (되묻기 {c.probe_streak}회) → 물러서기")
            c.probe_streak += 1
            return self._speak(State.EMOTIONAL, Strategy.GENTLE_PROBE, {},
                               "표정-발화 불일치 → 조심스럽게 되묻기")

        c.probe_streak = 0                                # 다른 응답이 오면 되묻기 연속 횟수 초기화
        prev_topic, c.last_topic = c.last_topic, topic    # 직전 턴과 같은 화제인지 (이어가기 응답용)
        negative = sit.valence == "negative"
        c.register_user_turn(negative, sig.topics)

        if sig.grief:                                     # 고인에 대한 그리움: 일상 응답 대신 그리움을 짚는 공감
            topic = "grief"
        if not negative and not sig.grief:
            happy_face = c.last["emotion"] == "happy" and now - c.emotion_ts <= self.cfg.emotion_ttl
            joy = sig.joy or happy_face             # 기쁨은 화제 응답보다 먼저 함께 기뻐한다
            kind = self._small_talk_kind(sig, sit, topic, joy, prev_topic)
            gen_ctx = {"kind": kind}
            if kind == "time":
                gen_ctx["time_text"] = korean_time(now)   # 입력 시각 기준 (테스트에서 시각 주입 가능)
            elif self.memory is not None:
                gen_ctx = self._with_memory(sig, kind, gen_ctx)
            return self._speak(State.NORMAL, Strategy.SMALL_TALK, gen_ctx,
                               "일상 발화" + (" (기쁨 먼저 받아주기)" if joy else ""))

        notice = None
        if c.negative_streak >= self.cfg.streak_for_notice and not c.notice_sent:
            c.notice_sent = True
            notice = (f"부정적인 감정 표현이 {c.negative_streak}회 연속 이어지고 있어요. "
                      "안부 확인을 권장해요.")

        repeated = c.repeated_topic()
        streak_followup = c.negative_streak - c.followup_at_streak >= self.cfg.streak_for_followup
        if streak_followup or repeated:
            why = f"같은 주제 반복({repeated})" if repeated else f"부정 발화 연속 {c.negative_streak}회"
            out = self._speak(State.EMOTIONAL, Strategy.FOLLOW_UP, {"topic": repeated}, why)
            c.followup_at_streak = c.negative_streak      # 같은 되묻기 반복 방지
            if repeated:
                c.topic_counts[repeated] = 0
        else:
            level = "deep" if sit.emotion_intensity >= 0.6 else "light"
            out = self._speak(State.EMOTIONAL, Strategy.EMPATHIZE,
                              {"emotion": c.last["emotion"], "level": level, "topic": topic},
                              f"부정 감정(강도 {sit.emotion_intensity:.2f}) → 공감 {level}"
                              + (f", 주제 '{topic}' 반영" if topic in R.TOPIC_EMPATHY else ""))
        out.notice = notice
        return out

    @staticmethod
    def _memory_topics(sig: TextSignals, text: str) -> list:
        """기억에 남길 화제: 약 복용 이야기만으로는 건강 화제로 남기지 않는다 (다음 세션 "몸이 불편하다고…" 방지).
        증상·몸 부위·병원 표현이 함께 있으면 그대로 건강으로 기록."""
        if sig.medicine and "health" in sig.topics:
            compact = "".join(text.split())
            if not any(k in compact for k in HEALTH_NOT_MEDICINE):
                return [t for t in sig.topics if t != "health"]
        return sig.topics

    def _with_memory(self, sig: TextSignals, kind: str, gen_ctx: dict) -> dict:
        """장기 기억이 켜졌을 때만: 새 세션 첫 2턴 안에 지난 화제를 한 번 꺼내고, 그 대답을 이어서 받는다."""
        recalled, self._just_recalled = self._just_recalled, None
        if recalled in ("health", "sleep") and kind in RECALL_KINDS | {"goodnight", "relieved"}:
            better = sig.recovered or sig.positive or (sig.ok_explicit and not sig.negative)                 or (recalled == "sleep" and sig.sleep_well)
            if better and not sig.discomfort and not sig.sleep_bad:
                return {"kind": "relieved"}       # "이제 괜찮아/좋아졌어/잘 자" → 다행이에요
        if not self._recall_topic:
            return gen_ctx
        if kind in RECALL_KINDS and self._recall_turns < RECALL_WINDOW:
            topic, self._recall_topic = self._recall_topic, None
            pool = R.RECALL["plain" if self._spoke else "greet"][topic]
            text = pool[self.memory.mention_index() % len(pool)]
            self.memory.mark_mentioned()
            self.ctx.last_topic = topic           # 다음 대답이 같은 화제면 이어가기 응답
            self._just_recalled = topic
            return {"kind": "recall", "fixed_text": text}
        if kind not in INFO_KINDS:
            self._recall_turns += 1
            if self._recall_turns >= RECALL_WINDOW:
                self._recall_topic = None
        return gen_ctx

    @staticmethod
    def _small_talk_kind(sig: TextSignals, sit: Situation, topic, joy: bool = False, prev_topic=None) -> str:
        if sig.greeting: return "greeting"
        if sig.wait: return "wait"
        if sig.past_positive: return "good_day"            # "그래도 오늘은 좋았어": 좋았던 일을 받아준다
        if sig.denies_negative or sig.recovered: return "relieved"
        if sig.thanks: return "thanks"
        if sig.goodnight: return "goodnight"
        if sig.about_ai: return "about_ai"
        if sig.question: return {"time": "time", "weather": "weather_ask", "abilities": "abilities"}[sig.question]
        if joy:                                       # 기쁨 표현·밝은 표정: 화제보다 먼저 기쁨을 받아준다
            return {"family": "joy_family", "health": "joy_health"}.get(topic, "joy")
        if sig.boredom: return "boredom"
        if sig.medicine: return "medicine"                  # 약 복용은 식사 응답보다 먼저 (식사로 되묻지 않음)
        if sig.meal_state: return "meal_" + sig.meal_state   # 먹었다는 말을 다시 묻지 않고, 안 먹었으면 가볍게 제안
        if sig.meal: return "meal"
        if sig.discomfort: return "health_ack"              # 이미 말한 불편감은 되묻지 않고 받아준다
        if sig.sleep_bad: return "sleep_bad"                # 잠을 못 잤다는 말은 다시 묻지 않고 받아준다
        if sig.tired: return "tired"                        # 피곤하다는 말은 쉬도록 받아준다
        if topic in R.SMALL_TALK:
            if topic == prev_topic:                         # 직전 턴과 같은 화제: 처음 꺼내는 말 대신 이어가기
                return "continue_family" if topic == "family" else "continue"
            return topic
        # '긍정' 응답은 실제 발화가 긍정일 때만 (오래된 밝은 표정은 sit.valence에 남아 있어도 쓰지 않는다.
        # 60초 안의 밝은 표정은 위 joy 분기에서 이미 처리)
        if sig.positive: return "positive"
        return "generic"

    # ───── ④ 안전 확인 세션 ─────
    def _start_safety(self, now, sit: Situation, sig: TextSignals, why: str = "안전 확인 시작") -> DialogueOutput:
        if sit.trigger == "utterance":
            variant = "pain" if sig.distress == "mild" else "fell_report"
        else:
            variant = sit.trigger or "fall"
        timeout = self.cfg.response_timeout
        if sit.risk_level >= RiskLevel.HIGH:
            timeout *= self.cfg.high_risk_timeout_ratio     # 위험도 높으면 기다리는 시간 단축
        self.state = State.SAFETY_CHECK
        self.session = SafetySession(asked_at=now, timeout=timeout, variant=variant, assessment=sit.to_dict())
        self.ctx.consume_events(now)
        out = self._speak(State.SAFETY_CHECK, Strategy.ASK_CHECK, {"variant": variant},
                          f"{why}: " + " / ".join(sit.evidence))
        out.assessment = sit.to_dict()
        out.response_deadline_sec = timeout
        return out

    def _safety(self, inp, sig: TextSignals, now) -> DialogueOutput:
        s = self.session
        if s.phase == "ESCALATED":
            return self._after_escalation(inp, sig, now)

        # 확인 중에 들어온 새 센서 근거를 다시 평가: 다른 원인의 낙상이 생겼거나 위험도가 올라가면
        # 이전 질문에 대한 대답으로 넘어가지 않고 그 근거로 다시 확인한다 (같은 입력의 대답도 반영하지 않음)
        sensed = assess(inp, NO_TEXT, self.ctx, self.cfg)
        start = (s.assessment or {}).get("risk_score", 0)
        if sensed.risk_level >= RiskLevel.MEDIUM and (
                (sensed.trigger and sensed.trigger != s.variant) or sensed.risk_score > start):
            prev = (s.assessment or {}).get("evidence", [])
            sensed.evidence = [f"이전 확인 근거: {x}" for x in prev] + sensed.evidence
            return self._start_safety(now, sensed, NO_TEXT, "확인 중 새 위험 근거 → 다시 확인")

        if not sig.empty:
            s.responded = True             # 한 번이라도 말했으면 '무응답'이 아니다 (응답 있음 ≠ 안전 확인)
        if sig.self_harm:
            return self._escalate(f"확인 중 극단적 선택 암시 발화: '{inp.stt.strip()}'", "crisis", now)
        if sig.distress != "none":
            return self._escalate(f"확인 중 위험 응답: '{inp.stt.strip()}'", "danger", now)
        if sig.ok:
            self._end_safety(now)
            return self._speak(State.NORMAL, Strategy.RESUME, {"variant": s.variant}, "응답 있음: 정상 확인")
        if not sig.empty:
            if s.clarified:
                self._end_safety(now)
                return self._speak(State.NORMAL, Strategy.RESUME, {"variant": s.variant},
                                   "응답 있음: 대화 가능 상태")
            s.clarified, s.asked_at = True, now
            return self._waiting(self._speak(State.SAFETY_CHECK, Strategy.CLARIFY, {}, "모호한 응답 → 구체 확인"))

        # 텍스트 없음 (speech / voice_unclear: 목소리는 감지됐지만 STT 결과가 없음)
        if inp.sound_event in SPEECH_EVENTS and s.listen_retries < self.cfg.max_listen_retries:
            s.listen_retries += 1
            s.asked_at = now
            return self._waiting(self._speak(State.SAFETY_CHECK, Strategy.RETRY_LISTEN, {},
                                             "음성은 감지됐으나 인식 실패 → 다시 듣기 (무응답 아님)"))
        if now - s.asked_at < s.timeout:
            return DialogueOutput(State.SAFETY_CHECK, Strategy.NONE, None, reason="응답 대기 중")

        if s.ask_count < self.cfg.max_asks:                  # 시간 초과 → 재질문
            s.ask_count += 1
            s.asked_at = now
            return self._waiting(self._speak(State.SAFETY_CHECK, Strategy.REASK, {}, "무응답 → 재질문"))
        motion = self.ctx.motion(now)      # 유효시간 안에 실제로 받은 움직임만 해제 근거로 쓴다
        # 움직임은 '일어났다'는 뜻이 아니다: 바닥 자세가 오래 이어져 다시 확인한 경우엔 움직임만으로 해제하지 않는다
        if motion == "moving" and s.variant != "fall_persist":
            self._end_safety(now)
            return self._speak(State.NORMAL, Strategy.RESUME, {"variant": "moving_noreply"},
                               "무응답이나 움직임 확인됨 (일단 정상 처리, 모니터링 계속)")
        what = ("움직임은 있으나 바닥 자세 지속" if motion == "moving"
                else "움직임 없음" if motion else "움직임 정보 없음")
        head = "응답은 있었으나 안전 미확인 → 추가 질문에 무응답" if s.responded else "무응답"
        return self._escalate(f"{head} + {what} (motion={motion or '정보 없음'}, "
                              f"pose={self.ctx.last['pose']})", "no_response", now)

    # ───── 헬퍼 ─────
    def _waiting(self, out: DialogueOutput) -> DialogueOutput:
        """이번 질의 후 응답 대기 시간을 출력에 싣는다 (UI 카운트다운이 엔진 대기 시간과 맞도록)."""
        out.response_deadline_sec = self.session.timeout
        return out

    def _speak(self, state, strategy, gen_ctx, reason) -> DialogueOutput:
        self.state = state
        gen_ctx = {**gen_ctx, "history": self.ctx.recent_texts(), "recent_ai": self.ctx.recent_ai()}
        return DialogueOutput(state, strategy, self.gen.generate(strategy, gen_ctx), reason=reason)

    # ───── 비상 이후: 응답 있음 ≠ 안전 확인 ─────
    def _after_escalation(self, inp, sig: TextSignals, now) -> DialogueOutput:
        s = self.session
        if not sig.empty:
            s.responded, s.last_prompt_at = True, now           # 말을 했으므로 더 이상 '무응답'은 아니다
            text = inp.stt.strip()
            new = "crisis" if sig.self_harm else "danger" if sig.distress != "none" else None
            if new and CAUSE_RANK[new] > CAUSE_RANK.get(s.cause, 0):
                old, s.cause = s.cause, new                     # 더 심각한 정보 → 사유 갱신 + 즉시 알림
                s.evidence.append(f"비상 후 어르신 발화: '{text}' ({CAUSE_NOTE[new]})")
                return self._alert("updated", new, now, f"비상 중 더 위급한 발화 → 사유 갱신({old} → {new})")
            if new:                                             # 같은 수준의 위험: 이미 알렸으므로 안심시키기
                return self._speak(State.SAFETY_CHECK, Strategy.REASSURE, {"cause": s.cause},
                                   "비상 중 위험 발화 지속 → 비상 유지, 안심시키기")
            if sig.ok_explicit:                                 # "괜찮아" 같은 명확한 안전 응답만 해제
                cause, origin = s.cause, s.first_cause
                self._end_safety(now)
                variant = "after_crisis" if cause == "crisis" else "after_escalation"
                out = self._speak(State.NORMAL, Strategy.RESUME, {"variant": variant},
                                  "비상 후 명확한 안전 응답 → 비상 해제 (보호자에게 해제 알림)")
                out.alert_status, out.alert_cause, out.alert_origin, out.user_responded = \
                    "resolved", cause, origin, True
                return out
            if sig.ok and s.cause != "crisis":                 # "응/네"만으로는 확정하지 않고 되묻기
                return self._speak(State.SAFETY_CHECK, Strategy.CLARIFY, {},
                                   "비상 후 짧은 긍정 응답 → 안전 확인 전, 구체적으로 되묻기")
            return self._speak(State.SAFETY_CHECK, Strategy.REASSURE, {"cause": s.cause},
                               "비상 후 사용자 발화 (안전 미확인) → 비상 유지, 안심시키기")

        if s.realerts < self.cfg.max_realerts and now - s.last_alert_at >= self.cfg.realert_after:
            s.realerts += 1
            why = "응답은 있었으나 안전 미확인" if s.responded else f"{int(now - s.escalated_at)}초간 응답 없음"
            return self._alert("realert", "realert_responded" if s.responded else "realert", now,
                               f"비상 후 {why} → 재알림({s.realerts}/{self.cfg.max_realerts})")
        if now - s.last_prompt_at >= self.cfg.escalated_prompt_gap:
            s.last_prompt_at = now
            return self._speak(State.SAFETY_CHECK, Strategy.REASSURE, {"cause": s.cause},
                               "비상 후 주기적 안심 멘트")
        return DialogueOutput(State.SAFETY_CHECK, Strategy.NONE, None, reason="비상 알림 전송됨, 대기")

    def _escalate(self, reason, cause, now, sit: Optional[Situation] = None) -> DialogueOutput:
        self.state = State.SAFETY_CHECK
        if self.session is None:
            self.session = SafetySession()
        s = self.session
        if sit:
            s.assessment, s.evidence = sit.to_dict(), list(sit.evidence)
        else:                                       # 확인 중 비상: 확인을 시작한 근거 + 비상이 된 이유
            s.evidence = list((s.assessment or {}).get("evidence", [])) + [reason]
        s.phase, s.cause, s.first_cause, s.escalated_at = "ESCALATED", cause, cause, now
        s.last_prompt_at, s.realerts, s.alert_seq = now, 0, 0     # responded는 확인 단계 값을 이어받는다
        key = "no_response_after_reply" if cause == "no_response" and s.responded else cause
        return self._alert("raised", key, now, reason)

    def _alert(self, status, text_key, now, reason) -> DialogueOutput:
        """비상 알림 한 건 (최초/갱신/재알림). 알림 순번을 올리고 누적 근거를 함께 내보낸다."""
        s = self.session
        s.alert_seq += 1
        s.last_alert_at = s.last_prompt_at = now
        assessment = {**(s.assessment or {}), "evidence": list(s.evidence)}
        return DialogueOutput(State.SAFETY_CHECK, Strategy.ESCALATE,
                              self.gen.generate(Strategy.ESCALATE, {"cause": text_key}),
                              emergency=True, alert_seq=s.alert_seq, alert_cause=s.cause,
                              alert_status=status, alert_origin=s.first_cause,
                              user_responded=s.responded, reason=reason, assessment=assessment)

    def _end_safety(self, now):
        self.state, self.session = State.NORMAL, None
        self.ctx.consume_events(now)