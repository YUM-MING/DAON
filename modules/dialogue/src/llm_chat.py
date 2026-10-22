"""LLM 교감형 대화 보조: 허용 전략 표 · 프롬프트 · 출력 검증 · 비동기 요청(번호·무효화·시간 초과) · 서킷 브레이커.

안전 판단은 규칙 엔진이 먼저 끝낸 뒤, 일상·공감 계열 응답 문장만 LLM에 맡긴다.
LLM 문장이 검증을 통과하지 못하거나 늦으면 엔진이 이미 골라 둔 템플릿 문장을 쓴다.
"""
from __future__ import annotations

import itertools
import re
import threading
import time
from typing import Optional

from dialogue_types import Strategy
from llm_client import timed_generate
from text_analyzer import robot_speech_flags

# ───────── 허용 전략 표 (애매한 것은 금지) ─────────
# SMALL_TALK은 kind별로 판단한다. 사실 답변(시간·날씨·기능)·증상 관련·기억 회상·정해진 소형 의도는 금지
SMALL_TALK_ALLOWED = {
    "greeting", "thanks", "goodnight", "meal", "meal_done", "meal_not", "meal_ask", "boredom", "positive",
    "joy", "joy_family", "joy_health", "weather", "family", "health", "good_day", "tired", "sleep_bad", "sleep",
    "continue", "continue_family", "generic", "resume_topic",
}
SMALL_TALK_FORBIDDEN = {
    "about_ai", "time", "weather_ask", "abilities", "recall", "back_off", "relieved", "improving", "medicine",
    "health_ack", "wait", "medicine_missed", "medicine_ask", "medicine_refuse", "date", "date_unknown",
}
STT_REASK_ALLOWED = {"stt_retry", "stt_mic"}            # 안전 확인 밖의 일반 재질문만 (재질문 표지가 있어야 통과)
OTHER_STRATEGIES_ALLOWED = {Strategy.EMPATHIZE, Strategy.FOLLOW_UP, Strategy.GENTLE_PROBE}
SYMPTOM_KINDS = {"pain_soft", "pain_soft_nopart", "pain_bearable", "recall_symptom", "recall_none", "checkin",
                 "checkin_nopart"}

KIND_INTENT = {
    "greeting": "인사에 반갑게 답하고 안부를 묻기",
    "thanks": "고맙다는 말에 따뜻하게 답하기",
    "goodnight": "잘 자라는 인사에 편안한 밤 인사로 답하기",
    "meal": "식사 이야기에 관심을 보이고 이어 묻기",
    "meal_done": "식사를 하셨다는 말에 반응하고 무엇이 맛있었는지 묻기",
    "meal_not": "아직 식사를 안 하셨다는 말에 가볍게 식사를 권하기",
    "meal_ask": "무엇을 먹을지 고민하시는 말에 함께 고민해 주기",
    "boredom": "심심하다는 말에 공감하고 함께 이야기할 거리를 찾기",
    "positive": "좋은 이야기에 함께 기뻐하기",
    "joy": "기뻐하시는 말에 함께 기뻐하고 더 들려 달라고 하기",
    "joy_family": "가족 이야기로 기뻐하시는 말에 함께 기뻐하기",
    "joy_health": "몸이 좋아졌다는 기쁜 말에 함께 기뻐하기",
    "weather": "날씨 이야기에 맞장구치기 (실제 날씨를 지어내지 않기)",
    "family": "가족 이야기에 관심을 보이고 이어 묻기",
    "health": "건강·병원 이야기에 진단 없이 공감하기",
    "good_day": "좋았던 하루 이야기에 함께 기뻐하기",
    "tired": "피곤하다는 말에 공감하고 쉬시라고 하기",
    "sleep_bad": "잠을 잘 못 주무셨다는 말에 공감하기",
    "sleep": "잠 이야기에 공감하고 이어 묻기",
    "continue": "같은 화제를 이어 가며 자연스럽게 다음 이야기를 묻기",
    "continue_family": "가족 이야기를 이어 가며 자연스럽게 다음 이야기를 묻기",
    "generic": "말씀에 공감하고 관련된 질문 하나로 대화를 이어 가기",
    "resume_topic": "잘 못 알아들어 다시 여쭌 뒤 다시 말씀하심: 직전 화제를 기억해서 자연스럽게 이어 가기",
    "stt_retry": "말씀을 알아듣지 못함: 내용을 추측해 답하지 말고, 다시 한 번 말씀해 달라고 자연스럽게 부탁하기",
    "stt_mic": "두 번째로 알아듣지 못함: 조금 가까이에서 천천히 말씀해 달라고 부탁하기 (내용 추측 금지)",
}
STRATEGY_INTENT = {
    Strategy.EMPATHIZE: "감정에 공감하기",
    Strategy.FOLLOW_UP: "같은 고민이 이어져 걱정된다고 말하고 후속 질문 하나 하기",
    Strategy.GENTLE_PROBE: "표정이 어두운데 괜찮다고 하심: 캐묻지 말고 조심스럽게 마음을 묻기",
}
TOPIC_KO = {"lonely": "외로움", "family": "가족", "health": "건강", "sleep": "잠", "weather": "날씨", "grief": "그리운 분"}
EMOTION_KO = {"sad": "슬퍼 보임", "angry": "언짢아 보임", "fear": "불안해 보임", "happy": "밝아 보임"}

