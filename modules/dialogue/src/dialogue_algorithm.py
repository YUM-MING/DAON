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

import dataclasses
import datetime
import itertools
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import responses as R
from context import ConversationContext
from dialogue_types import (SPEECH_EVENTS, Config, DialogueInput, DialogueOutput, RiskLevel,
                            State, Strategy)
from situation import Situation, assess
from llm_client import client_from_env                                       # noqa: E402
from llm_chat import (EMOTION_KO, STT_REASK_ALLOWED, LLMRunner, build_prompt,  # noqa: E402
                      check_text as check_llm_text, clean as clean_llm_text, fix_honorifics,
                      intent_for as llm_intent_for, is_user_question, semantic_issue, trim as trim_llm_text)
from text_analyzer import FIRE_KINDS, TextSignals, analyze

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
INFO_KINDS = {"time", "weather_ask", "abilities", "about_ai", "meal", "meal_done", "meal_not", "meal_ask", "medicine",
              "medicine_missed", "medicine_ask", "medicine_refuse", "date", "date_unknown"}
KST = datetime.timezone(datetime.timedelta(hours=9))   # 한국 시간 (서머타임 없음). Windows 기본 환경엔 tz 데이터가 없어 고정 오프셋
WEEKDAYS_KO = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")
EPOCH_PLAUSIBLE = 1_500_000_000   # 이보다 작은 timestamp는 실제 날짜가 아니다 (테스트·독립 실행의 경과 시간 등)
# 이 응답이 나오면 그 세션에서는 지난 화제를 꺼내지 않는다 (안전 전략은 SAFETY_STRATEGIES로 따로 확인)
HEALTH_NOT_MEDICINE = ("아프", "아파", "허리", "무릎", "다리", "쑤시", "쑤셔", "저려", "병원")   # 약 이야기여도 건강으로 기억
RECALL_BLOCKERS = {Strategy.EMPATHIZE, Strategy.FOLLOW_UP, Strategy.GENTLE_PROBE}


def _fill(template: str, slots: dict) -> str:
    for k, v in slots.items():
        template = template.replace("{" + k + "}", v)
    return template


def _josa(word: str, batchim: str, plain: str) -> str:
    """받침 유무에 맞는 조사 ('머리' + 가, '무릎' + 이)"""
    code = ord(word[-1]) - 0xAC00
    return word + (batchim if 0 <= code < 11172 and code % 28 else plain)


def spoken_said(part: Optional[str], said: str) -> str:
    """로봇이 증상을 되돌려 말할 때의 표현. '가슴이 아프다고'는 그대로 말하면 로봇 목소리가 다시 인식될 때
    가슴 통증(즉시 비상)으로 판정되므로 '가슴이 불편하다고'로 바꿔 말한다."""
    return "불편하다고" if part == "가슴" and said == "아프다고" else said


def korean_date(ts: float) -> Optional[str]:
    """입력 시각(Unix epoch 초) → '10월 22일 목요일' (Asia/Seoul). 실제 날짜로 볼 수 없는 값이면 None"""
    if ts is None or ts < EPOCH_PLAUSIBLE:
        return None
    d = datetime.datetime.fromtimestamp(ts, KST)
    return f"{d.month}월 {d.day}일 {WEEKDAYS_KO[d.weekday()]}"


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
            if c.get("hazard_hold"):                  # 환경 위험 비상 중 "괜찮아"·"대피했어": 비상 유지 (안전하다고 단정하지 않음)
                return R.HAZARD_HOLD[c["hazard_hold"]]
            if c.get("hazard"):                       # 환경 위험 비상 중: 짧은 대피 확인 (긴 안내를 매번 반복하지 않음)
                return self._pick(R.REASSURE_HAZARD[c["hazard"]], c.get("recent_ai", []))
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
        pool = self._pool(strategy, c)
        if c.get("slots"):                            # 빈칸을 먼저 채워야 최근 문장 비교(반복 피하기)가 맞다
            pool = [_fill(t, c["slots"]) for t in pool]
        text = self._pick(pool, c.get("recent_ai", []))
        if "{time}" in text:                          # 시간 질문 응답: 현재 시각 채우기
            text = text.replace("{time}", c.get("time_text", ""))
        if "{date}" in text:                          # 날짜 질문 응답: 오늘 날짜·요일 채우기
            text = text.replace("{date}", c.get("date_text", ""))
        return text

    @staticmethod
    def _safety_text(strategy, c) -> str:
        v = c.get("variant")
        if strategy == Strategy.ASK_CHECK:
            return _fill(R.ASK_CHECK.get(v, R.ASK_CHECK["fall"]), {"said": c.get("said", "")})
        if strategy == Strategy.RESUME:
            return R.RESUME.get(v, R.RESUME["default"])
        if strategy == Strategy.ESCALATE:
            return R.ESCALATE.get(c.get("cause"), R.ESCALATE["no_response"])
        if strategy == Strategy.CLARIFY and v == "pain":
            return R.CLARIFY_PAIN
        if strategy == Strategy.CLARIFY and v == "recall":
            return _fill(R.CLARIFY_RECALL, {"said": c.get("said", "")})
        if strategy == Strategy.CLARIFY and v == "recall_none":
            return R.CLARIFY_RECALL_NONE
        if strategy == Strategy.CLARIFY and v == "rescue_like":
            return R.CLARIFY_RESCUE_LIKE
        if strategy == Strategy.REASK and v == "sound_light":
            return R.REASK_LIGHT
        return {Strategy.REASK: R.REASK, Strategy.RETRY_LISTEN: R.RETRY_LISTEN,
                Strategy.CLARIFY: R.CLARIFY}[strategy]

    @staticmethod
    def _pool(strategy, c) -> list:
        if c.get("kind") in R.SYMPTOM_TALK:           # 이번 대화의 증상 (일상 통증·회상 질문·다시 묻기)
            return R.SYMPTOM_TALK[c["kind"]]
        if c.get("kind") in R.STT_TALK:               # STT 오류 다시 묻기·부르기·마이크 확인·다시 말하기
            return R.STT_TALK[c["kind"]]
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
    pain_probed: bool = False       # 확인 중 가벼운 통증 설명에 "얼마나 아프세요?"로 한 번 더 물었는지
    timeout: float = 10.0
    variant: str = "fall"           # fall | lying | pain | fell_report (복귀 문장 선택에도 사용)
    cause: str = ""                 # 현재 비상 사유 (no_response | danger | crisis) — 더 심각한 정보가 오면 갱신
    first_cause: str = ""           # 최초 비상 사유 (보존)
    escalated_at: float = 0.0
    last_prompt_at: float = 0.0
    last_alert_at: float = 0.0
    realerts: int = 0               # 시간 경과에 따른 재알림 횟수
    alert_seq: int = 0              # 이번 사건에서 보낸 비상 알림 수 (최초 + 갱신 + 재알림)
    incident: int = 0               # 비상 사건 번호 (_escalate에서 부여, 0=아직 비상 아님)
    hazards: list = field(default_factory=list)   # 이번 사건에서 들은 환경 위험 (gas·fire·smoke·burning), 추가만 됨
    responded: bool = False         # 비상 후 어르신이 말을 했는지 (응답 있음 ≠ 안전 확인)
    started_at: float = 0.0         # 이번 확인 질문을 시작한 시각 (따로 도착한 근거로 질문을 반복하지 않는 창)
    sound_only: bool = False        # 낙상음만으로 시작한 확인 (D)
    sound_ts: Optional[float] = None    # 그 낙상음 시각
    light: bool = False             # 가벼운 확인: 소리 단독 + 소리 전후 정상 자세 관측 (무응답이어도 정상 자세가 계속 보이면 알림 없음)
    danger_origin: bool = False     # 위험 증상(어지럼·가슴·숨 등)으로 시작한 확인 (호전 표현만으로 풀지 않음)
    assessment: Optional[dict] = None              # 안전 확인을 시작한 상황 판단
    evidence: list = field(default_factory=list)   # 비상 근거 누적 (최초 근거 + 이후 변화)


