"""리포트 '이번 주 상태 요약' — 규칙 기반 주의 신호 탐지 (GET /api/homes/{id}/report/insights).

미래 상태를 예측하는 모델이 아니라, 현재 기간(최근 days 일)에 설정된 규칙에 걸리는 신호가 있는지 찾아
보여 준다. 직전 같은 길이 기간은 기록이 충분할 때만 수치로 비교한다(악화·호전 같은 해석은 하지 않는다).
진단·원인 단정·예측을 하지 않고, 외부 LLM 없이 규칙과 문장 템플릿만 쓴다.

근거 데이터
    user_said      어르신 발화를 report_stats.aggregate 로 다시 분석한 언급 수 (발화·카테고리당 1회)
    camera_events  카메라 표정 '변화 이벤트' (표정이 바뀔 때만 기록됨 → 시간 비율·실제 감정 상태로 해석하지 않음)
    alert_history  알림 발생 사실 (신호·종합 단계에는 넣지 않고 alerts 필드로만)
"""
from __future__ import annotations

from typing import Callable

from server.core import report_stats as rs

VERSION = "report_insights/2"
BASIS = "user_said+camera_events+alert_history"
DISCLAIMER = "참고용 주의 신호이며 진단이나 예측이 아닙니다. 임계값은 검증되지 않은 개발용 초기값입니다."
ALERT_NOTE = "알림 유형별 중요도 기준이 확인되기 전까지 종합 단계에는 반영하지 않습니다."

# ======================================================================
# 임계값 — 의학적으로 검증된 기준이 아니라 1차 구현을 위한 '검증되지 않은 개발용 초기값'이다.
# 실제 사용 데이터로 오탐·미탐을 확인한 뒤 조정해야 한다.
# ======================================================================
SLEEP_BAD_WARN = 3          # 수면 부족 언급 ≥3회 → 주의
SLEEP_BAD_NOTE = 2          # 수면 부족 언급 2회 → 참고
SYMPTOM_DAYS_WARN = 3       # 같은 부위를 ≥3일 언급 → 주의
SYMPTOM_DAYS_NOTE = 2       # 같은 부위를 2일 언급 → 참고
EMOTION_WARN = 3            # 마음이 힘든 표현(lonely·sad·anxious·lethargic·grief) 합계 ≥3 → 주의
EMOTION_NOTE = 2            # 합계 2 → 참고
LETHARGIC_WARN = 2          # 무기력 표현 단독 ≥2 → 주의
MEAL_NOT_WARN = 2           # '안 먹었다' 언급 ≥2회 → 주의
MEAL_NOT_NOTE = 1           # 1회 → 참고 (단발이라 종합 단계에는 반영하지 않음, SINGLE_MENTION 참고)
VOLUME_MIN_PREV = 6         # 직전 기간 어르신 발화가 이 이상일 때만 대화량 비교
VOLUME_DROP_RATIO = 0.5     # 현재 < 직전 × 0.5 → 참고 (대화량 감소)
CAMERA_MIN_EVENTS = 10      # 표정 변화 이벤트가 이 이상일 때만
CAMERA_NEG_RATIO = 0.5      # 부정 표정 변화 이벤트 비중 ≥50% → 참고 (참고 전용)
MIN_ANALYZED = 6            # 종합 판단에 필요한 최소 분석 발화 수
MIN_DAYS_WITH_DATA = 2      # 종합 판단에 필요한 최소 '분석된 발화가 있는 날' 수 (하루치로 주간 경향을 보지 않음)
COMPARE_MIN_PREV_ANALYZED = 6   # 직전 기간 분석 발화가 이 이상일 때만 직전 기간과 비교
SINGLE_MENTION = 1          # 발화 횟수 기반 '참고' 신호가 이 횟수뿐이면 표시만 하고 종합 단계에는 반영하지 않음
OVERALL_WARN_FOR_CHECK = 3  # 주의 신호 ≥3개 → 확인 권장
OVERALL_NOTE_FOR_WATCH = 2  # (단계에 영향 있는) 참고 신호 ≥2개 → 관찰 필요

