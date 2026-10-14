"""① 입력 분석: STT 텍스트 → 의미 신호 (규칙 기반, 부정문/단어 경계 고려)"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

# --- 위험 ---
SEVERE = ("도와줘", "도와주세요", "살려줘", "살려주", "쓰러졌", "못일어나", "숨이안쉬", "숨을못",
          "숨쉬기힘", "숨쉬기가힘", "숨이막", "의식이")   # 가슴 통증은 CHEST로 따로 판단
# 위급 표현처럼 보이지만 감사/일반 문장인 경우는 판정 전에 제거 ("도와줘서 고마워", "살려주셔서")
NOT_SEVERE = ("도와줘서", "도와주셔서", "살려줘서", "살려주셔서", "도와주신", "살려주신", "도와주셨", "살려주셨")
SEVERE_FUZZY = ("도와줘", "도와주세요", "살려줘", "살려주세요")     # STT 오인식 대비 유사 매칭
OK_FUZZY = ("괜찮아", "괜찮아요", "괜찮습니다", "괜찮은데")
MILD = ("아파", "아프", "어지러", "어지럽", "다쳤", "피가", "피나", "못움직", "힘이없",
        "안괜찮", "괜찮지않", "괜찮지가않", "괜찮지못", "숨이차", "숨차", "숨이너무차", "숨이많이차", "숨이좀차")
NOT_PAIN = ("안아파", "아프지않", "아프지는않", "아프진않", "안아프")
NOT_SYMPTOM = ("피가안나", "피는안나", "피도안나", "피안나", "피가나지않", "피는나지않")   # "피가 안 멈춰"는 위험 유지
# 가슴 통증: 신체 증상이면 위급, 정서적 맥락("그 얘기 들으니")이 분명하면 마음의 아픔으로 본다
CHEST = re.compile(r"가슴이?(?:너무|많이|좀|정말|진짜|자꾸|계속|갑자기)?(?:아파|아프)")
HEART = re.compile(r"마음이?(?:너무|많이|좀|정말|진짜|참)?(?:아파|아프)")
# 흉부 답답함·압박감: 신체 증상이면 안전 확인(mild, '숨이 차'와 같은 수준). 정서 단서 규칙은 가슴 통증과 동일
CHEST_TIGHT = re.compile(r"가슴이?(?:너무|많이|좀|정말|진짜|자꾸|계속|갑자기|꽉)?(?:답답|막히|막힌|막혀|조이|조여|조인|짓눌|눌리|눌려)")
# '얘기/소식' 같은 명사만으로는 정서로 보지 않는다: 듣고·생각하는 등 마음이 움직인 맥락이 있어야 한다
EMOTION_CUES = ("들으니", "들으면", "듣고", "듣는데", "들어보니", "생각하면", "생각만해도", "생각나", "생각이나",
                "보니", "안쓰러", "짠해", "불쌍", "그리워", "보고싶", "돌아가신", "떠난", "서운", "섭섭")
# 갑자기 생긴 통증·하던 일 중의 통증·동반 증상은 정서 단서가 있어도 신체 증상으로 본다 (안전 우선)
PHYSICAL_CUES = ("갑자기", "다가", "하는데", "하는중", "숨", "땀", "쥐어", "조여", "찌르", "뻐근")
# 증상 문자열을 우연히 포함한 일반 단어 ("아파트"의 "아파")
NOT_SYMPTOM_WORDS = ("아파트", "아프리카", "피나는노력")
SELF_HARM = ("죽고싶", "죽어버리", "죽어버릴", "죽을래", "살기싫", "살고싶지않", "사라지고싶", "그만살",
             "목숨을끊", "자살하", "자살할", "자살생각")
NOT_SELF_HARM = ("죽고싶지않", "죽고싶진않", "죽고싶지는않")
# "배고파 죽을래"처럼 상태 표현 바로 뒤의 '죽을래'는 과장 표현 ("힘들어서 죽을래"처럼 이유가 붙으면 위기로 유지)
HYPERBOLE_BEFORE = ("배고파", "힘들어", "졸려", "더워", "추워", "심심해", "지루해", "웃겨", "피곤해", "귀찮아",
                    "바빠", "답답해", "짜증나", "궁금해", "부러워", "배불러", "목말라", "가려워", "무서워", "창피해")
FALL_REPORT = ("넘어졌", "미끄러졌", "쓰러질뻔")       # '떨어졌'은 _fell_off에서 무엇이 떨어졌는지 보고 판단
# "기운이 (좀) 떨어졌어"처럼 주어와 '떨어졌' 사이에 올 수 있는 말
FELL_SKIP = ("좀", "많이", "너무", "확", "다", "조금", "요즘", "갑자기", "또", "완전", "자꾸")
# 말하는 사람 자신이 주어면 '~가'여도 낙상 ("내가 떨어졌어")
FIRST_PERSON = ("내가", "제가", "나는", "저는", "나도", "저도", "난", "전", "나", "저")
# 과거 일을 얘기하는 말이면 지금 넘어진 게 아니므로 낙상 보고로 보지 않는다 ("어제 넘어졌었어")
PAST_MARKERS = ("어제", "그제", "그저께", "지난", "예전", "옛날", "작년", "며칠전", "오래전", "전에")
# 단, 최근 일이면 지금 안전 확인이 필요하다 ("방금 전에", "조금 전에", "10분 전에")
RECENT_MARKERS = ("방금", "조금전", "좀전", "아까", "금방", "분전", "시간전")

# --- 정서 ---
NEGATIVE = ("우울", "외로", "외롭", "쓸쓸", "힘들", "슬퍼", "슬프", "속상", "보고싶", "무서", "무섭",
            "불안", "답답", "허전", "서러", "짜증", "화나", "걱정돼", "걱정되", "걱정이",
            "아무도없", "사람이없", "의지할")
NEGATIVE_FIXED = ("기분이별로", "별로야", "별로네", "별로다", "별로예요", "별로에요", "안좋", "좋지않")
POSITIVE = ("좋아", "좋네", "행복", "기뻐", "즐거", "신나", "재밌", "편안", "든든")
# 함께 기뻐할 만한 분명한 기쁨 표현 ("날씨가 좋네" 같은 약한 긍정은 제외, "안 기뻐"는 부정)
JOY = ("기뻐", "기쁘", "기쁜", "행복", "신나", "신난", "즐거", "기분좋", "기분이좋")
# 부정 표현 앞뒤의 부정어: "안 외로워", "외롭지 않아" → 부정 감정 아님
NEG_AFTER = ("지않", "지는않", "진않", "지도않", "지가않", "하지않", "하지는않", "하진않")

# --- 일상 화제 ---
GREETING = ("안녕", "좋은아침", "굿모닝", "잘잤어")
THANKS = ("고마워", "고맙", "감사")
# 나아졌다는 말: 직전 표정이 슬펐어도 공감으로 되돌리지 않고 평상시 응답 ("안 좋아졌어"처럼 앞의 '안'은 부정)
RECOVERY = ("좋아졌", "나아졌", "괜찮아졌", "풀렸", "편해졌", "가벼워졌")
# 과거형 긍정: "그래도 오늘은 좋았어" — 회복 신호로도 쓰여 슬픈 표정 직후에도 공감으로 되돌리지 않는다
PAST_POSITIVE = ("좋았", "좋더라", "즐거웠", "재밌었", "재미있었", "신났", "행복했", "기뻤")
GOODNIGHT = ("잘자", "잘게", "주무실게", "졸려")
ABOUT_AI = ("너누구", "너는누구", "이름이뭐", "이름이뭔", "너뭐야", "뭐하는로봇", "뭐하는애")
MEAL = ("밥", "식사", "점심", "저녁", "아침", "배고", "먹었")
# 식사 상태: 무엇을 먹을지 묻는 말 / 아직 안 먹은 말 / 먹었다는 말 (방금 한 말을 되묻지 않기 위해)
MEAL_ASK = ("먹을까", "먹지", "먹을지", "뭐먹", "뭘먹", "뭐해먹")
MEAL_NOT = ("안먹", "못먹", "굶", "배고", "거르", "걸렀")
MEAL_DONE = ("먹었", "식사했", "식사를했")
MEDICINE = ("약먹", "약을먹", "약은먹", "약도먹", "약챙겨", "약을드", "약복용")   # "약 먹었어"는 식사가 아니라 건강 이야기
# 고인에 대한 그리움: 그리움 표현 + 고인 표현이 함께 있을 때만 (안전·위기 판정과 무관, 공감 문장만 바뀜)
MISSING = ("보고싶", "그리워", "그립", "생각나", "생각이나")
DECEASED = ("돌아가신", "돌아가셨", "영감", "할멈", "먼저간", "먼저가신", "세상떠", "하늘나라", "떠나보낸")
# 잘 잔다는 말: 잠 이야기를 다시 물은 직후의 대답에만 쓴다 ("잘 자"는 평소엔 잘 자라는 인사)
SLEEP_WELL = ("잘자", "푹자", "잘잔", "잘잤", "푹잤", "잘주무", "푹주무")
# 질문형: 시간 / 날씨(정보가 없으므로 솔직하게) / 로봇이 할 수 있는 것
TIME_ASK = ("몇시", "지금시간", "시간이몇", "시간좀알려", "시간알려")
WEATHER_ASK_MARKS = ("어때", "어떨", "어떻", "알려")         # "날씨" + 이 표현 = 날씨를 묻는 말
ABILITY_ASK = ("뭐할줄", "뭘할줄", "뭐해줄", "뭘해줄", "뭐해줘", "뭘해줘", "할줄알아", "뭐할수있", "뭘할수있")
# 잠을 잘 못 잔다는 말 (되묻지 않고 받아준다. 의료 조언은 하지 않음)
SLEEP_BAD = ("잠을못", "잠을잘못", "못잤", "못자", "잠이안와", "잠이안오", "잠이안와서", "잠을설", "설쳤", "뒤척")
# 피곤함 ("힘들어"는 NEGATIVE라 기존 공감 경로, "힘이 없어"는 MILD라 안전 확인 → 여기서는 다루지 않음)
TIRED = ("피곤", "지쳤", "지친", "지쳐", "기운이없", "기운없", "고단", "나른")
# 이미 말한 불편감 (통증 '아파'는 MILD로 안전 확인 → 여기서는 다루지 않음)
DISCOMFORT = ("쑤셔", "쑤시", "저려", "저리", "결려", "결리", "뻐근", "시려", "시리", "욱신")
BOREDOM = ("심심", "할일없", "지루")
WAIT = ("잠깐", "잠시", "기다려")
INTENSE = ("너무", "정말", "진짜", "많이", "죽겠", "견디기", "계속")
OK_TOKENS = {"응", "네", "예", "그래", "그럼", "살아있어"}   # 단어 단위로만 ("이상하네"의 "네" 오탐 방지)
TOPICS = {
    "lonely": ("외로", "외롭", "쓸쓸", "혼자", "허전", "아무도없", "사람이없"),
    "family": ("아들", "딸", "손주", "손자", "가족", "며느리", "사위", "자식", "보고싶"),
    "health": ("아프", "아파", "허리", "무릎", "다리", "쑤시", "쑤셔", "저려", "약먹", "병원", "혈압", "당뇨"),
    "sleep": ("잠이", "잠을", "잠자", "못잤", "불면", "뒤척"),
    "weather": ("날씨", "추워", "더워", "비가", "눈이"),
}


@dataclass
class TextSignals:
    empty: bool = True
    distress: str = "none"          # none | mild | severe
    self_harm: bool = False         # 극단적 선택 암시
    ok: bool = False                # "괜찮다"는 긍정 응답
    ok_explicit: bool = False       # "괜찮아"처럼 명시적인 안전 응답 ("응/네"만 있으면 False)
    reports_fall: bool = False      # "방금 넘어졌어" 같은 사실 보고
    negative: bool = False
    positive: bool = False
    intense: bool = False           # "너무/정말" 등 강조
    greeting: bool = False
    thanks: bool = False
    goodnight: bool = False
    about_ai: bool = False
    meal: bool = False
    boredom: bool = False
    wait: bool = False
    denies_negative: bool = False   # "안 외로워"처럼 부정 감정을 부인
    recovered: bool = False         # "좋아졌어", "괜찮아졌어"처럼 나아졌다는 말
    joy: bool = False               # "너무 기뻐", "행복해"처럼 분명한 기쁨 표현
    past_positive: bool = False     # "오늘은 좋았어", "재밌었어"처럼 지난 일이 좋았다는 말
    meal_state: str = ""            # ask(뭐 먹을까) | not(안 먹었어·배고파) | done(먹었어) | ""
    discomfort: bool = False        # "무릎이 쑤셔"처럼 이미 말한 불편감
    tired: bool = False             # "피곤하네", "지쳤어"처럼 피곤하다는 말
    medicine: bool = False          # "약 먹었어", "약 먹을 시간이야"
    sleep_bad: bool = False         # "잠을 못 자", "잠이 안 와", "잠을 설쳤어"
    sleep_well: bool = False        # "요즘은 잘 자", "푹 잤어" ("잘 못 자"는 아님)
    grief: bool = False             # "돌아가신 영감이 보고 싶네", "할멈이 그리워"
    question: str = ""              # time(몇 시야) | weather(날씨 어때) | abilities(뭐 할 줄 알아) | ""
    topics: list = field(default_factory=list)


def _contains(compact: str, kws, negatable: bool = True) -> bool:
    """키워드 포함 여부. negatable이면 '안 + 키워드', '키워드 + 지 않'은 부정으로 보고 제외한다."""
    for kw in kws:
        start = 0
        while (i := compact.find(kw, start)) >= 0:
            start = i + 1
            if negatable:
                after = compact[i + len(kw):]
                if (i > 0 and compact[i - 1] == "안") or any(after.startswith(n) for n in NEG_AFTER):
                    continue
            return True
    return False


def _jamo(s: str) -> str:
    """한글 음절을 자모로 분해 (오인식 '괜찬아'와 '괜찮아'를 가깝게 보기 위함)"""
    out = []
    for ch in s:
        code = ord(ch) - 0xAC00
        if 0 <= code < 11172:
            out.append(chr(0x1100 + code // 588))
            out.append(chr(0x1161 + (code % 588) // 28))
            if code % 28:
                out.append(chr(0x11A7 + code % 28))
        else:
            out.append(ch)
    return "".join(out)


def _similar(token: str, targets, threshold: float) -> bool:
    j = _jamo(token)
    return any(difflib.SequenceMatcher(None, j, _jamo(t)).ratio() >= threshold for t in targets)


def _fuzzy_ok(tokens) -> bool:
    for i, t in enumerate(tokens):
        if len(t) < 3 or t[0] in "안못" or (i > 0 and tokens[i - 1] in ("안", "못")):
            continue                              # 부정문은 유사 매칭에서 제외
        if _similar(t, OK_FUZZY, 0.8):
            return True
    return False


def _has_past(token: str) -> bool:
    """받침 ㅆ이 있는 음절(줬·셨·었·았)이 있으면 과거/완료형 ("도와줬어", "살려주셨어")"""
    return any(0 <= ord(ch) - 0xAC00 < 11172 and (ord(ch) - 0xAC00) % 28 == 20 for ch in token)


def _fuzzy_severe(tokens, compact: str = "") -> bool:
    # 오인식 보정은 현재형 요청에만: 감사("~서")·과거형("~줬어")까지 '도와줘'로 넓히지 않는다
    # "시간/날씨 알려줘"처럼 정보를 묻는 문장의 '알려~'만 보정에서 뺀다 (단독 "알려줘", "나 좀 알려줘"는 그대로 보정)
    info_ask = len(tokens) > 1 and _question(compact) in ("time", "weather")
    return any(len(t) >= 3 and not t.endswith("서") and not _has_past(t)
               and not (info_ask and t.startswith("알려"))
               and _similar(t, SEVERE_FUZZY, 0.8) for t in tokens)


def _question(compact: str) -> str:
    if any(k in compact for k in TIME_ASK):
        return "time"
    if "날씨" in compact and any(k in compact for k in WEATHER_ASK_MARKS):
        return "weather"
    if any(k in compact for k in ABILITY_ASK):
        return "abilities"
    return ""


def _meal_state(compact: str) -> str:
    if any(k in compact for k in MEDICINE):
        return ""
    if any(k in compact for k in MEAL_ASK):
        return "ask"
    if any(k in compact for k in MEAL_NOT):
        return "not"
    if any(k in compact for k in MEAL_DONE):
        return "done"
    return ""


def _fell_off(tokens) -> bool:
    """'떨어졌'이 사람의 낙상인지: '입맛이/기운이 떨어졌어'처럼 주어(~이/가) 바로 뒤면 상태 변화로 본다.
    '침대에서 떨어졌어', '방금 떨어졌어', '떨어졌어'(단독)는 낙상으로 본다. '안 떨어졌어'는 부정."""
    for i, t in enumerate(tokens):
        if not t.startswith("떨어졌"):
            continue
        j = i - 1
        while j >= 0 and tokens[j] in FELL_SKIP:
            j -= 1
        prev = tokens[j] if j >= 0 else ""
        if prev == "안" or (prev not in FIRST_PERSON and prev.endswith(("이", "가"))):
            continue
        return True
    return False


def _aches(view: str, pattern) -> list:
    """'가슴이 (너무) 아파' 같은 표현의 위치. '아프지 않아'처럼 뒤에서 부정되면 제외"""
    return [m.span() for m in pattern.finditer(view)
            if not any(view[m.end():].startswith(n) for n in NEG_AFTER)]


def _blank(view: str, spans) -> str:
    for a, b in spans:
        view = view[:a] + "|" * (b - a) + view[b:]
    return view


def analyze(text: str) -> TextSignals:
    text = (text or "").strip()
    if not text:
        return TextSignals()
    compact = re.sub(r"\s+", "", text)
    tokens = re.findall(r"[가-힣A-Za-z0-9]+", text)

    word_view = compact
    for p in NOT_SYMPTOM_WORDS:
        word_view = word_view.replace(p, "|" * len(p))
    chest, heart, tight = _aches(word_view, CHEST), _aches(word_view, HEART), _aches(word_view, CHEST_TIGHT)
    emotional_cue = any(c in compact for c in EMOTION_CUES) and not any(c in compact for c in PHYSICAL_CUES)
    emotional_chest = bool(chest) and emotional_cue
    chest_tight = bool(tight) and not emotional_cue       # 정서 맥락이 아닌 흉부 답답함·압박감
    heartache = bool(heart) or emotional_chest or (bool(tight) and emotional_cue)  # 위험이 아니라 부정 감정
    emotional_view = _blank(word_view, heart + (chest if emotional_chest else []))
    pain_view = emotional_view
    for p in NOT_PAIN + NOT_SEVERE + NOT_SYMPTOM:
        pain_view = pain_view.replace(p, "")
    harm_view = compact
    for p in NOT_SELF_HARM + tuple(h + "죽을래" for h in HYPERBOLE_BEFORE):
        harm_view = harm_view.replace(p, "")

    self_harm = any(k in harm_view for k in SELF_HARM)
    if (chest and not emotional_chest) or _contains(pain_view, SEVERE):
        distress = "severe"
    elif chest_tight or _contains(pain_view, MILD):       # "안 다쳤어", "어지럽지 않아"는 부정으로 제외
        distress = "mild"
    else:
        distress = "none"

    if distress == "none" and not self_harm and _fuzzy_severe(tokens, compact):
        distress = "severe"                       # "도와저" 같은 오인식도 위급으로 본다

    explicit_ok = (any(t.startswith("괜찮") for t in tokens)
                   or "이상없" in compact or "문제없" in compact
                   or _fuzzy_ok(tokens))
    ok = distress == "none" and not self_harm and (any(t in OK_TOKENS for t in tokens) or explicit_ok)

    negative = (_contains(compact, NEGATIVE) or _contains(compact, NEGATIVE_FIXED, negatable=False)
                or heartache)
    past_positive = (not negative) and _contains(compact, PAST_POSITIVE)
    positive = (not negative) and (_contains(compact, POSITIVE) or past_positive)
    denies = (not negative) and any(k in compact for k in NEGATIVE) and not _contains(compact, NEGATIVE)
    return TextSignals(
        empty=False, distress=distress, self_harm=self_harm, ok=ok, ok_explicit=ok and explicit_ok,
        reports_fall=(_contains(compact, FALL_REPORT) or _fell_off(tokens)) and (   # "안 넘어졌어"는 부정
            any(k in compact for k in RECENT_MARKERS) or not any(k in compact for k in PAST_MARKERS)),
        negative=negative, positive=positive,
        intense=any(k in compact for k in INTENSE),
        greeting=any(k in compact for k in GREETING),
        thanks=any(k in compact for k in THANKS),
        recovered=(not negative) and (_contains(compact, RECOVERY) or past_positive),
        past_positive=past_positive,
        joy=(not negative) and _contains(compact, JOY),
        meal_state=_meal_state(compact),
        discomfort=_contains(compact, DISCOMFORT),
        tired=_contains(compact, TIRED),
        medicine=any(k in compact for k in MEDICINE),
        sleep_bad=any(k in compact for k in SLEEP_BAD),
        question=_question(compact),
        sleep_well=any(k in compact for k in SLEEP_WELL),
        grief=any(k in compact for k in MISSING) and any(k in compact for k in DECEASED),
        goodnight=any(k in compact for k in GOODNIGHT),
        about_ai=any(k in compact for k in ABOUT_AI),
        meal=any(k in compact for k in MEAL),
        boredom=any(k in compact for k in BOREDOM),
        wait=any(k in compact for k in WAIT), denies_negative=denies,
        topics=[n for n, kws in TOPICS.items() if any(k in emotional_view for k in kws)])   # '마음이 아파'는 건강 화제 아님