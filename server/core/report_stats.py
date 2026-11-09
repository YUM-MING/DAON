"""리포트 '대화 내용 통계' — 어르신이 말한 내용의 언급 기준 집계 (측정·진단이 아님, basis="user_said").

조회할 때마다 DB 에 저장된 dialogue 이벤트(어르신 발화)를 대화 모듈의 문장 분석기
modules/dialogue/src/text_analyzer.analyze (상태 없는 순수 함수) 로 다시 분석해 센다.
분석 결과는 DB 에 저장하지 않고, 외부 LLM 도 부르지 않는다. 응답에 원문은 넣지 않는다 (추출한 짧은 낱말만).

건수의 뜻 (세션 수가 아니라 발화·응답 건수다. 세션 식별자가 없으므로 세션 수는 제공하지 않는다)
    conversations[].user_msgs   중복 제거 뒤 어르신 발화 수 (STT 불명확·회상 질문 발화도 포함)
    conversations[].robot_msgs  저장된 로봇 응답 원 건수
    analyzed                    내용 통계에 들어간 어르신 발화 수
                                = user_msgs - stt_unclear - recall_question - analysis_error
                                (추출할 항목이 없는 일반 발화도 포함)
    skipped.stt_unclear / recall_question / analysis_error
                                발화는 있었지만 내용 통계에서 뺀 건수 (user_msgs 에는 포함)
    skipped.duplicate           1.5초 안에 같은 문장이 다시 저장된 건수 (user_msgs 에도 넣지 않음)
"""
from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Callable

from server.config import MODULES

DIALOGUE_SRC = MODULES / "dialogue" / "src"
VERSION = "report_stats/1"
DAYS_MIN, DAYS_MAX = 1, 30
DUP_WINDOW_SEC = 1.5            # 같은 정규화 문장이 이 시간 안에 다시 저장되면 1건 (대화 모듈 입력 방어막과 같은 기준)
FETCH_LIMIT = 20000             # 기간 안 대화 조회 상한
TOP_N = 5                       # wants 항목별 상위 개수

# 증상 표현(분석기 symptom_said) → 종류
SYMPTOM_KIND = {"아프다고": "pain", "쑤신다고": "ache", "저리다고": "numb", "결린다고": "stiff",
                "뻐근하다고": "stiff", "시리다고": "cold_ache", "욱신거린다고": "throb", "어지럽다고": "dizzy"}

# ---------- 원하는 것·좋아하는 음식 (보수적 규칙: 불확실하면 세지 않는다) ----------
ITEM_RE = re.compile(r"^[가-힣]{2,8}$")
VAGUE = {"뭐", "뭘", "뭔가", "무엇", "그거", "이거", "저거", "그것", "이것", "저것", "아무거나", "아무것", "거", "것",
         "어디", "거기", "여기", "저기", "누구", "아무", "아무데나", "다", "모두", "전부"}
PARTICLES = ("에서", "에도", "으로", "에게", "한테", "께서", "이랑", "랑", "하고", "을", "를", "이", "가", "은", "는",
             "도", "만", "에", "엘", "로", "좀")
FILLERS = {"참", "제일", "아주", "정말", "너무", "매우", "진짜", "좀", "많이", "꼭", "또", "다시", "자꾸", "괜히", "그냥",
           "요즘", "오늘", "사실", "막"}
RELATION_SUBJECT = re.compile(r"^(?:우리)?(?:딸|아들|며느리|사위|손자|손녀|손주|남편|아내|영감|할멈|친구|언니|오빠|누나|형|동생|"
                              r"엄마|아빠|어머니|아버지|할아버지|할머니|애들|자식|큰딸|작은딸|큰아들|작은아들)"
                              r"(?:이|가|은|는|도|께서|이도)$")
PAST_MARKS = ("예전", "옛날", "어릴", "어렸을", "젊었을", "젊을때", "왕년")
QUOTE_AFTER = re.compile(r"^(?:대|댔|래|랬|다고|다더|다며|다잖|잖|었|었다|었는데|었어|었지|던)")
NEG_MARKS = ("안먹고싶", "먹고싶지않", "먹고싶진않", "먹고싶지는않", "안좋아", "좋아하지않", "좋아하진않", "싫어", "싫다",
             "못먹", "별로")
FOOD_SUFFIX = ("죽", "국", "밥", "면", "떡", "빵", "탕", "찌개", "전", "김치", "과일", "나물", "반찬", "고기", "생선", "국수",
               "냉면", "라면", "만두", "수박", "사과", "커피", "우유", "차", "술", "부침개", "볶음", "구이", "조림", "무침")
WANT_VERBS = {"food": re.compile(r"먹고\s*싶"), "place": re.compile(r"가고\s*싶"), "person": re.compile(r"보고\s*싶")}
LIKE_VERBS = re.compile(r"좋아해|좋아합니다|즐겨\s*(?:먹|마시|드)")