CAMERA_NEGATIVE = ("sad", "angry", "fear")          # 카메라 표정 라벨 중 부정 표정 (fear = 불안(표정))
LEVELS = ("특이 신호 없음", "관찰 필요", "확인 권장", "판단 보류")
NOTE, WARN, CHECK = "참고", "주의", "확인"

SYMPTOM_NOUN = {"pain": "통증", "ache": "쑤심", "numb": "저림", "stiff": "뻐근함", "cold_ache": "시림",
                "throb": "욱신거림", "dizzy": "어지러움"}
EMOTION_KO = {"lonely": "외로움", "sad": "슬픔", "anxious": "불안", "lethargic": "무기력", "grief": "그리움"}


def _eul(word: str) -> str:
    """목적격 조사: 받침 있으면 '을', 없으면 '를'"""
    c = word[-1:] if word else ""
    return "을" if "가" <= c <= "힣" and (ord(c) - 0xAC00) % 28 else "를"


def _signal(key, level, title, detail, current, previous, affects=True) -> dict:
    return {"key": key, "level": level, "title": title, "detail": detail, "current": current, "previous": previous,
            "affects_level": affects}


def _count_affects(level: str, count: int) -> bool:
    """발화 횟수 기반 신호: '참고'인데 언급이 1회뿐이면 단계에 반영하지 않는다 ('주의'·'확인'은 항상 반영)"""
    return not (level == NOTE and count <= SINGLE_MENTION)


def _sum(daily: list[dict], k: str) -> int:
    return sum(d[k] for d in daily)


def _ndays(daily: list[dict], k: str) -> int:
    return sum(1 for d in daily if d[k] > 0)


def _over(nd: int) -> str:
    return "하루에" if nd == 1 else f"{nd}일에 걸쳐"


def _consecutive(daily: list[dict], k: str) -> bool:
    return any(daily[i][k] > 0 and daily[i + 1][k] > 0 for i in range(len(daily) - 1))


def _part_days(symptoms: dict) -> dict:
    """{부위: {"days": set, "kinds": [..]}} — 부위가 없는 언급(None)은 '같은 부위' 규칙에서 뺀다"""
    out: dict = {}
    for (part, kind), s in symptoms.items():
        if not part:
            continue
        p = out.setdefault(part, {"days": set(), "kinds": []})
        p["days"] |= s["days"]
        if kind not in p["kinds"]:
            p["kinds"].append(kind)
    return out


def _camera(events: list[dict]) -> dict:
    labels = [str((e.get("data") or {}).get("label") or "") for e in events]
    labels = [x for x in labels if x]
    return {"events": len(labels), "negative_events": sum(x in CAMERA_NEGATIVE for x in labels)}


def comparison(prev: dict, days: int) -> dict:
    ok = prev["analyzed"] >= COMPARE_MIN_PREV_ANALYZED
    return {"available": ok, "previous_analyzed": prev["analyzed"], "previous_days_with_data": prev["analyzed_days"],
            "reason": "" if ok else f"직전 {days}일에 비교할 만큼의 기록이 없습니다"}