# ───────────── 대화 엔진 ─────────────
class DialogueEngine:
    def __init__(self, config: Optional[Config] = None, llm=None, memory=None):
        self.cfg = config or Config()
        # LLM: 인자가 없으면 환경변수(DIALOGUE_LLM 등)로만 켠다 (없으면 None → 템플릿만, 지금과 동일).
        # generate(system, messages, timeout)를 가진 클라이언트는 비동기 경로(허용 표·출력 검증·요청 번호·무효화)로만 쓰고,
        # 예전 콜백(llm(dict) -> str)은 기존 동기 훅 그대로 둔다 (기존 테스트 호환)
        if llm is None:
            llm = client_from_env()
        if llm is not None and hasattr(llm, "generate"):
            self._llm = LLMRunner(llm, getattr(llm, "timeout", 1.2), self.cfg.llm_breaker_failures,
                                  self.cfg.llm_breaker_secs)
            llm = None
        else:
            self._llm = None
        self.gen = ResponseGenerator(llm)     # 예전 동기 콜백 자리 (기본 OFF, 안전 전략에는 쓰지 않음)
        self._last_gen = None                 # 마지막 _speak의 (출력, 전략, 생성 문맥): LLM 허용 여부 판단용
        self._resume_hint = None              # 이번 발화가 STT 재질문 뒤의 발화면 직전 화제 (LLM 문맥용)
        self.ctx = ConversationContext(self.cfg)
        self.state = State.NORMAL
        self.session: Optional[SafetySession] = None
        # memory: 장기 기억(memory.UserMemory), 기본 OFF. 엔진 하나 = 대화 세션 하나
        self.memory = memory
        self._recall_topic = memory.begin_session() if memory is not None else None   # 이번 세션에 한 번 언급할 지난 화제
        self._recall_turns = 0        # 언급 없이 지나간 일상 턴 수 (RECALL_WINDOW까지)
        self._spoke = False           # 이번 세션에 어르신 발화가 있었는지 (인사 문장은 첫 발화에서만)
        self._just_recalled = None    # 직전 턴에 언급한 화제 (대답 해석용)
        # 비상 사건 번호 (엔진마다 1부터, 같은 입력이면 같은 출력). 알림 중복 방지가 사건 단위로 동작하게 해서,
        # 외부에서 _end_safety()로 해제한 뒤의 새 비상이 같은 원인·순번이어도 막히지 않게 한다.
        self._incidents = itertools.count(1)
        self._internal_end = False    # 엔진 안에서 해제하는 중 (서버가 직접 부른 _end_safety = 보호자 해제와 구분)

    @property
    def history(self):
        return self.ctx.recent_texts()

    def process(self, inp: DialogueInput) -> DialogueOutput:
        """입력이 있을 때마다 호출. 입력이 없어도 1초 간격으로 빈 입력을 넣어 타이머를 진행시킨다."""
        now = inp.timestamp
        text = (inp.stt or "").strip()
        sig = analyze(text)                                   # ① 입력 분석
        self._resume_hint = None
        self._state_before = self.state                       # LLM 대기 중에는 상태 전환(NORMAL↔EMOTIONAL)을 출력 시점으로 미룬다
        emotion_changed = self.ctx.observe(inp)
        self._pain_q = None                                   # 직전 "견딜 만하세요?" 질문 (이번 발화가 그 대답인지)
        if text:
            self.ctx.user_turns += 1
            self._pain_q, self.ctx.pain_question = self.ctx.pain_question, None
            urgent_q, self.ctx.urgent_prompt = self.ctx.urgent_prompt, None
            self._note_symptoms(sig, now)
            if not sig.recall_question:                       # 회상 질문에 인용된 증상은 새 기록이 아니다
                self.ctx.note_dangers(sig.danger_kinds, now)
            self._track_unclear(sig)
            if sig.rescue_like and self.ctx.recent_fall_evidence(now):
                # 낙상 근거(15초 이내 또는 바닥 자세 지속)와 함께 들린 "알려줘"·"달려줘"는 구조 요청일 수 있다 → 안전 우선
                sig.distress = "severe"
            asked = (self.session is not None and self.session.phase == "WAITING") or (
                urgent_q is not None and 0 <= now - urgent_q <= self.cfg.pain_question_secs)
            if sig.urgent and asked and not sig.ok_explicit and sig.distress != "severe":
                # "급하시면 바로 말씀해 주세요"(로봇은 '도와줘'를 직접 말하지 않는다)에 대한 "급해", 또는 안전 확인 중 "급해"
                sig.distress, sig.urgent_answer = "severe", True

        if self.state == State.SAFETY_CHECK:                  # ④ 안전 확인 세션 진행
            out = self._safety(inp, sig, now)
        else:
            sit = assess(inp, sig, self.ctx, self.cfg)        # ② 상황 판단
            out = self._general(inp, sig, sit, emotion_changed, now)
            if out.assessment is None:
                out.assessment = sit.to_dict()
        out.warnings = list(inp.warnings)
        out.emergency_active = self.session is not None and self.session.phase == "ESCALATED"
        if self.session is not None and self.session.light and self.session.phase == "WAITING":
            out.check_level = "light"

        if text:
            self.ctx.add_turn("user", text, now)
        mem_out = out                         # 장기 기억 갱신은 요청 시점의 원래 출력 기준 (LLM 대기와 무관)
        if self._llm is not None:
            out = self._llm_step(out, text, now)
        if text and not sig.stt_unclear:
            self.ctx.resume_topic = None      # 다시 알아들었으면 돌아갈 화제는 이번 응답에서 썼다
        if out.response_text:                 # 로봇 문장 기록은 실제로 출력하는 시점에만 (대기·폐기된 문장은 남기지 않음)
            self.ctx.add_turn("ai", out.response_text, now, out.state.value, out.strategy.value)
        # 장기 기억 갱신: 안전 확인·비상 중이거나 안전 전략 응답이면 기록하지 않는다 (화제·감정 라벨만, 원문 없음)
        if (self.memory is not None and self.state != State.SAFETY_CHECK
                and mem_out.strategy not in SAFETY_STRATEGIES):
            self.memory.observe(self._memory_topics(sig, text), self.ctx.last["emotion"] if emotion_changed else None, now)
        if self.memory is not None:
            if (self.state == State.SAFETY_CHECK or mem_out.strategy in SAFETY_STRATEGIES
                    or mem_out.strategy in RECALL_BLOCKERS):
                self._recall_topic = None         # 안전·위기·공감 뒤에는 이번 세션에서 지난 화제를 꺼내지 않는다
            if text:
                self._spoke = True
        return out

    # ───── ③ 상태 결정 + ⑤ 전략 결정 (NORMAL / EMOTIONAL) ─────
    def _general(self, inp, sig, sit: Situation, emotion_changed, now) -> DialogueOutput:
        if sit.risk_level == RiskLevel.CRITICAL:
            cause = "crisis" if sig.self_harm else "danger"
            if sig.rescue_like:
                sit.evidence.append(f"낙상 근거 직후 구조 요청과 비슷한 말('{sig.rescue_word}') → 구조 요청으로 봄")
            if getattr(sig, "urgent_answer", False):
                sit.evidence.append("'급하시면 말씀해 주세요' 뒤의 급하다는 대답 → 구조 요청으로 봄")
            return self._escalate("위험 발화 감지: " + " / ".join(sit.evidence), cause, now, sit,
                                  text_key=self._escalate_text_key(sig), hazards=self._current_hazards(sig))
        if sit.risk_level >= RiskLevel.MEDIUM:
            if sit.sound_only and sit.sound:
                return self._sound_only_check(now, sit, sig)
            repeat = sig.pain_repeat and sit.trigger == "utterance" and not sig.reports_fall
            return self._start_safety(now, sit, sig, variant="pain_check" if repeat else None)
        recent = self._recent_symptom_said(now) if sig.worsening else None
        if recent:
            # 최근(symptom_repeat_secs 안) 증상 기록이 있는데 지금 더 나빠졌다고 함: 재호소에 준해 안전 확인 (즉시 비상 아님)
            sit.evidence.append(f"최근 증상 기록({recent}) 뒤 현재 악화 호소 → 재호소에 준해 안전 확인")
            return self._start_safety(now, sit, sig, "증상 악화 호소 → 안전 확인", variant="pain_check")

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

        if sig.stt_unclear:                               # 의미 없는 인식 결과: 공감·일상 응답으로 넘기지 않고 다시 묻는다
            return self._unclear_reply(sig)
        self._resume_hint = c.resume_topic                # 재질문 뒤 다시 알아들은 발화면 직전 화제
        if sig.hazard_level == "uncertain":               # "어제 가스 냄새가 났어": 지금 위험한지 확인 (일반 대화보다 먼저)
            sit.evidence.append(f"환경 위험 언급, 지금 위험한지 불확실 ({', '.join(sig.hazard_kinds)})")
            return self._start_safety(now, sit, sig, "환경 위험 불확실 → 안전 확인", variant="hazard_check")
        if sig.scream or sig.moan:                        # 비명·신음 단독("아악!", "윽"): 즉시 비상이 아니라 기존 안전 확인
            sit.evidence.append(("비명" if sig.scream else "신음") + " 단독 발화 (다른 말·위험 표현 없음)")
            recent = [R.DANGER_SAID[d.kind] + (" (이후 괜찮다고 하심)" if d.resolved else "") for d in c.dangers
                      if d.kind in R.DANGER_SAID and 0 <= now - d.ts <= self.cfg.symptom_repeat_secs]
            if recent:
                sit.evidence.append("최근 위험 증상 기록: " + ", ".join(recent) + " (문맥으로만 기록, 즉시 비상 아님)")
            if self.ctx.recent_fall_evidence(now):
                sit.evidence.append("최근 낙상 근거 있음 (기존 낙상 판단 기준 그대로, 즉시 비상 아님)")
            return self._start_safety(now, sit, sig, ("비명" if sig.scream else "신음") + " 단독 발화 → 안전 확인",
                                      variant="help")
        answered = self._pain_answer(sig, sit, now)       # "견딜 만하세요?"에 대한 대답 (표정 불일치보다 먼저)
        if answered:
            return answered
        if sig.small_intent:                              # "야", "내 목소리 들려?", "뭐라고?"
            return self._small_intent_reply(sig)
        if sig.rescue_unsure:                             # 인용·가정·대상 없는 "구해줘" → 즉시 비상 대신 안전 확인
            sit.evidence.append("'구해줘'가 인용·가정이거나 대상 없이 들림 → 지금 도움이 필요한지 확인")
            return self._start_safety(now, sit, sig, "구조 요청인지 불확실 → 안전 확인", variant="help")
        if sig.call_help == "hint":                       # "119"·"구급차"만 들림 → 도움이 필요한지 안전 확인
            sit.evidence.append("'119'·'구급차'만 들림 → 도움이 필요한지 확인")
            return self._start_safety(now, sit, sig, "도움 요청 낱말 → 안전 확인", variant="help")
        if sig.rescue_like:                               # "알려줘": 무엇을 원하는지 묻고, 급하면 바로 말하도록 안내
            stem = next((k for k in R.RESCUE_LIKE_ASK if sig.rescue_word.startswith(k)), None)
            ask = R.RESCUE_LIKE_ASK.get(stem, R.RESCUE_LIKE_DEFAULT)
            c.urgent_prompt = now                         # 다음 발화가 "급해"면 구조 요청으로 본다
            return self._speak(State.NORMAL, Strategy.SMALL_TALK, {"kind": "rescue_like", "slots": {"ask": ask}},
                               f"구조 요청과 소리만 비슷한 말('{sig.rescue_word}'), 낙상 근거 없음 → 되묻기 (비상 아님)")

        if sit.mismatch and not sig.recall_question:   # 표정은 어두운데 "괜찮아" → 정서 흐름(연속 횟수)은 건드리지 않고 조심스럽게 되묻기
            if c.probe_streak >= MAX_GENTLE_PROBES:      # 이미 두 번 되물었으면 더 캐묻지 않고 물러선다
                return self._speak(State.NORMAL, Strategy.SMALL_TALK, {"kind": "back_off"},
                                   f"표정-발화 불일치가 이어짐 (되묻기 {c.probe_streak}회) → 물러서기")
            c.probe_streak += 1
            return self._speak(State.EMOTIONAL, Strategy.GENTLE_PROBE, {},
                               "표정-발화 불일치 → 조심스럽게 되묻기")

        c.probe_streak = 0                                # 다른 응답이 오면 되묻기 연속 횟수 초기화
        prev_topic, c.last_topic = c.last_topic, topic    # 직전 턴과 같은 화제인지 (이어가기 응답용)
        c.prev_topic = prev_topic
        negative = sit.valence == "negative"
        c.register_user_turn(negative, sig.topics)

        if sig.recall_question and sig.danger_kinds:     # "아까 가슴이 아프다고 했지?": 위험이 무시되지 않게 먼저 판단
            out = self._recall_danger(sig, sit, now)
            if out is not None:
                return out
        if sig.recall_question and (sig.recall_symptom or sig.danger_kinds):   # 이번 대화에서 들은 증상만 말한다
            said = self._recalled_said()
            c.mark_symptoms_followed()
            if not said:
                return self._speak(State.NORMAL, Strategy.SMALL_TALK, {"kind": "recall_none"},
                                   "증상 회상 질문 (이번 대화에서 들은 증상 없음 → 지어내지 않음)")
            return self._speak(State.NORMAL, Strategy.SMALL_TALK, {"kind": "recall_symptom", "slots": {"said": said}},
                               "증상 회상 질문 → 이번 대화에서 들은 증상으로 대답")
        if sit.soft_pain:                                 # 위험 신호 없는 일상 통증: 공감하고 상태를 묻는다
            part = sig.symptom_parts[0] if sig.symptom_parts else None
            if part is None:
                return self._speak(State.EMOTIONAL, Strategy.EMPATHIZE, {"kind": "pain_soft_nopart"},
                                   "일상 통증 호소 → 공감·어디가 아픈지 묻기 (안전 확인 안 함)")
            c.pain_question = (c.symptoms[-1], now)
            return self._speak(State.EMOTIONAL, Strategy.EMPATHIZE,
                               {"kind": "pain_soft", "slots": {"part_subj": _josa(part, "이", "가")}},
                               "일상 통증 호소 → 공감·견딜 만한지 묻기 (안전 확인 안 함)")

        if sig.grief:                                     # 고인에 대한 그리움: 일상 응답 대신 그리움을 짚는 공감
            topic = "grief"
        if not negative and not sig.grief:
            happy_face = c.last["emotion"] == "happy" and now - c.emotion_ts <= self.cfg.emotion_ttl
            joy = sig.joy or happy_face             # 기쁨은 화제 응답보다 먼저 함께 기뻐한다
            kind = self._small_talk_kind(sig, sit, topic, joy, prev_topic)
            gen_ctx = {"kind": kind}
            if kind == "time":
                gen_ctx["time_text"] = korean_time(now)   # 입력 시각 기준 (테스트에서 시각 주입 가능)
            elif kind == "date":
                date = korean_date(now)                   # 서버는 Unix epoch 초를 넣는다. 경과 시간이면 날짜를 지어내지 않음
                gen_ctx = {"kind": "date", "date_text": date} if date else {"kind": "date_unknown"}
            elif self.memory is not None:
                gen_ctx = self._with_memory(sig, kind, gen_ctx)
            if kind in ("relieved", "improving"):          # "이제 괜찮아/좋아졌어/덜 아파": 증상을 다시 묻지 않는다
                c.mark_symptoms_followed()
                if sig.recovered:                         # "괜찮아졌어/나아졌어"는 위험 증상이 풀렸다는 분명한 말
                    c.resolve_dangers()
            elif kind in R.CHECKIN_KINDS and "fixed_text" not in gen_ctx:
                gen_ctx = self._with_checkin(kind, gen_ctx, now)
            if gen_ctx.get("kind") == "generic" and self._resume_hint in R.TOPIC_NAME:
                # STT 재질문 뒤 다시 알아들었는데 특별한 화제가 없는 말: 재질문 전에 하던 화제로 돌아간다
                gen_ctx = {"kind": "resume_topic", "slots": {"topic_name": R.TOPIC_NAME[self._resume_hint]}}
                c.last_topic = self._resume_hint          # 돌아간 화제가 다시 지금 화제
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

    # ───── 이번 대화의 증상 기억 (세션 단위, 장기 기억과 별개) ─────
    def _note_symptoms(self, sig: TextSignals, now: float) -> None:
        """증상 발화를 기억한다. 같은 증상을 10분 안에 다시 말했거나 '했잖아'로 재호소하면 일상 통증에서 뺀다."""
        if not sig.symptom_said:
            return
        if sig.distress == "mild" and (sig.insist or self.ctx.symptom_repeated(sig.symptom_parts, now)):
            sig.pain_soft, sig.pain_repeat = False, True
        self.ctx.note_symptom(sig.symptom_parts, sig.symptom_said, now,
                              followed=self.state == State.SAFETY_CHECK)   # 확인 중에 들은 증상은 나중에 다시 묻지 않음

    def _recent_symptom_said(self, now: float) -> Optional[str]:
        """이번 발화 전에 기록된 증상·위험 증상 중 symptom_repeat_secs 안의 것 (symptom_repeated와 같은 시각 비교).
        회상 질문은 기록을 남기지 않으므로 여기 쓰이지 않는다"""
        c = self.ctx
        win = self.cfg.symptom_repeat_secs
        notes = [s for s in c.symptoms if s.turn < c.user_turns and 0 <= now - s.ts <= win]
        if notes:
            s = notes[-1]
            return f"{s.part} {s.said}" if s.part else s.said
        dangers = [d for d in c.dangers if d.ts < now and 0 <= now - d.ts <= win and d.kind in R.DANGER_SAID]
        return R.DANGER_SAID[dangers[-1].kind] if dangers else None

    def _recalled_said(self) -> str:
        """이번 대화에서 들은 증상을 말로: '머리가 아프다고' / '머리가 아프다고 하셨고, 무릎도 쑤신다고' (최근 두 부위)"""
        picked = []
        with_part = {s.said for s in self.ctx.symptoms if s.part is not None}
        for s in reversed(self.ctx.symptoms):
            if s.part is None and s.said in with_part:
                continue                   # "머리가 아프다고"가 있으면 부위 없는 "아프다고"는 중복이라 빼고 말한다
            if all(p.part != s.part for p in picked):
                picked.append(s)
            if len(picked) == 2:
                break
        phrases = []
        for i, s in enumerate(reversed(picked)):
            if s.part is None:
                phrases.append(s.said)
            else:
                phrases.append(f"{_josa(s.part, '이', '가') if i == 0 else s.part + '도'} {spoken_said(s.part, s.said)}")
        return " 하셨고, ".join(phrases)

    def _with_checkin(self, kind: str, gen_ctx: dict, now: float) -> dict:
        """이전 턴에 들은 증상이 있으면 일상 대답 끝에 한 번만 '머리는 좀 어떠세요?'를 붙인다."""
        note = self.ctx.pending_checkin(now)
        if note is None:
            return gen_ctx
        self.ctx.mark_symptoms_followed()
        ack = R.CHECKIN_ACK.get(kind, R.CHECKIN_ACK["default"])
        if note.part is None:
            return {"kind": "checkin_nopart", "slots": {"ack": ack, "said": note.said}}
        return {"kind": "checkin", "slots": {"ack": ack, "part_topic": _josa(note.part, "은", "는"),
                                             "said": f"{_josa(note.part, '이', '가')} {spoken_said(note.part, note.said)}"}}

    def _pain_answer(self, sig: TextSignals, sit: Situation, now: float) -> Optional[DialogueOutput]:
        """'견딜 만하세요?' 바로 다음 대답: 견딜 만하면 안심, '아니'·'못 참겠어'면 안전 확인. 그 밖의 말은 평소대로."""
        q = self._pain_q
        if q is None or now - q[1] > self.cfg.pain_question_secs or sig.recall_question:
            return None
        note = q[0]
        if sig.answer_no and not sig.ok_explicit:
            sit.evidence.append(f"통증이 견딜 만하냐는 질문에 부정 대답 ({note.part or '부위 미상'})")
            return self._start_safety(now, sit, sig, "통증을 견디기 어렵다는 대답 → 안전 확인", variant="pain_check")
        if sig.ok or sig.answer_bearable:
            note.followed = True
            return self._speak(State.NORMAL, Strategy.SMALL_TALK, {"kind": "pain_bearable"},
                               "통증이 견딜 만하다는 대답 → 안심")
        return None

    # ───── 위험 증상 회상 질문 ─────
    @staticmethod
    def _danger_said(kinds) -> str:
        return " 하셨고, ".join(R.DANGER_SAID[k] for k in kinds)

    def _recall_danger(self, sig: TextSignals, sit: Situation, now: float) -> DialogueOutput:
        """회상 질문에 위험 증상이 인용됐을 때 (평상시). 회상 때문에 실제 위험이 무시되지 않게 한다.
        - 모두 이번 대화에서 들었고 이후 '괜찮다'고 분명히 확인됨 → 회상 대답
        - 들었지만 아직 괜찮다는 확인이 없음 → 그 증상을 짚어서 안전 확인
        - 들은 적 없음 → 새 호소일 수 있음: 숨·의식은 기존처럼 비상, 그 밖은 안전 확인 (기억을 지어내지 않음)"""
        c = self.ctx
        unresolved = [k for k in sig.danger_kinds if not (c.danger_record(k) and c.danger_record(k).resolved)]
        if not unresolved:
            c.mark_symptoms_followed()
            return self._speak(State.NORMAL, Strategy.SMALL_TALK,
                               {"kind": "recall_symptom", "slots": {"said": self._danger_said(sig.danger_kinds)}},
                               "위험 증상 회상 질문 (이번 대화에서 들었고 이후 괜찮다고 확인됨) → 회상 대답")
        unheard = [k for k in unresolved if c.danger_record(k) is None]
        if not unheard:
            sit.evidence.append(f"위험 증상 회상 질문 (기록 있음, 괜찮다는 확인 없음: {', '.join(unresolved)})")
            out = self._start_safety(now, sit, sig, "위험 증상 회상 → 지금 상태 확인", variant="recall_danger")
            out.response_text = self.gen.generate(Strategy.ASK_CHECK,
                                                  {"variant": "recall_danger", "said": self._danger_said(unresolved)})
            return out
        sit.evidence.append(f"위험 증상 회상 질문인데 이번 대화 기록 없음 ({', '.join(unheard)}) → 새 호소일 수 있음")
        if {"breath", "collapse"} & set(unheard):
            return self._escalate("위험 발화 감지: " + " / ".join(sit.evidence), "danger", now, sit)
        return self._start_safety(now, sit, sig, "기록 없는 위험 증상 회상 → 안전 확인", variant="recall_danger_new")

    # ───── STT 오류·작은 의도 ─────
    def _track_unclear(self, sig: TextSignals) -> None:
        c = self.ctx
        if not sig.stt_unclear:
            c.unclear_streak, c.last_unclear_key = 0, None
            return
        key = "shout" if sig.stt_unclear == "misheard_command" else "garbled"
        sig.unclear_same = c.last_unclear_key == key          # 직전 발화도 같은 종류의 오류였는지
        c.unclear_streak += 1
        c.last_unclear_key = key

    def _unclear_reply(self, sig: TextSignals) -> DialogueOutput:
        """첫 번째는 다시 말해 달라고, 두 번째는 마이크에 가까이 와 달라고, 같은 명령이 또 들리거나 세 번째부터는 화제 돌리기.
        들린 명령("소리 질러")은 실행하지 않는다."""
        n = self.ctx.unclear_streak
        if self.ctx.resume_topic is None and self.ctx.last_topic in R.TOPIC_NAME:
            self.ctx.resume_topic = self.ctx.last_topic   # 다시 알아들으면 이 화제로 돌아간다
        if sig.stt_unclear == "misheard_command":
            kind = "stt_back_off" if getattr(sig, "unclear_same", False) else "stt_retry"
        else:
            kind = "stt_retry" if n <= 1 else "stt_mic" if n == 2 else "stt_back_off"
        gen_ctx = {"kind": kind}
        if kind == "stt_back_off" and self.ctx.resume_topic:   # 같은 재질문을 반복하지 않고 하던 화제로 돌아가자고 한다
            gen_ctx = {"kind": "stt_back_off_topic", "slots": {"topic_name": R.TOPIC_NAME[self.ctx.resume_topic]}}
        return self._speak(State.NORMAL, Strategy.SMALL_TALK, gen_ctx,
                           f"STT 인식 결과가 불명확({sig.stt_unclear}, 연속 {n}회) → 실행·공감 없이 다시 묻기")

    def _small_intent_reply(self, sig: TextSignals) -> DialogueOutput:
        intent = sig.small_intent
        if intent == "mic_bare":                      # "들려?": 직전에 소리 이야기를 했으면 마이크 확인이 아니다
            prev = [t.text for t in self.ctx.history if t.role == "user"][-1:]
            intent = "sound_ask" if prev and ("소리" in prev[0] or "들려줘" in prev[0]) else "mic"
        if intent == "repeat":
            last = self.ctx.recent_ai(1)
            if not last:
                return self._speak(State.NORMAL, Strategy.SMALL_TALK, {"kind": "repeat_none"},
                                   "다시 말해 달라는 말, 직전 응답 없음 → 지어내지 않음")
            text = last[0][len(R.REPEAT_PREFIX):] if last[0].startswith(R.REPEAT_PREFIX) else last[0]
            return self._speak(State.NORMAL, Strategy.SMALL_TALK, {"kind": "repeat", "slots": {"last": text}},
                               "다시 말해 달라는 말 → 직전 응답 다시 말하기")
        return self._speak(State.NORMAL, Strategy.SMALL_TALK, {"kind": intent},
                           {"call": "부르는 말", "mic": "마이크 확인", "sound_ask": "소리 이야기 뒤의 '들려?'"}[intent])

    @staticmethod
    def _current_hazards(sig: TextSignals) -> list:
        return list(sig.hazard_kinds) if sig.hazard_level == "current" else []

    @staticmethod
    def _hazard_text_key(kinds) -> Optional[str]:
        """환경 위험 대피 안내: 화재·연기·타는 냄새가 하나라도 있으면 화재 안내(대피 우선), 가스만이면 가스 안내"""
        if not kinds:
            return None
        return "hazard_fire" if any(k in FIRE_KINDS for k in kinds) else "hazard_gas"

    @staticmethod
    def _hazard_group(kinds) -> Optional[str]:
        return None if not kinds else "fire" if any(k in FIRE_KINDS for k in kinds) else "gas"

    def _escalate_text_key(self, sig: TextSignals) -> Optional[str]:
        hazard = self._hazard_text_key(self._current_hazards(sig))
        return hazard or ("call_119" if sig.call_help == "call_119" else None)

    def _fall_context(self, s: SafetySession, now: float) -> bool:
        """이 안전 확인이 낙상과 관련됐는지 (낙상 질문이었거나, 최근 낙상 근거가 있거나, 낙상 사건이 이어지는 중)"""
        return (s.variant in ("fall", "fall_persist", "fell_report") or self.ctx.fall_active
                or self.ctx.recent_fall_evidence(now))

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
        if sig.improving: return "improving"               # "덜 아파": 나아지는 중 (다 나았다고 단정하지 않는다)
        if sig.thanks: return "thanks"
        if sig.goodnight: return "goodnight"
        if sig.about_ai: return "about_ai"
        if sig.question: return {"time": "time", "date": "date", "weather": "weather_ask",
                                 "abilities": "abilities"}[sig.question]
        if joy:                                       # 기쁨 표현·밝은 표정: 화제보다 먼저 기쁨을 받아준다
            return {"family": "joy_family", "health": "joy_health"}.get(topic, "joy")
        if sig.boredom: return "boredom"
        if sig.medicine_state:                              # 누락·거부·복용 판단 질문은 완료 칭찬으로 보내지 않는다
            return "medicine_" + sig.medicine_state
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
    def _sound_only_check(self, now, sit: Situation, sig: TextSignals) -> DialogueOutput:
        """낙상음만 있고 바닥 자세 근거가 없는 확인 (움직임 없음이 더해져 확인 기준을 넘은 경우).
        - 소리 전후 sound_pose_fresh_secs 안에 정상 자세를 실제로 받았으면 '가벼운 확인' (D-1)
        - 자세 정보가 없거나 오래됐으면(모름) 지금처럼 일반 안전 확인
        - 가벼운 확인 종류는 괜찮다는 대답·보호자 해제 뒤 쿨다운 동안 다시 묻지 않고 근거만 남긴다 (D-3)"""
        ts, conf = sit.sound
        light = self.ctx.normal_around(ts, now)
        if light and now < self.ctx.sound_cooldown_until:
            # 소리는 기록하고 보류 근거로 둔다: 보류 시간 안에 바닥 자세가 오면 기존 낙상 경로로 간다
            self.ctx.events = [e for e in self.ctx.events if e[0] != "fall_sound"]
            self.ctx.sound_hold = (ts, conf, now + self.cfg.sound_evidence_hold_secs)
            left = int(self.ctx.sound_cooldown_until - now)
            sit.evidence.append(f"소리 단독 충격 재확인 보류 (쿨다운 {left}초 남음, 정상 자세 관측) → 근거만 기록")
            return DialogueOutput(self.state, Strategy.NONE, None,
                                  reason="소리 단독 충격, 정상 자세 관측, 쿨다운 중 → 다시 묻지 않음: "
                                         + " / ".join(sit.evidence))
        if light:
            sit.evidence.append(f"소리 전후 {int(self.cfg.sound_pose_fresh_secs)}초 안 정상 자세 관측 → 가벼운 확인")
            return self._start_safety(now, sit, sig, "소리 단독 충격 → 가벼운 확인", variant="sound_light",
                                      sound=(ts, conf), light=True)
        sit.evidence.append("자세 정보 없음·오래됨 (정상과 구분 불가) → 일반 안전 확인")
        return self._start_safety(now, sit, sig, sound=(ts, conf))

    def _start_safety(self, now, sit: Situation, sig: TextSignals, why: str = "안전 확인 시작",
                      variant: Optional[str] = None, sound: Optional[tuple] = None,
                      light: bool = False) -> DialogueOutput:
        if variant:
            pass                                            # 호출한 쪽이 문맥으로 정한 질문 (예: 통증 재호소)
        elif sit.trigger == "utterance":
            variant = "pain" if sig.distress == "mild" else "fell_report"
        else:
            variant = sit.trigger or "fall"
        timeout = self.cfg.response_timeout
        if sit.risk_level >= RiskLevel.HIGH:
            timeout *= self.cfg.high_risk_timeout_ratio     # 위험도 높으면 기다리는 시간 단축
        self.state = State.SAFETY_CHECK
        self.session = SafetySession(asked_at=now, timeout=timeout, variant=variant, assessment=sit.to_dict(),
                                     started_at=now, sound_only=sound is not None, danger_origin=bool(sig.danger_kinds),
                                     sound_ts=sound[0] if sound else None, light=light)
        self.ctx.consume_events(now)
        # 소리 단독 확인이면 그 소리를 보류 근거로 둔다 (확인 중에는 계속, 끝나면 보류 시간 동안). 그 밖의 확인은 비운다
        self.ctx.sound_hold = (sound[0], sound[1], None) if sound else None
        out = self._speak(State.SAFETY_CHECK, Strategy.ASK_CHECK, {"variant": variant},
                          f"{why}: " + " / ".join(sit.evidence))
        out.assessment = sit.to_dict()
        out.response_deadline_sec = timeout
        return out

    def _safety(self, inp, sig: TextSignals, now) -> DialogueOutput:
        s = self.session
        # 의미 없는 STT 결과는 안전 응답으로 인정하지 않는다: 텍스트 없는 말소리(voice_unclear)와 같은 흐름
        # (다시 듣기는 기존 상한(max_listen_retries)까지만, 그 밖에는 무응답 타이머를 그대로 둔다)
        unclear = bool(sig.stt_unclear)
        if unclear:
            sig = NO_TEXT
        if s.phase == "ESCALATED":
            return self._after_escalation(inp, sig, now)     # 비상 중 비명은 기존대로 사용자 발화(안심시키기)
        if sig.scream or sig.moan:
            # 확인 중 비명·신음 단독("악!", "윽")은 대답이 아니다: 괜찮다는 대답·모호한 대답으로 세지 않고(확인이 풀리지 않게)
            # 목소리는 났지만 알아듣지 못한 경우와 같은 기존 절차(다시 듣기 상한 → 대기 → 재질문·비상)를 탄다
            s.evidence.append(f"확인 중 비명·신음 단독 발화: '{inp.stt.strip()}'")
            sig, unclear = NO_TEXT, True

        # 확인 중에 들어온 새 센서 근거를 다시 평가: 다른 원인의 낙상이 생겼거나 위험도가 올라가면
        # 이전 질문에 대한 대답으로 넘어가지 않고 그 근거로 다시 확인한다 (같은 입력의 대답도 반영하지 않음)
        sensed = assess(inp, NO_TEXT, self.ctx, self.cfg)
        start = (s.assessment or {}).get("risk_score", 0)
        variant = "fall" if s.variant == "sound_light" else s.variant
        if s.sound_only and sensed.sound_only:
            pass                           # 같은 소리 단독 근거의 반복: 질문을 다시 하지 않는다 (무응답 시점에 자세로 판단)
        elif sensed.risk_level >= RiskLevel.MEDIUM and (
                (sensed.trigger and sensed.trigger != variant) or sensed.risk_score > start or (s.light and sensed.pose_evidence)):
            prev = (s.assessment or {}).get("evidence", [])
            sensed.evidence = [f"이전 확인 근거: {x}" for x in prev] + sensed.evidence
            # 같은 사건이 다른 tick으로 나뉘어 도착(소리 단독 확인 뒤 낙상 자세, 또는 같은 종류의 질문)일 때만 질문을 반복하지 않는다.
            # 다른 원인의 확인(통증·누움 → 낙상)은 기존대로 그 원인의 질문을 새로 한다
            if (sensed.pose_evidence and now - s.started_at <= self.cfg.sound_dup_window_secs
                    and (s.sound_only or (sensed.trigger or "fall") == variant)):
                return self._adjust_safety(now, sensed)
            return self._start_safety(now, sensed, NO_TEXT, "확인 중 새 위험 근거 → 다시 확인")
        if (s.sound_only and not s.light and inp.pose == "normal"
                and s.sound_ts is not None and now - s.sound_ts <= self.cfg.sound_pose_fresh_secs
                and self.ctx.normal_around(s.sound_ts, now)):
            # 자세 정보 없이 시작한 소리 단독 확인에 소리 뒤 정상 자세가 들어옴 → 가벼운 확인으로 (질문은 다시 하지 않음)
            s.light = True
            s.evidence.append(f"소리 뒤 {int(now - s.sound_ts)}초에 정상 자세 관측 → 가벼운 확인으로 전환")

        if not sig.empty:
            s.responded = True             # 한 번이라도 말했으면 '무응답'이 아니다 (응답 있음 ≠ 안전 확인)
        if sig.self_harm:
            return self._escalate(f"확인 중 극단적 선택 암시 발화: '{inp.stt.strip()}'", "crisis", now)
        if sig.hazard_level == "uncertain" and sig.distress == "none":
            # 확인 중 "어제 가스 냄새가 났어": 다른 위험 주제이므로 지금 상황을 다시 확인한다 (기존 확인을 해제하지 않음)
            sit = assess(inp, sig, self.ctx, self.cfg)
            sit.evidence.append(f"확인 중 환경 위험 언급, 지금 위험한지 불확실 ({', '.join(sig.hazard_kinds)})")
            return self._start_safety(now, sit, sig, "환경 위험 불확실 → 안전 확인", variant="hazard_check")
        if sig.recall_question and (sig.recall_symptom or sig.danger_kinds) and sig.distress == "none":
            unheard = [k for k in sig.danger_kinds if self.ctx.danger_record(k) is None]
            if unheard:                    # 이번 대화에서 들은 적 없는 위험 증상: 새 호소일 수 있으므로 위험 응답으로 본다
                return self._escalate(f"확인 중 기록 없는 위험 증상 언급({', '.join(unheard)}): "
                                      f"'{inp.stt.strip()}'", "danger", now)
            # "아까 어디가 아프다고 했지?"는 위험 응답이 아니다: 들은 증상만 말하고 다시 확인.
            # 회상 질문은 안전 대답이 아니므로 대기 타이머는 이어서 센다 (회상만으로 확인이 늘어지거나 풀리지 않게)
            said = self._danger_said(sig.danger_kinds) if sig.danger_kinds else self._recalled_said()
            out = self._speak(State.SAFETY_CHECK, Strategy.CLARIFY,
                              {"variant": "recall" if said else "recall_none", "said": said},
                              "확인 중 증상 회상 질문 → 들은 증상으로 답하고 다시 확인 (비상 아님, 대기 시간 유지)")
            out.response_deadline_sec = max(0.0, s.timeout - (now - s.asked_at))
            return out
        if sig.small_intent and s.listen_retries < self.cfg.max_listen_retries:
            # "뭐라고?", "들려?", "다온아": 질문을 못 들은 것 → 안전 질문을 다시 한다 (다시 듣기 상한 안에서만)
            s.listen_retries += 1
            s.asked_at = now
            return self._waiting(self._speak(State.SAFETY_CHECK, Strategy.REASK, {},
                                             f"확인 중 '{inp.stt.strip()}' → 질문 다시 하기 (안전 응답 아님)"))
        if sig.rescue_like and sig.distress == "none":
            # 낙상 근거 없이 들린 "알려줘": 새 비상으로 올리지 않고, 도움이 필요한지 분명히 묻는다.
            # 대기 시간은 이어서 센다 (타이머 초기화·모호한 응답 처리(clarified)로 확인이 끝나지 않게)
            out = self._speak(State.SAFETY_CHECK, Strategy.CLARIFY, {"variant": "rescue_like"},
                              f"확인 중 구조 요청과 비슷한 말('{sig.rescue_word}'), 낙상 근거 없음 → 도움 필요 여부 확인")
            out.response_deadline_sec = max(0.0, s.timeout - (now - s.asked_at))
            return out
        if (sig.distress == "mild" and sig.pain_soft and not s.pain_probed
                and not self._head_hurt_after_fall(s, sig)):
            if self._fall_context(s, now) and not self.ctx.recovered(now):
                # 낙상 근거가 이어지는 중(바닥 자세·최근 낙상 근거, 자세 정보 끊김 포함)의 통증·부상 호소 → 바로 비상
                return self._escalate(f"낙상 근거가 이어지는 중 통증 호소: '{inp.stt.strip()}' "
                                      f"(pose={self.ctx.last['pose']}, 정상 자세 회복 확인 안 됨)", "danger", now)
            # 정상 자세로 회복됐거나 낙상과 무관한 확인: 가벼운 통증 설명("무릎이 좀 아파")은 한 번만 더 묻는다.
            # 다시 아프다고 하거나 대답이 없으면(재질문 없이) 비상.
            s.pain_probed, s.asked_at = True, now
            s.ask_count = max(s.ask_count, self.cfg.max_asks)
            return self._waiting(self._speak(State.SAFETY_CHECK, Strategy.CLARIFY, {"variant": "pain"},
                                             "확인 중 가벼운 통증 설명 → 한 번 더 확인 (재호소·무응답이면 비상)"))
        if sig.distress != "none":
            why = (f"낙상 근거 직후 구조 요청과 비슷한 말('{sig.rescue_word}')" if sig.rescue_like
                   else f"확인 중 환경 위험 발화({', '.join(sig.hazard_kinds)}): '{inp.stt.strip()}'"
                   if sig.hazard_level == "current"
                   else f"확인 중 위험 응답: '{inp.stt.strip()}'")
            return self._escalate(why, "danger", now, text_key=self._escalate_text_key(sig),
                                  hazards=self._current_hazards(sig))
        if sig.worsening and sig.distress == "none" and not sig.ok:
            # "지금은 더 심해": 안전 대답이 아니다. 얼마나 아픈지 다시 묻되 확인을 풀거나 대기를 늘리지 않는다
            # (즉시 비상은 아님: 무응답·위험 응답이면 기존 규칙대로 비상)
            s.evidence.append(f"확인 중 악화 호소: '{inp.stt.strip()}'")
            out = self._speak(State.SAFETY_CHECK, Strategy.CLARIFY, {"variant": "pain"},
                              "확인 중 증상 악화 호소 → 상태 다시 확인 (해제 아님, 대기 시간 유지)")
            out.response_deadline_sec = max(0.0, s.timeout - (now - s.asked_at))
            return out
        if sig.ok:
            self.ctx.resolve_dangers()     # 괜찮다는 분명한 대답 → 이번 대화의 위험 증상은 해소된 것으로 기록
            self._ok_release(s, now)
            return self._speak(State.NORMAL, Strategy.RESUME, {"variant": s.variant}, "응답 있음: 정상 확인")
        if sig.improving and sig.distress == "none" and (s.variant not in ("pain", "pain_check") or s.danger_origin):
            # 낙상·위험 증상·환경 위험 등으로 시작한 확인: "덜 아파"만으로는 풀지 않는다 (대기 시간 유지, 무응답이면 기존대로 비상)
            # 일반 통증 확인(pain·pain_check)에서는 현재 상태에 대한 유효한 대답으로 아래 기존 흐름(되묻기 → 대화 가능)을 탄다
            s.evidence.append(f"확인 중 호전 표현(해제 근거 아님): '{inp.stt.strip()}'")
            out = self._speak(State.SAFETY_CHECK, Strategy.CLARIFY, {"variant": "pain"},
                              "확인 중 부분 호전 표현 → 상태 다시 확인 (낙상·위험 확인은 해제하지 않음, 대기 시간 유지)")
            out.response_deadline_sec = max(0.0, s.timeout - (now - s.asked_at))
            return out
        if sig.asks_listener:
            # 듣는 사람에게 묻거나 부탁하는 높임말("괜찮으세요?", "말씀해 주세요")은 어르신 자신의 상태 대답이 아니다
            # (로봇 질문이 다시 인식된 경우 포함). 모호한 대답으로 세지 않아 이것만으로 확인이 끝나지 않는다.
            # 다시 듣기 상한 안에서는 질문을 다시 하고, 그 뒤에는 대기 시간을 이어서 센다 (무응답이면 기존대로 재질문·비상)
            if s.listen_retries < self.cfg.max_listen_retries:
                s.listen_retries += 1
                s.asked_at = now
                return self._waiting(self._speak(State.SAFETY_CHECK, Strategy.REASK, self._reask_ctx(),
                                                 "확인 중 상대에게 묻는 높임말 → 상태 대답 아님, 질문 다시 하기"))
            return DialogueOutput(State.SAFETY_CHECK, Strategy.NONE, None,
                                  reason="확인 중 상대에게 묻는 높임말 (상태 대답 아님) → 대기 계속")
        if not sig.empty:
            if s.clarified:
                self._close_safety(now)
                return self._speak(State.NORMAL, Strategy.RESUME, {"variant": s.variant},
                                   "응답 있음: 대화 가능 상태")
            s.clarified, s.asked_at = True, now
            return self._waiting(self._speak(State.SAFETY_CHECK, Strategy.CLARIFY, {}, "모호한 응답 → 구체 확인"))

        # 텍스트 없음 (speech / voice_unclear: 목소리는 감지됐지만 STT 결과가 없음, 또는 의미 없는 STT 결과)
        if (inp.sound_event in SPEECH_EVENTS or unclear) and s.listen_retries < self.cfg.max_listen_retries:
            s.listen_retries += 1
            s.asked_at = now
            return self._waiting(self._speak(State.SAFETY_CHECK, Strategy.RETRY_LISTEN, {},
                                             "음성은 감지됐으나 인식 실패 → 다시 듣기 (무응답 아님)"
                                             + (" (의미 없는 STT 결과)" if unclear else "")))
        if now - s.asked_at < s.timeout:
            return DialogueOutput(State.SAFETY_CHECK, Strategy.NONE, None, reason="응답 대기 중")

        if s.ask_count < self.cfg.max_asks:                  # 시간 초과 → 재질문
            s.ask_count += 1
            s.asked_at = now
            return self._waiting(self._speak(State.SAFETY_CHECK, Strategy.REASK, self._reask_ctx(), "무응답 → 재질문"))
        if s.light and s.sound_ts is not None and self.ctx.normal_kept(s.sound_ts, now):
            # 가벼운 확인 무응답: 정상 자세가 신선하게 계속 보이고 소리 뒤 바닥 자세가 없으면 알림 없이 평소 상태로.
            # 자세가 끊겼거나 정상이 아니면 아래 기존 경로(움직임·알림)로 간다
            age = now - self.ctx.normal_seen_ts
            ev = f"소리 충격 미확인, 정상 자세 유지 (마지막 정상 관측 {age:.0f}초 전, 소리 뒤 바닥 자세 없음) → 알림 없이 평소 상태"
            assessment = {**(s.assessment or {}), "evidence": list((s.assessment or {}).get("evidence", [])) + [ev]}
            self._close_safety(now)
            out = self._speak(State.NORMAL, Strategy.RESUME, {"variant": "sound_light_noreply"}, ev)
            out.assessment, out.check_level = assessment, "light"
            return out
        motion = self.ctx.motion(now)      # 유효시간 안에 실제로 받은 움직임만 해제 근거로 쓴다
        # 움직임은 '일어났다'는 뜻이 아니다: 바닥 자세가 오래 이어져 다시 확인한 경우엔 움직임만으로 해제하지 않는다
        if motion == "moving" and s.variant != "fall_persist":
            self._close_safety(now)
            return self._speak(State.NORMAL, Strategy.RESUME, {"variant": "moving_noreply"},
                               "무응답이나 움직임 확인됨 (일단 정상 처리, 모니터링 계속)")
        what = ("움직임은 있으나 바닥 자세 지속" if motion == "moving"
                else "움직임 없음" if motion else "움직임 정보 없음")
        head = "응답은 있었으나 안전 미확인 → 추가 질문에 무응답" if s.responded else "무응답"
        return self._escalate(f"{head} + {what} (motion={motion or '정보 없음'}, "
                              f"pose={self.ctx.last['pose']})", "no_response", now)

    # ───── 선택적 LLM (비동기): 규칙 판단이 먼저, 일상·공감 문장만 ─────
    def _llm_step(self, out: DialogueOutput, text: str, now: float) -> DialogueOutput:
        """1) 대기 중인 요청: 새 발화·출력·안전 상태·상태 변화가 있으면 폐기(말하지 않은 응답은 기록에 남기지 않음),
              아니면 도착·시간 초과를 확인해 원래 응답의 전략·상태 그대로 한 번만 출력
           2) 이번 응답이 허용 표의 일상·공감 응답이면 요청을 보내고, 이번 턴은 NONE으로 기다린다"""
        r = self._llm
        if r.pending is not None:
            req = r.pending
            if (text or out.strategy != Strategy.NONE or self.session is not None or out.emergency
                    or out.alert_status or out.notice or out.state != req.state_before):
                r.cancel()
            else:
                status, req = r.poll()
                if status in ("ready", "timeout"):
                    return self._llm_output(req, status)
                return out
        intent = self._llm_intent(out, text)
        if intent is None:
            return out
        if not r.available():
            out.llm_fallback_reason = "circuit_open"
            return out
        return self._llm_request(out, intent, now)

    def _llm_intent(self, out: DialogueOutput, text: str) -> Optional[str]:
        g = self._last_gen
        if (not text or not out.response_text or g is None or g[0] is not out
                or out.state not in (State.NORMAL, State.EMOTIONAL) or self.session is not None
                or out.emergency or out.alert_status or out.notice or out.check_level):
            return None
        return llm_intent_for(g[1], g[2])

    def _llm_request(self, out: DialogueOutput, intent: str, now: float) -> DialogueOutput:
        c = self.ctx
        turns = [(t.role, t.text) for t in list(c.history)[-self.cfg.llm_history_turns:]]
        symptoms = [f"{s.part or '부위 미상'} {s.said} ({max(0, int((now - s.ts) // 60))}분 전)" for s in c.symptoms[-3:]]
        symptoms += [f"{R.DANGER_SAID[d.kind]} ({max(0, int((now - d.ts) // 60))}분 전)" for d in c.dangers[-3:]
                     if d.kind in R.DANGER_SAID and not d.resolved]
        emotion = None
        if now - c.emotion_ts <= self.cfg.emotion_ttl and c.last["emotion"] in EMOTION_KO:
            emotion = EMOTION_KO[c.last["emotion"]]
        user_text = turns[-1][1] if turns and turns[-1][0] == "user" else ""
        system, messages = build_prompt(intent, turns, symptoms, c.last_topic, c.prev_topic, emotion,
                                        self._resume_hint, recent_ai=c.recent_ai(3),
                                        user_question=is_user_question(user_text))
        reask = self._last_gen[2].get("kind") in STT_REASK_ALLOWED
        req = self._llm.submit(out, out.response_text, system, messages, reask)
        # 응답을 내기 전까지는 이전 상태를 유지한다: 대기 중 출력이 상태 변화로 보이면 서버가 'thinking' 표시를 끝내므로
        req.state_before = self.state = self._state_before
        return DialogueOutput(self.state, Strategy.NONE, None, reason=f"LLM 응답 준비 중 (요청 #{req.rid}): {out.reason}",
                              assessment=out.assessment, warnings=list(out.warnings))

    def _llm_output(self, req, status: str) -> DialogueOutput:
        last = self.ctx.recent_ai(1)
        if status == "timeout":
            text, src, why, ms = req.fallback, "template", "timeout", None
        else:
            # 1) 원본의 의미상 위험(트리거·괜찮다는 대답·의료·안전 약속)을 먼저 본다: 후처리로 잘라 내 통과시키지 않는다
            # 2) 호칭 교정 + 문장 수·연속 질문 후처리  3) 후처리 결과에 기존 검증 전체
            raw = clean_llm_text(req.text)
            why = (req.error or "empty") if raw is None else semantic_issue(raw)
            cand = None
            if why is None:
                prev_q = bool(last) and last[0].strip().endswith("?")      # 실제로 출력된 직전 로봇 문장만
                cand = trim_llm_text(fix_honorifics(raw), prev_q, keep_question=req.reask)
                why = check_llm_text(cand, last[0] if last else None, self.cfg.llm_max_chars,
                                     self.cfg.llm_min_hangul, req.reask)
            text, src, ms = (cand, "llm", req.ms) if why is None else (req.fallback, "template", req.ms)
        base = req.out
        self.state = base.state                               # 미뤄 둔 상태 전환을 출력과 함께 적용
        note = " (LLM 문장)" if src == "llm" else f" (LLM 대신 템플릿: {why})"
        return dataclasses.replace(base, response_text=text, response_source=src, llm_ms=ms, llm_fallback_reason=why,
                                   reason=base.reason + note, warnings=[], emergency_active=False)

    # ───── 헬퍼 ─────
    @staticmethod
    def _head_hurt_after_fall(s: SafetySession, sig: TextSignals) -> bool:
        """낙상(의심) 뒤 머리·목 통증은 가볍게 말해도 바로 위험 응답으로 본다."""
        return s.variant in ("fall", "lying", "fall_persist", "fell_report", "sound_light") and bool({"머리", "목"} & set(sig.symptom_parts))

    def _waiting(self, out: DialogueOutput) -> DialogueOutput:
        """이번 질의 후 응답 대기 시간을 출력에 싣는다 (UI 카운트다운이 엔진 대기 시간과 맞도록)."""
        out.response_deadline_sec = self.session.timeout
        return out

    def _speak(self, state, strategy, gen_ctx, reason) -> DialogueOutput:
        self.state = state
        gen_ctx = {**gen_ctx, "history": self.ctx.recent_texts(), "recent_ai": self.ctx.recent_ai()}
        out = DialogueOutput(state, strategy, self.gen.generate(strategy, gen_ctx), reason=reason)
        self._last_gen = (out, strategy, gen_ctx)
        return out

    # ───── 비상 이후: 응답 있음 ≠ 안전 확인 ─────
    def _after_escalation(self, inp, sig: TextSignals, now) -> DialogueOutput:
        s = self.session
        if not sig.empty:
            s.responded, s.last_prompt_at = True, now           # 말을 했으므로 더 이상 '무응답'은 아니다
            text = inp.stt.strip()
            if sig.hazard_level == "current":
                # 비상 중 환경 위험: 처음 듣는 종류면 같은 사건 안에서 갱신 알림(updated)과 그 위험의 대피 안내,
                # 이미 알린 종류면 중복 알림 없이 짧은 확인 (사건을 끝내거나 새 사건으로 만들지 않는다)
                added = [k for k in sig.hazard_kinds if k not in s.hazards]
                if added:
                    s.hazards += added
                    if CAUSE_RANK["danger"] > CAUSE_RANK.get(s.cause, 0):
                        s.cause = "danger"
                    s.evidence.append(f"비상 중 환경 위험 발화: '{text}' ({', '.join(added)})")
                    return self._alert("updated", self._hazard_text_key(added), now,
                                       f"비상 중 새 환경 위험({', '.join(added)}) → 갱신 알림")
                return self._speak(State.SAFETY_CHECK, Strategy.REASSURE,
                                   {"cause": s.cause, "hazard": self._hazard_group(s.hazards)},
                                   "비상 중 같은 환경 위험 반복 → 중복 알림 없이 짧은 대피 확인")
            new = "crisis" if sig.self_harm else "danger" if sig.distress != "none" else None
            if new and CAUSE_RANK[new] > CAUSE_RANK.get(s.cause, 0):
                old, s.cause = s.cause, new                     # 더 심각한 정보 → 사유 갱신 + 즉시 알림
                s.evidence.append(f"비상 후 어르신 발화: '{text}' ({CAUSE_NOTE[new]})")
                return self._alert("updated", new, now, f"비상 중 더 위급한 발화 → 사유 갱신({old} → {new})")
            if new:                                             # 같은 수준의 위험: 이미 알렸으므로 안심시키기
                return self._speak(State.SAFETY_CHECK, Strategy.REASSURE, {"cause": s.cause, "hazard": self._hazard_group(s.hazards)},
                                   "비상 중 위험 발화 지속 → 비상 유지, 안심시키기")
            if s.hazards and (sig.ok or sig.hazard_report):
                # 화재·가스·연기가 누적된 사건(처음 원인이 낙상이어도)은 어르신 말만으로 끝내지 않는다 (보호자 해제 전까지 유지).
                # "괜찮아"·"불 껐어"·"밖으로 나왔어"는 상황 정보로만 기록하고, 새 알림은 만들지 않는다
                s.evidence.append(f"환경 위험 비상 중 어르신 발화(상황 정보, 해제 근거 아님): '{text}'")
                return self._speak(State.SAFETY_CHECK, Strategy.REASSURE,
                                   {"hazard_hold": "evacuated" if sig.hazard_report == "evacuated" else "default"},
                                   "환경 위험 비상 중 괜찮다·대피했다는 말 → 정보로만 기록, 비상 유지 (보호자 해제 전까지)")
            if sig.ok_explicit:                                 # "괜찮아" 같은 명확한 안전 응답만 해제
                cause, origin, incident = s.cause, s.first_cause, s.incident
                self.ctx.resolve_dangers()
                self._ok_release(s, now)
                variant = "after_crisis" if cause == "crisis" else "after_escalation"
                out = self._speak(State.NORMAL, Strategy.RESUME, {"variant": variant},
                                  "비상 후 명확한 안전 응답 → 비상 해제 (보호자에게 해제 알림)")
                out.alert_status, out.alert_cause, out.alert_origin, out.user_responded = \
                    "resolved", cause, origin, True
                out.alert_incident = incident
                return out
            if sig.ok and s.cause != "crisis":                 # "응/네"만으로는 확정하지 않고 되묻기
                return self._speak(State.SAFETY_CHECK, Strategy.CLARIFY, {},
                                   "비상 후 짧은 긍정 응답 → 안전 확인 전, 구체적으로 되묻기")
            return self._speak(State.SAFETY_CHECK, Strategy.REASSURE, {"cause": s.cause, "hazard": self._hazard_group(s.hazards)},
                               "비상 후 사용자 발화 (안전 미확인) → 비상 유지, 안심시키기")

        if s.realerts < self.cfg.max_realerts and now - s.last_alert_at >= self.cfg.realert_after:
            s.realerts += 1
            why = "응답은 있었으나 안전 미확인" if s.responded else f"{int(now - s.escalated_at)}초간 응답 없음"
            return self._alert("realert", "realert_responded" if s.responded else "realert", now,
                               f"비상 후 {why} → 재알림({s.realerts}/{self.cfg.max_realerts})")
        if now - s.last_prompt_at >= self.cfg.escalated_prompt_gap:
            s.last_prompt_at = now
            return self._speak(State.SAFETY_CHECK, Strategy.REASSURE, {"cause": s.cause, "hazard": self._hazard_group(s.hazards)},
                               "비상 후 주기적 안심 멘트")
        return DialogueOutput(State.SAFETY_CHECK, Strategy.NONE, None, reason="비상 알림 전송됨, 대기")

    def _escalate(self, reason, cause, now, sit: Optional[Situation] = None,
                  text_key: Optional[str] = None, hazards: Optional[list] = None) -> DialogueOutput:
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
        s.incident = next(self._incidents)          # _escalate는 비상이 아닌 상태에서만 불린다 → 항상 새 사건
        s.hazards = list(hazards or [])             # 이번 사건의 환경 위험 (알림 제목·대피 안내·짧은 반복 확인에 사용)
        key = text_key or ("no_response_after_reply" if cause == "no_response" and s.responded else cause)
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
                              user_responded=s.responded, reason=reason, assessment=assessment,
                              alert_incident=s.incident, hazard=",".join(s.hazards))

    def _end_safety(self, now):
        s = self.session
        if s is not None and s.sound_only:
            h = self.ctx.sound_hold
            if h is not None and h[2] is None:      # 확인이 끝나도 보류 시간 동안 소리 근거를 남긴다 (D-2)
                self.ctx.sound_hold = (h[0], h[1], now + self.cfg.sound_evidence_hold_secs)
            if not self._internal_end:              # 서버가 직접 부른 해제 = 보호자 해제 (D-3)
                self.ctx.sound_cooldown_until = now + self.cfg.sound_recheck_cooldown_secs
        self.state, self.session = State.NORMAL, None
        self.ctx.consume_events(now)

    def _close_safety(self, now):
        """엔진 안에서의 해제 (보호자 해제와 구분해 쿨다운을 걸지 않는다)"""
        self._internal_end = True
        try:
            self._end_safety(now)
        finally:
            self._internal_end = False

    def _ok_release(self, s: SafetySession, now):
        """어르신이 괜찮다고 해서 끝난 확인·비상: 소리 단독 확인이면 재확인 쿨다운 (D-3),
        누움 확인이면 다음 누움 무동작 재확인을 lying_recheck_after_ok_secs 뒤로 (D-4)"""
        if s.sound_only:
            self.ctx.sound_cooldown_until = now + self.cfg.sound_recheck_cooldown_secs
        if s.variant == "lying":
            self.ctx.lying_recheck_at = now + self.cfg.lying_recheck_after_ok_secs
        self._close_safety(now)

    def _reask_ctx(self) -> dict:
        return {"variant": "sound_light"} if self.session is not None and self.session.light else {}

    def _adjust_safety(self, now, sensed: Situation) -> DialogueOutput:
        """확인 시작 직후(sound_dup_window_secs 안) 따로 도착한 낙상 자세 근거: 같은 질문을 다시 하지 않고
        일반 안전 확인으로 단계·응답 시한만 조정한다 (질문 시각은 그대로, 위험도가 높으면 시한 단축) (D-5, D-2)"""
        s = self.session
        timeout = self.cfg.response_timeout
        if sensed.risk_level >= RiskLevel.HIGH:
            timeout *= self.cfg.high_risk_timeout_ratio
        was_light = s.light
        s.light, s.variant = False, sensed.trigger or "fall"
        s.timeout = min(s.timeout, timeout)
        s.assessment = sensed.to_dict()
        self.ctx.consume_events(now)
        self.ctx.sound_hold = None
        why = ("가벼운 확인 중 낙상 자세 근거 도착 → 일반 안전 확인으로 승격" if was_light
               else "확인 시작 직후 낙상 자세 근거가 따로 도착") + " (질문 반복 없이 단계·응답 시한 조정)"
        out = DialogueOutput(State.SAFETY_CHECK, Strategy.NONE, None, reason=f"{why}: " + " / ".join(sensed.evidence),
                             assessment=sensed.to_dict())
        out.response_deadline_sec = max(0.0, s.timeout - (now - s.asked_at))
        return out