SYSTEM_PROMPT = (
    "너는 '다온'이라는 이름의 말벗 로봇이다. 혼자 지내시는 어르신과 대화한다.\n"
    "규칙:\n"
    "- 항상 존댓말로, 따뜻하고 간결하게 1~2문장으로 말한다. 질문은 한 번에 최대 1개만 한다.\n"
    "- 사용자가 실제로 한 말과 아래 참고 정보만 사용한다. 모르는 사실(날씨, 일정, 사람 이름 등)을 지어내지 않는다.\n"
    "- 사용자가 하지 않은 말을 한 것처럼 말하지 않는다.\n"
    "- 질병을 진단하거나 원인을 단정하지 않는다. 약 복용·중단·용량, 치료 방법, 병원·응급실 방문을 지시하지 않는다.\n"
    "- 약을 걸렀거나 먹어도 되는지 물으시면 판단하지 말고, 처방받은 병원이나 약국에 확인하시도록 부드럽게 안내한다.\n"
    "- 긴급 상황이라고 단정하지 않고, 119·구급차·보호자 연락을 약속하지 않는다. 안전 확인은 다른 기능이 맡는다.\n"
    "- 이모지, 영어, 마크다운, 목록 기호를 쓰지 않는다. 문장을 '네,'로 시작하지 않는다.\n"
    "- 알아듣지 못한 말은 내용을 추측해 답하지 말고, 같은 표현을 반복하지 않게 다시 말씀해 달라고 부탁한다.\n"
    "- 이야기를 청할 때는 '말씀해 주세요', '이야기해 주세요'처럼 분명한 표현을 쓴다. "
    "소리가 '살려주세요'와 비슷한 '들려주세요', '들려주실래요'는 쓰지 않는다.\n"
    "가족 호칭:\n"
    "- 어르신이 쓴 호칭을 자연스럽게 존중한다. 필요하면 '따님', '아드님', '손주분'처럼 자연스러운 존칭을 쓴다.\n"
    "- '딸님', '아들님'처럼 어색한 표현은 쓰지 않는다. 같은 호칭을 지나치게 반복하지 않는다.\n"
    "대화 방식:\n"
    "- 모든 말에 질문을 붙일 필요는 없다. 공감, 맞장구, 짧은 감상, 알맞은 후속 질문을 상황에 맞게 섞는다.\n"
    "- 어르신이 방금 답한 내용을 다시 묻지 않는다. 비슷한 질문이나 같은 어미('~하시나요?', '~있으신가요?')를 반복하지 않는다.\n"
    "- 어르신이 좋은 추억을 이야기하면 새 질문보다 그 마음을 함께 나눈다.\n"
    "- 어르신이 대화를 마무리하려 하면 억지로 다른 이야기를 요구하지 않는다.\n"
    "- 어르신이 말하지 않은 사건, 행동, 관계, 감정을 사실처럼 단정하지 않는다.\n"
    "- 어르신이 질문하시면 그 질문에 먼저 정확히 답한다. 전에 하신 말씀을 물으시면 기억한 내용으로 먼저 답하고 불필요한 질문을 붙이지 않는다."
)
QUESTION_PAUSE_HINT = ("직전 응답에서 이미 질문했습니다. 이번에는 새로운 질문을 하지 말고, 어르신의 말씀을 받아들이는 "
                       "공감·맞장구·짧은 감상으로 자연스럽게 답하세요.")