# ---------------- 신호 ----------------
def detect(cur: dict, prev: dict, cam: dict, cam_prev: dict, days: int,
           compare: bool = True) -> tuple[list[dict], list[str]]:
    """(signals, recommendations). cur/prev 는 report_stats.aggregate 결과.
    compare=False 면 previous 는 None, 직전 기간 비교 문장·대화량 감소 신호를 만들지 않는다."""
    span, before = f"최근 {days}일간", f"직전 {days}일"
    sig, rec = [], []
    daily, pdaily = cur["daily"], prev["daily"]
    pv = (lambda x: x) if compare else (lambda x: None)

    def vs(p, n, unit):   # 수치만 비교 (해석 표현 없음)
        return f"(직전 {days}일 {p}{unit} → 이번 {days}일 {n}{unit})" if compare else ""

    # 1. 수면 부족
    n, pn = _sum(daily, "sleep_bad"), _sum(pdaily, "sleep_bad")
    if n >= SLEEP_BAD_NOTE:
        flow = _consecutive(daily, "sleep_bad")
        level = WARN if n >= SLEEP_BAD_WARN else NOTE
        detail = f"{span} 수면 부족을 {_over(_ndays(daily, 'sleep_bad'))} {n}회 언급하셨습니다{vs(pn, n, '회')}."
        detail += (" 이틀 연속 말씀하신 날이 있어 컨디션을 확인해 보시길 권합니다." if flow
                   else " 컨디션을 확인해 보시길 권합니다.")
        sig.append(_signal("sleep_bad", level, "수면 부족 언급" + (" (이어지는 흐름)" if flow else ""), detail, n, pv(pn),
                           _count_affects(level, n)))
        rec.append("수면 상태를 한 번 여쭤봐 주세요.")

    # 2. 증상: 위험 표현(확인) / 같은 부위 반복(참고·주의) — 언급 날짜 수 기준
    danger = [s for s in cur["symptoms"].values() if s["danger"]]
    if danger:
        what = ", ".join(f"{s['part'] or '부위 미상'} {SYMPTOM_NOUN.get(s['kind'], s['kind'])}" for s in danger)
        cnt = sum(s["count"] for s in danger)
        pcnt = sum(s["count"] for s in prev["symptoms"].values() if s["danger"])
        sig.append(_signal("symptom_danger", CHECK, "몸이 불편하다고 말씀하신 기록",
                           f"몸이 불편하다고 말씀하신 기록이 있습니다({what}). 현재 상태를 직접 확인해 보시길 권합니다.",
                           cnt, pv(pcnt)))
        rec.append("어르신께 연락해 현재 몸 상태를 직접 확인해 보시길 권합니다.")
    pparts = _part_days(prev["symptoms"])
    for part, p in sorted(_part_days(cur["symptoms"]).items(), key=lambda kv: (-len(kv[1]["days"]), kv[0])):
        nd = len(p["days"])
        if nd < SYMPTOM_DAYS_NOTE:
            continue
        noun = "·".join(SYMPTOM_NOUN.get(k, k) for k in p["kinds"])
        pd = len(pparts.get(part, {}).get("days", ()))
        sig.append(_signal(f"symptom:{part}", WARN if nd >= SYMPTOM_DAYS_WARN else NOTE, f"{part} 불편 언급",
                           f"{part} {noun}{_eul(noun)} {nd}일에 걸쳐 언급하셨습니다{vs(pd, nd, '일')}.", nd, pv(pd)))
        rec.append(f"{part} 상태가 어떠신지 한 번 여쭤봐 주세요.")

    # 3. 감정 표현 (말씀 기준)
    n, pn = _sum(daily, "negative_emotion_mentions"), _sum(pdaily, "negative_emotion_mentions")
    leth = cur["emotion"].get("lethargic", 0)
    if leth >= LETHARGIC_WARN or n >= EMOTION_NOTE:
        level = WARN if (leth >= LETHARGIC_WARN or n >= EMOTION_WARN) else NOTE
        kinds = "·".join(EMOTION_KO[k] for k in rs.NEG_EMOTION_KINDS if cur["emotion"].get(k))
        detail = (f"{span} {kinds} 등 마음이 힘든 표현을 {_over(_ndays(daily, 'negative_emotion_mentions'))} "
                  f"{n}회 말씀하셨습니다{vs(pn, n, '회')}.")
        if leth >= LETHARGIC_WARN:
            detail += f" 그중 무기력 표현이 {leth}회였습니다."
        sig.append(_signal("emotion_text", level, "마음이 힘든 표현 (말씀 기준)", detail, n, pv(pn),
                           _count_affects(level, n)))
        rec.append("요즘 마음이 어떠신지 편하게 이야기를 나눠 보세요.")

    # 4. 식사 거름
    n, pn = _sum(daily, "meal_not"), _sum(pdaily, "meal_not")
    if n >= MEAL_NOT_NOTE:
        level = WARN if n >= MEAL_NOT_WARN else NOTE
        sig.append(_signal("meal_not", level, "식사 거름 언급",
                           f"{span} 식사를 안 하셨다는 말씀을 {_over(_ndays(daily, 'meal_not'))} {n}회 하셨습니다"
                           f"{vs(pn, n, '회')}.", n, pv(pn), _count_affects(level, n)))
        rec.append("식사를 잘 챙기시는지 여쭤봐 주세요.")

    # 5. 대화량 감소 (직전 기간 비교가 가능할 때만)
    n, pn = _sum(daily, "user_msgs"), _sum(pdaily, "user_msgs")
    if compare and pn >= VOLUME_MIN_PREV and n < pn * VOLUME_DROP_RATIO:
        sig.append(_signal("talk_volume", NOTE, "대화량 감소",
                           f"{before}보다 대화가 줄었습니다({pn}건 → {n}건).", n, pn))
        rec.append("평소처럼 대화하고 계신지 안부를 여쭤봐 주세요.")

    # 6. 카메라 표정 변화 이벤트 (참고 전용 — 시간 비율·감정 상태로 해석하지 않음)
    if cam["events"] >= CAMERA_MIN_EVENTS and cam["negative_events"] >= cam["events"] * CAMERA_NEG_RATIO:
        sig.append(_signal("camera_negative_changes", NOTE, "부정 표정 변화 이벤트 비중",
                           f"표정 변화 기록 중 부정 표정으로의 변화 비중이 높았습니다"
                           f"({cam['events']}건 중 {cam['negative_events']}건, 참고용).", cam, pv(cam_prev)))
    return sig, rec


