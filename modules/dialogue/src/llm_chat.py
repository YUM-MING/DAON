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
from text_analyzer import RELATION_GROUNDED_BY, relations_in, robot_speech_flags

# ───────── 허용 전략 표 (애매한 것은 금지) ─────────
# SMALL_TALK은 kind별로 판단한다. 사실 답변(시간·날씨·기능)·증상 관련·기억 회상·정해진 소형 의도는 금지
SMALL_TALK_ALLOWED = {
    "greeting", "thanks", "goodnight", "meal", "meal_done", "meal_not", "meal_ask", "boredom", "positive",
    "joy", "joy_family", "joy_health", "weather", "family", "health", "good_day", "tired", "sleep_bad", "sleep",
    "continue", "continue_family", "generic", "resume_topic", "meal_memory", "visit", "short_yes", "short_no",
    "emotion", "family_distance", "meal_done_item", "family_plan_more",
}
SMALL_TALK_FORBIDDEN = {
    "about_ai", "time", "weather_ask", "abilities", "recall", "back_off", "relieved", "improving", "medicine",
    "health_ack", "wait", "medicine_missed", "medicine_ask", "medicine_refuse", "date", "date_unknown",
    "medicine_check", "medicine_check_pain", "medicine_check_pain_nopart",
    "short_open", "short_just", "short_chain",       # 질문 없는 고정 문장이어야 하는 짧은 대답 응답
    "listen",                                        # 경청 요청: 화제 전환·질문 없는 고정 문장
    "complaint",                                     # 응답에 대한 지적: 사과·다시 말씀 요청 고정 문장
    "recall_visit", "family_word", "family_word_visit",   # 대화 기록 회상·호칭 이어받기: 사실·확인된 관계만 쓰는 고정 문장
}
STT_REASK_ALLOWED = {"stt_retry", "stt_mic"}            # 안전 확인 밖의 일반 재질문만 (재질문 표지가 있어야 통과)
OTHER_STRATEGIES_ALLOWED = {Strategy.EMPATHIZE, Strategy.FOLLOW_UP, Strategy.GENTLE_PROBE}
SYMPTOM_KINDS = {"pain_soft", "pain_soft_nopart", "pain_bearable", "recall_symptom", "recall_none", "checkin",
                 "checkin_nopart"}