USER_QUESTION_HINT = "어르신이 질문하셨습니다. 그 질문에 먼저 정확히 답하세요 (모르는 사실은 지어내지 말고 모른다고 말하기)."
USER_QUESTION_RE = re.compile(r"(\?|니|냐|나요|까요|까|어때|뭐야|뭐지|어디야|언제야|누구야|했지|했더라|지요|죠)\s*$")
# LLM이 자주 쓰는 어색한 가족 호칭 → 자연스러운 존칭 (확인된 표현만, 다른 문장은 바꾸지 않는다)
HONORIFIC_FIX = (("딸님", "따님"), ("아들님", "아드님"))


def fix_honorifics(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    for bad, good in HONORIFIC_FIX:
        text = text.replace(bad, good)
    return text


def is_user_question(text: Optional[str]) -> bool:
    t = (text or "").strip().rstrip(".!~…")
    return bool(t) and bool(USER_QUESTION_RE.search(t))


def intent_for(strategy: Strategy, gen_ctx: dict) -> Optional[str]:
    """LLM을 써도 되는 응답이면 이번 응답의 의도 설명, 아니면 None (허용 표 밖은 모두 금지)"""
    kind = gen_ctx.get("kind")
    if gen_ctx.get("fixed_text") or gen_ctx.get("proactive"):
        return None
    if strategy == Strategy.SMALL_TALK:
        if kind in SMALL_TALK_ALLOWED or kind in STT_REASK_ALLOWED:
            return KIND_INTENT.get(kind)
        return None
    if strategy in OTHER_STRATEGIES_ALLOWED:
        if kind in SYMPTOM_KINDS:
            return None
        return STRATEGY_INTENT[strategy]
    return None


def build_prompt(intent: str, turns: list, symptoms: list, topic: Optional[str], prev_topic: Optional[str],
                 emotion: Optional[str], resume_topic: Optional[str], recent_ai=(), user_question: bool = False) -> tuple:
    """(system, messages). turns: [(role, text)] 최근 것까지 (role: user|ai). 식별 정보는 넣지 않는다.
    recent_ai: 실제로 출력된 최근 로봇 문장(오래된 것부터, 최대 3개). 마지막 문장이 질문이면 이번에는 질문하지 않도록 힌트"""
    info = [f"- 이번 응답의 의도: {intent}"]
    if user_question:
        info.append(f"- {USER_QUESTION_HINT}")
    recent = [t for t in recent_ai if t][-3:]
    if recent:
        info.append("- 최근 다온이 한 말(오래된 것부터): " + " / ".join(f"「{t}」" for t in recent)
                    + " — 같은 질문·표현·어미를 반복하지 말 것")
        if recent[-1].strip().endswith("?"):
            info.append(f"- {QUESTION_PAUSE_HINT}")
    if topic or prev_topic:
        info.append(f"- 현재 화제: {TOPIC_KO.get(topic, topic) or '없음'} / 직전 화제: "
                    f"{TOPIC_KO.get(prev_topic, prev_topic) or '없음'}")
    if resume_topic:
        info.append(f"- 다시 여쭙기 전에 하던 이야기: {TOPIC_KO.get(resume_topic, resume_topic)}")
    if symptoms:
        info.append("- 이번 대화에서 말씀하신 불편: " + ", ".join(symptoms) + " (진단·조언하지 말 것)")
    if emotion:
        info.append(f"- 표정 참고(확실하지 않음): {emotion}")
    system = SYSTEM_PROMPT + "\n\n[참고 정보]\n" + "\n".join(info)
    messages = [{"role": "assistant" if r == "ai" else "user", "content": t} for r, t in turns if t]
    return system, messages


# ───────── 출력 검증 (하나라도 걸리면 템플릿) ─────────
URL_RE = re.compile(r"https?://|www\.", re.I)
MARKDOWN_RE = re.compile(r"[*#`_>|\[\]{}]|^\s*[-•·]\s", re.M)
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
REASK_MARKERS = ("다시", "한 번 더", "한번 더", "못 들", "못들", "못 알아", "잘 안 들", "천천히", "가까이")
# 위험한 의료 지시·진단 (일반적인 병원·약 이야기 공감은 허용)
MEDICAL_RE = re.compile(
    r"(약|복용|용량|약물)[^.?!]{0,10}(드세요|드셔야|드시면 돼|드시지 마|끊으세요|끊으셔야|줄이세요|늘리세요|복용하세요|"
    r"복용하셔야|중단하세요|바꾸세요|더 드|덜 드)"
    r"|(병원|응급실|진료|검사|의사)[^.?!]{0,10}(가셔야|가 보셔야|가보셔야|가세요|받으셔야|받으세요|가셔야 해|가야 해)"
    r"|(찜질|파스|마사지|주사|수술|처방|소독|붕대|얼음)[^.?!]{0,8}(하세요|하셔야|붙이세요|받으세요|해 보세요|해보세요)"
    r"|(병|질환|염|증후군|암|치매|당뇨|고혈압|디스크|관절염|뇌졸중|골절)\s*(이에요|예요|일 거예요|인 것 같|일 수 있|"
    r"이신 것 같|때문)"
    r"|건너뛰셔도|건너뛰어도|걸러도 (돼|괜찮)|거르셔도|안 드셔도|안드셔도|그만 드셔도|그만드셔도|끊으셔도|두 배로|두배로|한꺼번에 드|"
    r"지금 드세요|바로 드세요|드셔도 (돼|괜찮)"
)
CAUSE_RE = re.compile(r"때문일|때문인 것|때문에 그런|원인은|탓일")
SYMPTOM_CONTEXT = ("아프", "아파", "통증", "쑤시", "저리", "어지러", "머리", "허리", "무릎", "가슴", "배가", "속이")
PROMISE_RE = re.compile(r"119|구급|응급|출동|신고|곧 도착|도착할|보호자[^.?!]{0,8}(연락|알리|알려|전화)|연락드릴|"
                        r"연락해 드릴|연락하고 있|불러 드릴|불러드릴")


def check_text(text: Optional[str], last_ai: Optional[str], max_chars: int, min_hangul: float,
               reask: bool = False) -> Optional[str]:
    """LLM 문장 검증. 통과하면 None, 아니면 탈락 사유 (템플릿으로 대체)"""
    if text is None or not text.strip():
        return "empty"
    t = text.strip()
    if len(t) > max_chars:
        return "length"
    if URL_RE.search(t) or MARKDOWN_RE.search(t) or EMOJI_RE.search(t) or t.count("\n") >= 1:
        return "format"
    letters = [c for c in t if c.isalpha()]
    hangul = sum(1 for c in letters if "가" <= c <= "힣")
    if not letters or hangul / len(letters) < min_hangul:
        return "hangul"
    if t.count("?") > 1:
        return "questions"
    if t.startswith("네,") or t.startswith("네 ") or t == "네":
        return "starts_ne"
    issue = semantic_issue(t)
    if issue:
        return issue
    if last_ai is not None and t == last_ai.strip():
        return "duplicate"
    if reask and not any(m in t for m in REASK_MARKERS):
        return "not_reask"
    return None


def semantic_issue(text: str) -> Optional[str]:
    """의미상 위험만 (check_text와 같은 순서·사유): ok_answer · trigger(비명 포함) · medical · safety_promise.
    LLM 원본에 먼저 적용해, 뒤 문장에 위험 내용이 있으면 후처리로 잘라 내기 전에 탈락시킨다"""
    flags = robot_speech_flags(text)
    if "ok" in flags:
        return "ok_answer"
    if any(f != "ok" for f in flags):
        return "trigger"
    # 비명은 '그 말만으로 된 발화' 기준이라 문장마다 따로 본다 (뒤 문장의 "아악!"이 잘려 나가 통과하지 않게)
    if any({"scream", "moan"} & set(robot_speech_flags(s)) for s in split_sentences(text)):
        return "trigger"
    if MEDICAL_RE.search(text) or (CAUSE_RE.search(text) and any(k in text for k in SYMPTOM_CONTEXT)):
        return "medical"
    if PROMISE_RE.search(text):
        return "safety_promise"
    return None


SENTENCE_RE = re.compile(r"[^.?!…]+[.?!…]*")


def split_sentences(text: str) -> list:
    """'.', '?', '!', '…' 기준 문장 나누기 (말줄임표·연속 부호로 빈 문장이 생기지 않게)"""
    return [s.strip() for s in SENTENCE_RE.findall(text) if any(c.isalnum() for c in s)]


def trim(text: str, prev_was_question: bool, keep_question: bool = False) -> str:
    """일반 대화 LLM 문장 후처리 (문장을 새로 만들지 않고 고르기만 한다)
    - 질문 문장이 2개 이상이면 첫 질문 문장까지만 (그 앞의 평서문은 유지, 뒤 문장은 뺀다)
    - 3문장 이상이면 앞의 2문장
    - 직전 실제 로봇 문장이 질문이었고 이번 마지막 문장도 질문이며, 질문이 아닌 문장이 남아 있으면 마지막 질문을 뺀다
      (keep_question: STT 재질문처럼 질문이 목적인 응답은 빼지 않는다)
    결과가 비면 원문을 그대로 돌려준다 (판단은 기존 검증에 맡김)"""
    sents = split_sentences(text)
    if not sents:
        return text
    questions = [n for n, x in enumerate(sents) if x.endswith("?")]
    if len(questions) >= 2:                 # "…하고 계시나요? …있으신가요?" → 첫 질문까지만 (질문이 목적인 재질문도 하나는 남음)
        sents = sents[:questions[0] + 1]
    if len(sents) >= 3:
        sents = sents[:2]
    if (prev_was_question and not keep_question and len(sents) >= 2 and sents[-1].endswith("?")
            and any(not s.endswith("?") for s in sents[:-1])):
        sents = sents[:-1]
    out = " ".join(sents).strip()
    return out or text


def clean(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    t = " ".join(text.strip().split(" "))
    return t.strip().strip('"').strip("“”'").strip() or None


# ───────── 비동기 요청 · 서킷 브레이커 ─────────
class LLMRequest:
    def __init__(self, rid: int, out, fallback: str, reask: bool, started: float):
        self.rid, self.out, self.fallback, self.reask, self.started = rid, out, fallback, reask, started
        self.done = threading.Event()
        self.text: Optional[str] = None
        self.ms: Optional[int] = None
        self.error: Optional[str] = None


class LLMRunner:
    """대기 요청은 최대 1개. 결과는 작업 스레드가 요청 객체에만 쓰고, 엔진 스레드가 읽는다 (엔진 상태는 건드리지 않음).
    무효화된 요청의 HTTP는 끊을 수 없으므로 결과만 버린다."""

    def __init__(self, client, timeout: float, breaker_failures: int = 3, breaker_secs: float = 60.0,
                 clock=time.monotonic):
        self.client, self.timeout, self.clock = client, float(timeout), clock
        self.breaker_failures, self.breaker_secs = breaker_failures, breaker_secs
        self.pending: Optional[LLMRequest] = None
        self.failures = 0
        self.open_until = float("-inf")
        self.calls = 0
        self._ids = itertools.count(1)

    def available(self) -> bool:
        return self.clock() >= self.open_until

    def submit(self, out, fallback: str, system: str, messages: list, reask: bool) -> LLMRequest:
        req = LLMRequest(next(self._ids), out, fallback, reask, self.clock())
        self.pending = req
        self.calls += 1

        def work():
            req.text, req.ms, req.error = timed_generate(self.client, system, messages, self.timeout)
            req.done.set()
        threading.Thread(target=work, name=f"dialogue-llm-{req.rid}", daemon=True).start()
        return req

    def cancel(self) -> Optional[LLMRequest]:
        req, self.pending = self.pending, None
        return req

    def poll(self) -> tuple:
        """('wait' | 'ready' | 'timeout', req). ready/timeout이면 대기 요청을 비운다 (한 요청은 한 번만 출력)"""
        req = self.pending
        if req is None:
            return "none", None
        if req.done.is_set():
            self.pending = None
            self._record(req.text is not None)
            return "ready", req
        if self.clock() - req.started >= self.timeout:
            self.pending = None
            self._record(False)
            return "timeout", req
        return "wait", req

    def _record(self, ok: bool) -> None:
        if ok:
            self.failures = 0
            return
        self.failures += 1
        if self.failures >= self.breaker_failures:
            self.open_until = self.clock() + self.breaker_secs
            self.failures = 0