# ---------------- 데이터 충분성·종합 단계 ----------------
def sufficiency(cur: dict) -> dict:
    analyzed, nd, health = cur["analyzed"], cur["analyzed_days"], cur["health_mentions"]
    reason = (f"분석된 어르신 발화가 {MIN_ANALYZED}건 미만입니다" if analyzed < MIN_ANALYZED else
              f"분석된 날이 {nd}일뿐이라 주간 경향을 판단하기 어렵습니다" if nd < MIN_DAYS_WITH_DATA else
              "수면·식사·증상·감정 관련 언급이 없습니다" if health == 0 else "")
    return {"analyzed": analyzed, "min_required": MIN_ANALYZED, "days_with_data": nd,
            "min_days_required": MIN_DAYS_WITH_DATA, "health_mentions": health, "enough": not reason, "reason": reason}


def overall(signals: list[dict], cur: dict, days: int) -> tuple[str, str, dict]:
    suff = sufficiency(cur)
    analyzed, nd, health = suff["analyzed"], suff["days_with_data"], suff["health_mentions"]
    warns = [s for s in signals if s["level"] == WARN]
    notes = [s for s in signals if s["level"] == NOTE]
    notes_eff = [s for s in notes if s["affects_level"]]
    titles = lambda xs: "·".join(s["title"].split(" (")[0] for s in xs)   # noqa: E731
    tail = " 아래 신호는 참고로만 살펴봐 주세요." if signals else ""

    if any(s["level"] == CHECK for s in signals):
        return "확인 권장", "몸이 불편하다고 말씀하신 기록이 있습니다. 현재 상태를 직접 확인해 보시길 권합니다.", suff
    if analyzed < MIN_ANALYZED:
        return "판단 보류", (f"최근 {days}일간 분석된 어르신 발화가 {analyzed}건으로 적어"
                          f"(최소 {MIN_ANALYZED}건) 종합 판단을 보류합니다.{tail}"), suff
    if nd < MIN_DAYS_WITH_DATA:
        return "판단 보류", (f"최근 {days}일간 분석된 날이 {nd}일이라 주간 경향을 판단하기 어렵습니다"
                          f"(최소 {MIN_DAYS_WITH_DATA}일).{tail}"), suff
    if health == 0:
        return "판단 보류", (f"최근 {days}일간 분석된 발화 {analyzed}건 중 수면·식사·증상·감정 관련 언급이 없어 "
                          "상태를 판단할 수 없습니다."), suff
    if len(warns) >= OVERALL_WARN_FOR_CHECK:
        return "확인 권장", (f"주의 신호가 {len(warns)}가지 확인되었습니다({titles(warns)}). "
                          "어르신께 직접 상태를 확인해 보시길 권합니다."), suff
    if warns or len(notes_eff) >= OVERALL_NOTE_FOR_WATCH:
        parts = [f"주의 신호 {len(warns)}가지" if warns else "", f"참고 신호 {len(notes_eff)}가지" if notes_eff else ""]
        found = "와 ".join(p for p in parts if p)
        return "관찰 필요", (f"{found}가 확인되었습니다({titles(warns + notes_eff)}). "
                          "며칠 더 지켜보시며 안부를 여쭤봐 주세요."), suff
    extra = f" 참고 신호 {len(notes)}가지는 아래에서 확인할 수 있습니다." if notes else ""
    return "특이 신호 없음", (f"수면·식사·증상·감정 관련 언급 {health}회가 확인되었으나 "
                          f"설정된 주의 신호는 없었습니다.{extra}"), suff