KIND_INTENT = {
    "greeting": "인사에 반갑게 답하기 (안부 질문은 필요할 때만 하나)",
    "thanks": "고맙다는 말에 따뜻하게 답하기",
    "goodnight": "잘 자라는 인사에 편안한 밤 인사로 답하기",
    "meal": "식사 이야기에 관심을 보이며 공감하기 (질문은 필요할 때만 하나)",
    "meal_done": "식사를 하셨다는 말에 반가워하기 (무엇을 드셨는지 묻는 것은 필요할 때만)",
    "meal_not": "아직 식사를 안 하셨다는 말에 가볍게 식사를 권하기",
    "meal_ask": "무엇을 먹을지 고민하시는 말에 함께 고민해 주기",
    "boredom": "심심하다는 말에 공감하고 함께 이야기할 거리를 찾기",
    "positive": "좋은 이야기에 함께 기뻐하기",
    "joy": "기뻐하시는 말에 함께 기뻐하고 더 들려 달라고 하기",
    "joy_family": "가족 이야기로 기뻐하시는 말에 함께 기뻐하기",
    "joy_health": "몸이 좋아졌다는 기쁜 말에 함께 기뻐하기",
    "weather": "날씨 이야기에 맞장구치기 (실제 날씨를 지어내지 않기)",
    "family": "가족 이야기에 관심을 보이며 공감하기 (질문은 필요할 때만 하나)",
    "health": "건강·병원 이야기에 진단 없이 공감하기",
    "good_day": "좋았던 하루 이야기에 함께 기뻐하기",
    "tired": "피곤하다는 말에 공감하고 쉬시라고 하기",
    "sleep_bad": "잠을 잘 못 주무셨다는 말에 공감하기",
    "sleep": "잠 이야기에 공감하기 (질문은 필요할 때만 하나)",
    "continue": "같은 화제를 이어 가며 공감하기 (다음 이야기를 묻는 것은 필요할 때만 하나)",
    "continue_family": "가족 이야기를 이어 가며 공감하기 (다음 이야기를 묻는 것은 필요할 때만 하나)",
    "generic": "말씀에 공감하며 대화를 이어 가기 (질문은 필요할 때만 하나)",
    "visit": "누가 놀러 온다는 소식에 반가움을 함께 나누기 (누구인지·어떤 관계인지 짐작해 붙이지 않기)",
    "short_yes": "직전 질문에 어르신이 '응'이라고 답하심: 그 대답을 받아 같은 이야기를 자연스럽게 이어 가기 (새 사실을 지어내지 않기)",
    "short_no": "직전 질문에 어르신이 '아니'라고 답하심: 그대로 받아들이고 캐묻지 않기 (새 사실을 지어내지 않기)",
    "emotion": "어르신이 말씀하신 마음에 공감하기",
    "family_distance": "가족 연락이 뜸하다는 말에 허전한 마음을 받아 주고 곁에 있다고 말하기 (연락 여부를 되묻지 않기)",
    "meal_done_item": "드신 음식을 말씀하셨으니 무엇을 드셨는지 다시 묻지 말고 잘 드셨다고 반가워하기",
    "family_plan_more": "가족에게 해 주려는 준비 말씀에 그 마음을 받아 주기 (아직 하지 않은 일이니 끝난 것처럼 묻지 않기, 질문 없이)",
    "meal_memory": "추억을 들어주고 공감하며 그 음식에 대해 묻기 (방금 식사하신 것이 아님. 질문은 필요할 때만, 마음을 함께 나누는 것이 먼저)",
    "resume_topic": "잘 못 알아들어 다시 여쭌 뒤 다시 말씀하심: 직전 화제를 기억해서 자연스럽게 이어 가기",
    "stt_retry": "말씀을 알아듣지 못함: 내용을 추측해 답하지 말고, 다시 한 번 말씀해 달라고 자연스럽게 부탁하기",
    "stt_mic": "두 번째로 알아듣지 못함: 조금 가까이에서 천천히 말씀해 달라고 부탁하기 (내용 추측 금지)",
}
STRATEGY_INTENT = {
    Strategy.EMPATHIZE: "감정에 공감하기",
    Strategy.FOLLOW_UP: "같은 고민이 이어져 걱정된다고 말하기 (후속 질문은 필요할 때만 하나)",
    Strategy.GENTLE_PROBE: "표정이 어두운데 괜찮다고 하심: 캐묻지 말고 조심스럽게 마음을 살피기 (질문은 필요할 때만 하나)",
}
# 감정·단계별 짧은 지시 (EMPATHIZE·FOLLOW_UP·emotion 의도 뒤에 붙인다). 감정은 어르신이 직접 말했을 때만 정해진다
EMO_INTENT = {
    "lonely": {1: "외로움: 마음을 받아 주기", 2: "외로움: 곁에 있다는 마음을 전하며 경청하기",
               3: "외로움: 공감하며 가까운 관계나 일상 이야기로 부드럽게 이어 가기 (새 인물·사건을 지어내지 않기)"},
    "sad": {1: "슬픔: 감정을 그대로 받아 주기 (위로를 서두르지 않기)", 2: "슬픔: 판단 없이 경청하기",
            3: "슬픔: 부담 없는 위로 건네기"},
    "anxious": {1: "불안: 그런 마음이 들 수 있다고 인정하기", 2: "불안: 무엇이 마음에 걸리는지 부드럽게 확인하기",
                3: "불안: 안정을 돕는 말 건네기 (해결책을 단정하지 않기)"},
    "angry": {1: "화: 감정을 받아 주기 (따지거나 판단하지 않기)", 2: "화: 무슨 일이 있었는지 경청하기",
              3: "화: 마음이 가라앉도록 차분하게 돕기"},
    "lethargic": {1: "의욕이 없다고 하심: 공감하고 다그치지 않기", 2: "무기력: 곁에 있다는 마음을 전하기",
                  3: "무기력: 부담 없는 아주 작은 활동을 제안하기 (강요하지 않기)"},
}
TOPIC_KO = {"lonely": "외로움", "family": "가족", "health": "건강", "sleep": "잠", "weather": "날씨", "grief": "그리운 분"}
EMOTION_KO = {"sad": "슬퍼 보임", "angry": "언짢아 보임", "fear": "불안해 보임", "happy": "밝아 보임"}

# LLM이 '도저히 알아들을 수 없음'을 알리는 표시 (보조 수단: 규칙 판정이 먼저). 사용자에게·TTS·기록으로 나가지 않는다
UNCLEAR_MARK = "[다시]"