def _norm(text: str) -> str:
    return re.sub(r"[^가-힣A-Za-z0-9]", "", text or "")


def _default_analyze():
    if str(DIALOGUE_SRC) not in sys.path:
        sys.path.insert(0, str(DIALOGUE_SRC))
    from text_analyzer import analyze  # noqa: E402
    return analyze


_analyze: Callable | None = None


def analyzer() -> Callable:
    """text_analyzer.analyze (처음 쓸 때 한 번 불러온다). 테스트는 이 모듈의 _analyze 를 바꿔 끼울 수 있다."""
    global _analyze
    if _analyze is None:
        _analyze = _default_analyze()
    return _analyze


def parse_days(raw) -> int:
    """days 쿼리 → 1~30 정수. 비었거나 숫자가 아니거나 범위를 벗어나면 ValueError (clamp 하지 않음)."""
    s = "" if raw is None else str(raw).strip()
    if not s.isdigit():
        raise ValueError("days 는 1~30 사이의 정수여야 합니다")
    n = int(s)
    if not DAYS_MIN <= n <= DAYS_MAX:
        raise ValueError("days 는 1~30 사이의 정수여야 합니다")
    return n


def period(days: int, now: float) -> tuple[str, str, float, float, list[str]]:
    """오늘 포함 최근 days 일 (서버 로컬 날짜 기준): (from, to, 시작 ts, 끝 ts, 날짜 목록)"""
    today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    first = today - timedelta(days=days - 1)
    names = [(first + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    return names[0], names[-1], first.timestamp(), (today + timedelta(days=1)).timestamp(), names


# ---------- 원하는 것 추출 ----------
def _strip_particle(word: str) -> str:
    for p in PARTICLES:
        if word.endswith(p) and len(word) > len(p):
            return word[: -len(p)]
    return word


def _item_before(words: list[str], idx: int, head: str) -> str | None:
    """words[idx] 안의 동사 앞부분(붙여 쓴 경우) 또는 그 앞 낱말(부사는 건너뜀)에서 대상 낱말을 뽑는다"""
    prefix = words[idx][: words[idx].find(head)] if head in words[idx] else ""
    if prefix:
        cand = prefix
    else:
        j = idx - 1
        while j >= 0 and words[j] in FILLERS:
            j -= 1
        if j < 0:
            return None
        cand = words[j]
    item = _strip_particle(cand)
    if item in VAGUE or not ITEM_RE.match(item):
        return None
    return item


def _other_subject(words: list[str]) -> bool:
    """관계 낱말 + 주격 조사("우리 딸은 …", "딸이 먹고 싶대"): 다른 사람의 이야기 (음식·장소에만 적용.
    "아들이 보고 싶어"의 '아들이'는 보고 싶은 대상이므로 사람 범주에는 적용하지 않는다)"""
    return any(RELATION_SUBJECT.match(w) for w in words)


def _context_blocks(words: list[str], compact: str, sig) -> bool:
    """시제·부정·인용·복약·위험 맥락: 하나라도 있으면 세지 않는다"""
    if any(m in compact for m in PAST_MARKS) or any(m in compact for m in NEG_MARKS) or "잖아" in compact:
        return True
    if getattr(sig, "medicine", False) or getattr(sig, "medicine_state", "") or getattr(sig, "self_harm", False):
        return True
    return getattr(sig, "distress", "none") == "severe"


def extract_wants(text: str, sig) -> dict:
    """{"food": item, "like_food": item, "place": item, "person": item} 중 찾은 것만 (발화당 종류별 최대 1개)"""
    words = re.findall(r"[가-힣A-Za-z0-9]+", text or "")
    compact = _norm(text)
    if not words or _context_blocks(words, compact, sig):
        return {}
    other = _other_subject(words)
    out = {}
    for cat, verb in WANT_VERBS.items():
        m = verb.search(text)
        if not m or (other and cat != "person"):
            continue
        after = re.sub(r"\s+", "", text[m.end():])
        if QUOTE_AFTER.match(after):                    # "먹고 싶대", "먹고 싶다고 했잖아", "먹고 싶었어"
            continue
        head = {"food": "먹고", "place": "가고", "person": "보고"}[cat]
        idx = next((i for i, w in enumerate(words) if head in w), None)
        item = _item_before(words, idx, head) if idx is not None else None
        if item:
            out[cat] = item
    m = LIKE_VERBS.search(text)
    if m and not other:
        after = re.sub(r"\s+", "", text[m.end():])
        if not QUOTE_AFTER.match(after) and not re.search(r"좋아(?:했|한대|한다고|하셔|하시)", compact):
            head = "좋아" if "좋아" in m.group() else "즐겨"
            idx = next((i for i, w in enumerate(words) if w.startswith(head) or head in w), None)
            item = _item_before(words, idx, head) if idx is not None else None
            food_ctx = bool(re.search(r"먹|마시|드", m.group()))
            if item and (food_ctx or item.endswith(FOOD_SUFFIX)):
                out["like_food"] = item
    return out


# ---------- 집계 ----------
def compute(db, hid: int, days: int, now: float, analyze: Callable | None = None) -> dict:
    analyze = analyze or analyzer()
    d_from, d_to, start, end, names = period(days, now)
    rows = db.report_dialogue_events(hid, start, end, FETCH_LIMIT)

    per_day = {d: {"day": d, "user_msgs": 0, "robot_msgs": 0} for d in names}
    skipped = {"duplicate": 0, "stt_unclear": 0, "recall_question": 0, "analysis_error": 0}
    sleep = {"bad": 0, "good": 0, "tired": 0}
    meal = {"done": 0, "not": 0, "ask": 0, "memory": 0}
    symptoms: dict = {}
    emotion: Counter = Counter()
    topics: Counter = Counter()
    wants: dict = defaultdict(Counter)
    analyzed = 0
    last_user: tuple[str, float] | None = None

    for r in rows:
        data = r.get("data") or {}
        day = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d")
        if day not in per_day:
            continue
        if data.get("who") == "robot":
            per_day[day]["robot_msgs"] += 1             # 로봇 발화는 건수만 (분석하지 않음)
            continue
        if data.get("who") != "user":
            continue
        text = str(data.get("text") or "").strip()
        norm = _norm(text)
        if last_user is not None and norm and norm == last_user[0] and 0 <= r["ts"] - last_user[1] <= DUP_WINDOW_SEC:
            skipped["duplicate"] += 1                   # 같은 문장 재저장: user_msgs 에도 넣지 않음
            continue
        last_user = (norm, r["ts"])
        per_day[day]["user_msgs"] += 1
        try:
            sig = analyze(text)
            if sig.stt_unclear:
                skipped["stt_unclear"] += 1
                continue
            if sig.recall_question:
                skipped["recall_question"] += 1
                continue
            found = _collect(sig, text)
        except Exception:                               # 이 발화만 건너뛴다 (전체 집계는 계속)
            skipped["analysis_error"] += 1
            continue
        analyzed += 1
        for k in found["sleep"]:
            sleep[k] += 1
        for k in found["meal"]:
            meal[k] += 1
        for (part, kind), danger in found["symptoms"].items():
            s = symptoms.setdefault((part, kind), {"part": part, "kind": kind, "count": 0, "days": set(), "danger": False})
            s["count"] += 1
            s["days"].add(day)
            s["danger"] = s["danger"] or danger
        emotion.update(found["emotion"])
        topics.update(found["topics"])
        for cat, item in found["wants"].items():
            wants[cat][item] += 1

    sym_list = sorted(({**s, "days": len(s["days"])} for s in symptoms.values()),
                      key=lambda s: (-s["count"], s["part"] or "", s["kind"]))
    return {
        "days": days, "from": d_from, "to": d_to,
        "analyzed": analyzed,
        "conversations": [per_day[d] for d in names],
        "sleep": sleep, "meal": meal, "symptoms": sym_list,
        "emotion_text": dict(emotion), "topics": dict(topics),
        "wants": {cat: [{"item": i, "count": n} for i, n in wants[cat].most_common(TOP_N)]
                  for cat in ("food", "like_food", "place", "person")},
        "basis": "user_said", "version": VERSION, "skipped": skipped,
    }


def _collect(sig, text: str) -> dict:
    """발화 하나에서 카테고리별 항목 (같은 카테고리 값은 발화당 1회)"""
    sleep = set()
    if sig.sleep_bad:
        sleep.add("bad")
    elif sig.sleep_well and not sig.goodnight:          # "잘 자"(잘 자라는 인사)는 수면 언급이 아니다
        sleep.add("good")
    if sig.tired:
        sleep.add("tired")
    meal = set()
    if sig.meal_state in ("done", "not", "ask"):
        meal.add(sig.meal_state)
    if sig.meal_memory:
        meal.add("memory")
    syms = {}
    if sig.symptom_said:
        kind = SYMPTOM_KIND.get(sig.symptom_said, sig.symptom_said)
        danger = sig.distress == "severe" or bool(sig.danger_kinds)
        for part in (sig.symptom_parts or [None]):
            syms[(part, kind)] = syms.get((part, kind), False) or danger
    emotion = set()
    if sig.emotion_kind:
        emotion.add(sig.emotion_kind)
    if sig.grief:
        emotion.add("grief")
    return {"sleep": sleep, "meal": meal, "symptoms": syms, "emotion": emotion,
            "topics": set(sig.topics or []), "wants": extract_wants(text, sig)}