# ---------------- 조립 ----------------
def compute(db, hid: int, days: int, now: float, analyze: Callable | None = None) -> dict:
    d_from, d_to, start, end, names = rs.period(days, now)
    _, _, pstart, pend, pnames = rs.previous_period(days, now)
    cur = rs.aggregate(db, hid, start, end, names, analyze)
    prev = rs.aggregate(db, hid, pstart, pend, pnames, analyze)
    cam = _camera(db.report_events_between(hid, "emotion", start, end))
    cam_prev = _camera(db.report_events_between(hid, "emotion", pstart, pend))
    comp = comparison(prev, days)

    signals, recs = detect(cur, prev, cam, cam_prev, days, compare=comp["available"])
    level, summary, suff = overall(signals, cur, days)

    al = db.report_alerts_between(hid, start, end)
    alerts = {"count": len(al), "items": [{"ts": a["ts"], **({"type": a["cause"]} if a["cause"] else {})} for a in al],
              "note": ALERT_NOTE}

    s = lambda a, k: _sum(a["daily"], k)  # noqa: E731
    pv = (lambda x: x) if comp["available"] else (lambda x: None)
    changes = [{"label": label, "current": f(cur), "previous": pv(f(prev))} for label, f in (
        ("어르신 발화", lambda a: s(a, "user_msgs")),
        ("내용 분석된 발화", lambda a: a["analyzed"]),
        ("수면 부족 언급", lambda a: s(a, "sleep_bad")),
        ("피곤 언급", lambda a: s(a, "tired")),
        ("식사 거름 언급", lambda a: s(a, "meal_not")),
        ("몸 불편 언급", lambda a: s(a, "symptom_mentions")),
        ("마음이 힘든 표현", lambda a: s(a, "negative_emotion_mentions")),
    )] + [{"label": "표정 변화 기록", "current": cam["events"], "previous": pv(cam_prev["events"])}]

    return {
        "days": days, "from": d_from, "to": d_to,
        "level": level, "summary": summary, "signals": signals, "alerts": alerts, "changes": changes,
        "comparison": comp, "recommendations": list(dict.fromkeys(recs)), "sufficiency": suff, "daily": cur["daily"],
        "basis": BASIS, "version": VERSION, "disclaimer": DISCLAIMER,
    }