SYSTEM_PROMPT = (
    "너는 '다온'이라는 이름의 말벗 로봇이다. 혼자 지내시는 어르신과 대화한다.\n"
    "규칙:\n"
    "- 항상 존댓말로, 따뜻하고 간결하게 1~2문장으로 말한다. 질문은 한 번에 최대 1개만 한다.\n"
    "- 사용자가 실제로 한 말과 아래 참고 정보만 사용한다. 모르는 사실(날씨, 일정, 사람 이름 등)을 지어내지 않는다.\n"
    "- 사용자가 하지 않은 말을 한 것처럼 말하지 않는다.\n"
    "- 사용자 발화와 확인된 문맥에 없는 사실을 생성하지 않는다. 불확실한 핵심 정보는 사실로 확정하거나 반복하지 않고, "
    "필요한 경우 짧게 확인한다.\n"
    f"- 문장이 말이 안 되거나 낱말이 서로 맞지 않아 의미를 알 수 없으면, 상황을 짐작하거나 질문을 만들지 말고 {UNCLEAR_MARK} 만 "
    f"출력한다. 예: '창문이 국수를 달린다. 원래 그렇다.', '이 신발의 노래 두 번' → {UNCLEAR_MARK}. "
    "짧거나 서툴러도 뜻이 통하는 말('피곤해', '허리가 아파')에는 평소처럼 답한다.\n"
    "- 질병을 진단하거나 원인을 단정하지 않는다. 약 복용·중단·용량, 치료 방법, 병원·응급실 방문을 지시하지 않는다.\n"
    "- 약을 걸렀거나 먹어도 되는지 물으시면 판단하지 말고, 처방받은 병원이나 약국에 확인하시도록 부드럽게 안내한다.\n"
    "- 긴급 상황이라고 단정하지 않고, 119·구급차·보호자 연락을 약속하지 않는다. 안전 확인은 다른 기능이 맡는다.\n"
    "- 이모지, 영어, 마크다운, 목록 기호를 쓰지 않는다. 문장을 '네,'로 시작하지 않는다.\n"
    "- 알아듣지 못한 말은 내용을 추측해 답하지 말고, 같은 표현을 반복하지 않게 다시 말씀해 달라고 부탁한다.\n"
    "- 이야기를 청할 때는 '말씀해 주세요', '이야기해 주세요'처럼 분명한 표현을 쓴다. "
    "소리가 '살려주세요'와 비슷한 '들려주세요', '들려주실래요'는 쓰지 않는다.\n"
    "가족 호칭:\n"
    "- 참고 정보의 '확인된 관계'에 있는 호칭만 사용한다. 어르신이 말하지 않은 가족관계를 짐작해 넣지 않는다.\n"
    "- 확인된 관계를 높여 부를 때는 딸은 '따님', 아들은 '아드님', 손녀는 '손녀분', 손자는 '손자분', 손주는 '손주분'처럼 "
    "어르신이 말한 관계를 그대로 높인다. '딸님', '아들님'처럼 어색한 표현은 쓰지 않는다. 같은 호칭을 지나치게 반복하지 않는다.\n"
    "- 어르신이 말한 사람 이름은 이름만 쓰거나 '철수 씨'처럼 부른다. 이름 뒤에 '분'을 붙이지 않는다.\n"
    "대화 방식:\n"
    "- 모든 말에 질문을 붙일 필요는 없다. 공감, 맞장구, 짧은 감상, 알맞은 후속 질문을 상황에 맞게 섞는다.\n"
    "- 어르신이 방금 답한 내용을 다시 묻지 않는다. 비슷한 질문이나 같은 어미('~하시나요?', '~있으신가요?')를 반복하지 않는다.\n"
    "- 어르신이 좋은 추억을 이야기하면 새 질문보다 그 마음을 함께 나눈다.\n"
    "- 어르신이 대화를 마무리하려 하면 억지로 다른 이야기를 요구하지 않는다.\n"
    "- 어르신이 말하지 않은 사건, 행동, 관계, 감정을 사실처럼 단정하지 않는다.\n"
    "- '~하려고', '~할 거야', '~해 줘야지', '~해 주려고'처럼 앞으로 할 일을 말씀하시면 아직 하지 않은 일로 다룬다. "
    "그 일이 이미 끝난 것처럼 묻거나('다 만드셨어요?', '드셨어요?') 단정하지 않는다.\n"
    "- 어르신이 질문하시면 그 질문에 먼저 정확히 답한다. 전에 하신 말씀을 물으시면 기억한 내용으로 먼저 답하고 불필요한 질문을 붙이지 않는다."
)
QUESTION_PAUSE_HINT = ("직전 응답에서 이미 질문했습니다. 이번에는 새로운 질문을 하지 말고, 어르신의 말씀을 받아들이는 "
                       "공감·맞장구·짧은 감상으로 자연스럽게 답하세요.")
LISTENING_HINT = ("어르신이 이야기를 들어 주기를 원하십니다. 질문하지 말고 공감·수용하는 말 1~2문장으로만 답하세요. "
                  "다른 화제를 제안하지 마세요.")
USER_QUESTION_HINT ="어르신이 질문하셨습니다. 그 질문에 먼저 정확히 답하세요 (모르는 사실은 지어내지 말고 모른다고 말하기)."
USER_QUESTION_RE = re.compile(r"(\?|니|냐|나요|까요|까|어때|뭐야|뭐지|어디야|언제야|누구야|했지|했더라|지요|죠)\s*$")
# LLM이 자주 쓰는 어색한 가족 호칭 → 자연스러운 존칭 (확인된 표현만, 다른 문장은 바꾸지 않는다)
HONORIFIC_FIX = (("딸님", "따님"), ("아들님", "아드님"), ("딸분", "따님"))
# 로봇이 어르신의 딸을 "딸아이"라고 부르면 "따님"으로 (받침이 생기므로 조사도 맞춘다). 로봇 문장(LLM 출력)에만 적용
DAUGHTER_CHILD_RE = re.compile(r"딸아이(이랑|가|는|를|와|랑)?")
DAUGHTER_CHILD_JOSA = {"가": "이", "는": "은", "를": "을", "와": "과", "랑": "이랑", "이랑": "이랑"}


def fix_honorifics(text: Optional[str]) -> Optional[str]:
    """LLM이 쓴 어색한 가족 호칭만 고친다 (어르신 원문·인용·관계 판정 낱말에는 쓰지 않는다)"""
    if text is None:
        return None
    for bad, good in HONORIFIC_FIX:
        text = text.replace(bad, good)
    return DAUGHTER_CHILD_RE.sub(lambda m: "따님" + DAUGHTER_CHILD_JOSA.get(m.group(1) or "", m.group(1) or ""), text)


# "철수 분이" → "철수 씨가": 어르신이 말한 이름 뒤의 '분'만 고친다 ("두 분", "그분", "분위기"는 그대로). 이름 사전 없음:
# 어르신 발화에 '이름+조사'로 나온 낱말만 이름으로 본다
NAME_BUN_RE = re.compile(r"([가-힣]{2,3}) ?분(께서|이랑|께|이|을|과|은|도|의)?(?![가-힣])")
NAME_BUN_PARTICLE = {"께서": "씨가", "이": "씨가", "께": "씨에게", "을": "씨를", "과": "씨와", "은": "씨는", "도": "씨도",
                     "의": "씨의", "이랑": "씨랑", "": "씨"}
NAME_PARTICLES = ("가", "이", "는", "은", "랑", "이랑", "하고", "도", "한테", "를", "을", "와", "과", "네")
NOT_NAMES = {"어르신", "손님", "다른", "모든", "여러", "어느", "이웃", "선생"}


def fix_name_honorific(text: Optional[str], user_texts=()) -> Optional[str]:
    if not text:
        return text
    words = {w for t in user_texts for w in re.findall(r"[가-힣]+", t or "")}

    def said_as_name(x: str) -> bool:
        return (x not in NOT_NAMES and not relations_in(x) and not relations_in(x, robot=True)
                and any(w == x + p for w in words for p in NAME_PARTICLES))

    def repl(m):
        name, particle = m.group(1), m.group(2) or ""
        return f"{name} {NAME_BUN_PARTICLE[particle]}" if said_as_name(name) else m.group(0)
    return NAME_BUN_RE.sub(repl, text)


# 잘못 들린 친족어를 사람처럼 높여 부르는 문장("솔녀 씨가", "소녀분이"): 사전에 없는 낱말을 인물로 인용하지 않는다 → 템플릿
KIN_STT_QUOTE_RE = re.compile(r"(?:소녀|솔녀|손너|손여|손쥬|손짜)\s?(?:분|씨|님)")


def kin_quote_issue(text: Optional[str]) -> Optional[str]:
    return "kin_stt_quote" if text and KIN_STT_QUOTE_RE.search(text) else None


# 계획·미완료 시제 보호: 이번 어르신 발화가 '앞으로 할 일'(려고·줄 거·해 줘야지 …)이고 그 행동이 음식 만들기·준비일 때만,
# 그 행동이 이미 끝났다고 전제하는 로봇 표현("다 만드셨어요?", "끓이셨어요?")을 막는다. 과거형 전체를 막지 않는다
# ("손녀분이 좋아하시던 음식인가요?"는 통과)
PLAN_MARK_RE = re.compile(r"(?:려고|려구|줄려|주려|줄거|할거|해야지|줘야지|줘야겠|하려|할래)")
PLAN_STEMS = ("끓", "만들", "요리", "해주", "해 주", "해줘", "해 줘", "차려", "준비")
PLAN_DONE_RE = re.compile(r"끓이셨|끓였|만드셨|만들었|요리하셨|해 주셨|해주셨|해 드렸|해드렸|차리셨|준비하셨|준비했|다 하셨")


def plan_tense_issue(text: Optional[str], user_text: Optional[str]) -> Optional[str]:
    if not text or not user_text:
        return None
    if not PLAN_MARK_RE.search(re.sub(r"\s+", "", user_text)) or not any(s in user_text for s in PLAN_STEMS):
        return None
    return "plan_tense" if PLAN_DONE_RE.search(text) else None


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
            intent = KIND_INTENT.get(kind)
            if intent and gen_ctx.get("question"):        # 짧은 대답: 어떤 질문에 대한 대답인지 함께 준다
                intent += f" (직전 질문: 「{gen_ctx['question']}」)"
            return _with_emotion(intent, gen_ctx)
        return None
    if strategy in OTHER_STRATEGIES_ALLOWED:
        if kind in SYMPTOM_KINDS or kind in ("redirect", "redirect_no"):   # 환기 제안·거절 응답은 사실만 쓰는 고정 문장
            return None
        return _with_emotion(STRATEGY_INTENT[strategy], gen_ctx)
    return None


def _with_emotion(intent: Optional[str], gen_ctx: dict) -> Optional[str]:
    """감정·단계 지시를 짧게 붙인다 (어르신이 직접 말한 감정이 있을 때만)"""
    hint = EMO_INTENT.get(gen_ctx.get("emo_kind"), {}).get(gen_ctx.get("emo_stage"))
    return f"{intent} ({hint})" if intent and hint else intent


def build_prompt(intent: str, turns: list, symptoms: list, topic: Optional[str], prev_topic: Optional[str],
                 emotion: Optional[str], resume_topic: Optional[str], recent_ai=(), user_question: bool = False,
                 known_relations=None, focus: Optional[str] = None, listening: bool = False) -> tuple:
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
    if listening:
        info.append(f"- {LISTENING_HINT}")
    if focus or topic:
        info.append(f"- 지금 이야기 중인 사람·화제: {focus or '없음'} · {TOPIC_KO.get(topic, topic) or '없음'} "
                    "(짧거나 이어지는 말이면 이 문맥으로 답한다)")
    if known_relations is not None:
        rels = ", ".join(sorted(known_relations)[:MAX_RELATIONS_IN_PROMPT])
        info.append(f"- 확인된 관계: {rels or '없음'} (이 밖의 가족관계는 짐작해 말하지 않는다)")
    system = SYSTEM_PROMPT + "\n\n[참고 정보]\n" + "\n".join(info)
    messages = [{"role": "assistant" if r == "ai" else "user", "content": t} for r, t in turns if t]
    return system, messages


# ───────── 근거 검사: 어르신이 말하지 않은 관계·상황을 지어냈는지 (규칙 기반, 추가 LLM 호출 없음) ─────────
MAX_RELATIONS_IN_PROMPT = 5
# 어르신의 상황·감정을 짐작해 단정하는 말 ("불편하신가 봐요", "쥐가 나서 불편하신가 봅니다"). 일반 대화(generic·visit)에만 적용
GUESS_RE = re.compile(r"(?:신가|은가|는가|나|운가|인가|셨나|었나|았나)\s?(?:봐요|봅니다|보네요|보군요)")
GUESS_KINDS = {"generic", "visit"}


def ungrounded_relations(text: Optional[str], grounded) -> list:
    """로봇 문장의 가족·인물 관계 중 어르신이 말한(확인된) 관계로 뒷받침되지 않는 것. "딸" → "따님"은 근거 있음"""
    grounded = set(grounded or ())
    return sorted(rel for rel in relations_in(text or "", robot=True)
                  if not (RELATION_GROUNDED_BY.get(rel, {rel}) & grounded))


def grounding_issue(raw: Optional[str], grounded, kind: Optional[str]) -> Optional[str]:
    """근거 없는 관계 생성 → 'ungrounded', (일반 대화에서) 근거 없는 상황·감정 짐작 → 'guess'"""
    if ungrounded_relations(raw, grounded):
        return "ungrounded"
    if kind in GUESS_KINDS and raw and GUESS_RE.search(raw):
        return "guess"
    return None


def is_unclear_mark(raw: Optional[str]) -> bool:
    """LLM이 표시 문자열을 냈는지 (다른 말이 섞여도 표시가 있으면 그 응답은 쓰지 않는다)"""
    return bool(raw) and UNCLEAR_MARK in raw


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


# 로봇 질문 유형 (짧은 대답 해석용). 의문사가 있으면 개방형, 예/아니요 어미면 예/아니요형, 그 밖(부탁형 등)은 모호 → None
WH_WORDS = ("뭐", "무엇", "무슨", "어떤", "어떻", "어떠", "어디", "언제", "누구", "누가", "왜", "얼마", "몇", "어느")
REQUEST_MARKS = ("주시겠", "주실래", "주세요", "말씀해", "이야기해")
YESNO_END_RE = re.compile(r"(?:세요|셨어요|셨나요|나요|신가요|시죠|었어요|았어요|였어요)\?$")


def question_type(text: Optional[str]) -> Optional[str]:
    """출력된 로봇 문장의 마지막 문장이 질문이면 'open' | 'yesno', 질문이 아니거나 판단이 모호하면 None"""
    sents = split_sentences(text or "")
    last = sents[-1] if sents else ""
    if not last.endswith("?"):
        return None
    if any(w in last for w in WH_WORDS):
        return "open"
    if any(m in last for m in REQUEST_MARKS):
        return None
    return "yesno" if YESNO_END_RE.search(last) else None


def drop_questions(text: Optional[str]) -> Optional[str]:
    """질문 문장을 위치와 관계없이 모두 뺀 진술 부분 (경청 응답 전용). 남는 진술이 없으면 None.
    다른 경로는 기존 drop_last_question(끝 질문만)을 그대로 쓴다"""
    sents = [s for s in split_sentences(text or "") if not s.endswith("?")]
    return " ".join(sents) if sents else None


def drop_last_question(text: Optional[str]) -> Optional[str]:
    """끝의 질문 문장들을 뺀 진술 부분. 남는 진술 문장이 없으면 None (빈 응답을 만들지 않는다)"""
    sents = split_sentences(text or "")
    while sents and sents[-1].endswith("?"):
        sents.pop()
    return " ".join(sents) if sents else None


def _bigrams(s: str) -> set:
    s = re.sub(r"[^가-힣]", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


# 질문 반복 판정: 마지막 질문 문장이 최근 로봇 질문과 글자 두 개 묶음 기준으로 이만큼 겹치면 같은 질문으로 본다
REPEAT_QUESTION_OVERLAP = 0.55


def repeats_question(text: Optional[str], recent_ai=()) -> bool:
    """이번 문장의 마지막 질문이 최근 로봇 질문과 사실상 같은 질문인지 ("어떤 이야기를 하셨나요?" ↔ "어떤 이야기를 나누셨나요?")"""
    sents = split_sentences(text or "")
    if not sents or not sents[-1].endswith("?"):
        return False
    mine = _bigrams(sents[-1].split(",")[-1])          # "말씀해 주세요, 딸과 어떤 …?" → 쉼표 뒤 질문 부분만 비교
    for prev in recent_ai:
        for q in (s for s in split_sentences(prev or "") if s.endswith("?")):
            other = _bigrams(q.split(",")[-1])
            if mine and other and len(mine & other) / min(len(mine), len(other)) >= REPEAT_QUESTION_OVERLAP:
                return True
    return False


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
        self.kind: Optional[str] = None          # 원래 응답 종류 (근거 검사 범위)
        self.grounded: set = set()               # 요청 시점의 확인된 관계 (근거 검사 기준)
        self.budget: Optional[int] = None        # 요청 시점의 연속 질문 수 (None: 질문 예산 제외 대상)
        self.listening = False                   # 요청 시점에 경청 모드였는지 (응답의 질문 문장을 모두 뺀다)


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
