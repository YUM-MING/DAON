"""리포트 '이번 주 상태 요약' (server/core/report_insights.py, GET /report/insights). 로컬 전용 테스트.
임시 SQLite, 외부 LLM 호출 없음. 문장 분석은 실제 text_analyzer 를 쓴다."""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime

import pytest

from server.core import report_insights as ri
from server.core import report_stats as rs
from server.db import DB

HID = 1
NOW = datetime(2026, 11, 9, 15, 0, 0).timestamp()     # 현재 기간 11/3~11/9, 직전 기간 10/27~11/2
DAY = 86400
NEUTRAL = ["비가 오네", "텔레비전 보고 있어", "오늘 하늘이 맑네", "바람이 시원하네", "꽃이 예쁘게 피었네", "단호박죽 먹고 싶어"]


@pytest.fixture
def db():
    return DB(os.path.join(tempfile.mkdtemp(), "t.db"))


_seq = {"n": 0}


def say(db, text, off=0):
    """off 일 전 (같은 날 안에서 시각을 조금씩 다르게)"""
    _seq["n"] += 1
    db.record_event(HID, "dialogue", {"who": "user", "text": text}, NOW - off * DAY - 3600 - _seq["n"] * 37)


def pad(db, n, off=0):
    """일반 발화 n건. off 일과 off+1 일에 번갈아 넣는다 (분석된 날 ≥2일 조건을 채워, 날짜 수와 무관한 테스트가
    원래 확인하려던 규칙만 보게 한다)"""
    for i in range(n):
        say(db, NEUTRAL[i % len(NEUTRAL)], off + i % 2)


def face(db, label, off=0):
    _seq["n"] += 1
    db.record_event(HID, "emotion", {"label": label}, NOW - off * DAY - 7200 - _seq["n"] * 11)


def ins(db, days=7):
    return ri.compute(db, HID, days, NOW)


def sig(r, key):
    return next((s for s in r["signals"] if s["key"] == key), None)


def texts(r):
    out = [r["summary"], *r["recommendations"]]
    for s in r["signals"]:
        out += [s["title"], s["detail"]]
    return out


# ───────── 1. 수면 ─────────
@pytest.mark.parametrize("n,level", [(1, None), (2, "참고"), (3, "주의")])
def test_sleep_thresholds(db, n, level):
    for i in range(n):
        say(db, "어젯밤에 잠을 잘 못 잤어", off=i * 2)          # 이틀 간격 (연속 아님)
    pad(db, 6)
    s = sig(ins(db), "sleep_bad")
    assert (s and s["level"]) == level
    if s:
        assert s["current"] == n and "이어지는 흐름" not in s["title"]
        assert f"최근 7일간 수면 부족을 {n}일에 걸쳐 {n}회 언급하셨습니다." in s["detail"]   # 보완 1: 언급 날짜 수
        assert s["affects_level"] is True


def test_sleep_consecutive_flow(db):
    say(db, "어젯밤에 잠을 잘 못 잤어", 0)
    say(db, "어젯밤에 잠을 잘 못 잤어", 1)
    pad(db, 6)
    s = sig(ins(db), "sleep_bad")
    assert s["title"] == "수면 부족 언급 (이어지는 흐름)" and "이틀 연속 말씀하신 날이 있어" in s["detail"]


def test_sleep_previous_period(db):
    for off in (8, 9, 10, 11):
        say(db, "어젯밤에 잠을 잘 못 잤어", off)                 # 직전 기간 4회
    pad(db, 2, off=8)                                         # 직전 기간 분석 발화 6건 → 비교 가능 (보완 4)
    say(db, "어젯밤에 잠을 잘 못 잤어", 0)
    say(db, "어젯밤에 잠을 잘 못 잤어", 3)
    pad(db, 6)
    s = sig(ins(db), "sleep_bad")
    assert (s["current"], s["previous"], s["level"]) == (2, 4, "참고")
    assert "(직전 7일 4회 → 이번 7일 2회)" in s["detail"]


# ───────── 2. 증상 ─────────
@pytest.mark.parametrize("offs,level", [((0, 0), None), ((0, 1), "참고"), ((0, 2, 4), "주의")])
def test_symptom_days(db, offs, level):
    for off in offs:
        say(db, "요즘 무릎이 좀 아프네", off)
    pad(db, 6)
    s = sig(ins(db), "symptom:무릎")
    assert (s and s["level"]) == level
    if s:
        assert s["current"] == len(set(offs)) and s["detail"] == f"무릎 통증을 {len(set(offs))}일에 걸쳐 언급하셨습니다."


def test_symptom_kinds_merged_per_part(db):
    say(db, "요즘 무릎이 좀 아프네", 0)
    say(db, "무릎이 쑤시네", 1)
    pad(db, 6)
    s = sig(ins(db), "symptom:무릎")
    assert s["level"] == "참고" and s["detail"] == "무릎 쑤심·통증을 2일에 걸쳐 언급하셨습니다."   # 먼저 말한 순서


def test_symptom_previous_days(db):
    for off in (7, 8):
        say(db, "허리가 좀 아프네", off)
    pad(db, 4, off=9)                                         # 직전 기간 분석 발화 6건 → 비교 가능 (보완 4)
    for off in (0, 1, 2):
        say(db, "허리가 좀 아프네", off)
    pad(db, 6)
    s = sig(ins(db), "symptom:허리")
    assert (s["current"], s["previous"]) == (3, 2)


def test_danger_with_little_data_is_check(db):
    say(db, "가슴이 너무 아파")
    pad(db, 3)                                               # 발화 4건 (데이터 부족)
    r = ins(db)
    assert r["level"] == "확인 권장" and sig(r, "symptom_danger")["level"] == "확인"
    assert r["sufficiency"]["enough"] is False and r["sufficiency"]["analyzed"] == 4
    assert r["summary"] == "몸이 불편하다고 말씀하신 기록이 있습니다. 현재 상태를 직접 확인해 보시길 권합니다."  # 보완 3


# ───────── 3. 감정 (말씀 기준) ─────────
@pytest.mark.parametrize("lines,level", [
    (["너무 외로워"], None),
    (["너무 외로워", "요즘 불안해"], "참고"),
    (["너무 외로워", "요즘 불안해", "너무 슬퍼"], "주의"),
    (["의욕이 없어", "만사가 귀찮아"], "주의"),                    # 무기력 단독 2회
    (["의욕이 없어"], None),
])
def test_emotion_thresholds(db, lines, level):
    for i, t in enumerate(lines):
        say(db, t, i)
    pad(db, 6)
    s = sig(ins(db), "emotion_text")
    assert (s and s["level"]) == level


def test_angry_and_positive_not_negative_sum(db):
    for t in ("화가 나", "화가 나", "오늘 날씨가 참 좋네", "오늘 날씨가 참 좋네"):
        say(db, t)
    pad(db, 6)
    assert sig(ins(db), "emotion_text") is None


# ───────── 4. 식사 ─────────
@pytest.mark.parametrize("n,level", [(0, None), (1, "참고"), (2, "주의")])
def test_meal_not(db, n, level):
    for i in range(n):
        say(db, "아직 아침을 안 먹었어", i)
    say(db, "오늘 점심 먹었어")
    pad(db, 6)
    s = sig(ins(db), "meal_not")
    assert (s and s["level"]) == level


# ───────── 5. 대화량 ─────────
@pytest.mark.parametrize("prev,cur,hit", [(6, 2, True), (6, 3, False), (5, 0, False), (14, 5, True)])
def test_talk_volume(db, prev, cur, hit):
    pad(db, prev, off=8)
    pad(db, cur, off=0)
    s = sig(ins(db), "talk_volume")
    assert bool(s) == hit
    if s:
        assert s["level"] == "참고" and s["detail"] == f"직전 7일보다 대화가 줄었습니다({prev}건 → {cur}건)."


# ───────── 6. 카메라 표정 변화 이벤트 ─────────
@pytest.mark.parametrize("neg,other,hit", [(9, 0, False), (5, 5, True), (4, 6, False), (10, 0, True)])
def test_camera_thresholds(db, neg, other, hit):
    for i in range(neg):
        face(db, ("sad", "angry", "fear")[i % 3])
    for i in range(other):
        face(db, ("happy", "neutral", "surprised")[i % 3])
    pad(db, 6)
    say(db, "오늘 점심 먹었어")
    s = sig(ins(db), "camera_negative_changes")
    assert bool(s) == hit
    if s:
        assert s["level"] == "참고" and s["current"] == {"events": neg + other, "negative_events": neg}
        assert "%" not in s["detail"] and "동안" not in s["detail"] and "참고용" in s["detail"]


def test_camera_alone_does_not_raise_level(db):
    for i in range(12):
        face(db, "sad")
    say(db, "오늘 점심 먹었어")
    pad(db, 6)
    r = ins(db)
    assert sig(r, "camera_negative_changes") and r["level"] == "특이 신호 없음"
    assert "참고 신호 1가지" in r["summary"]


def test_camera_plus_one_note_is_watch(db):
    for i in range(10):
        face(db, "fear")
    say(db, "어젯밤에 잠을 잘 못 잤어", 0)
    say(db, "어젯밤에 잠을 잘 못 잤어", 3)                      # 수면 참고(2회, 단계 반영) + 카메라 참고 = 참고 2개
    pad(db, 6)
    assert ins(db)["level"] == "관찰 필요"


def test_camera_plus_single_meal_is_clear(db):
    for i in range(10):
        face(db, "fear")
    say(db, "아직 아침을 안 먹었어")                            # 식사 거름 1회는 단발 → 단계 미반영 (보완 2)
    pad(db, 6)
    r = ins(db)
    assert sig(r, "meal_not")["affects_level"] is False and sig(r, "camera_negative_changes")["affects_level"] is True
    assert r["level"] == "특이 신호 없음" and "참고 신호 2가지는 아래에서" in r["summary"]


# ───────── 종합 단계 ─────────
def test_insufficient_shows_signals(db):
    say(db, "어젯밤에 잠을 잘 못 잤어", 0)
    say(db, "어젯밤에 잠을 잘 못 잤어", 1)
    say(db, "아직 아침을 안 먹었어", 0)
    say(db, "아직 아침을 안 먹었어", 2)                         # 발화 4건, 위험 표현 없음
    r = ins(db)
    assert r["level"] == "판단 보류" and r["sufficiency"]["reason"] == "분석된 어르신 발화가 6건 미만입니다"
    assert sig(r, "sleep_bad") and sig(r, "meal_not") and "참고로만" in r["summary"]


@pytest.mark.parametrize("n", [5, 6])
def test_min_analyzed_boundary(db, n):
    say(db, "아직 아침을 안 먹었어", 0)
    say(db, "아직 아침을 안 먹었어", 1)                         # 식사 거름 주의 1개
    pad(db, n - 2)
    r = ins(db)
    assert r["sufficiency"]["analyzed"] == n
    assert r["level"] == ("판단 보류" if n < 6 else "관찰 필요")


def test_only_weather_talk_is_pending_not_clear(db):
    for i in range(20):
        say(db, (NEUTRAL + ["오늘 날씨가 참 좋네"])[i % 7], i % 7)
    r = ins(db)
    assert r["sufficiency"]["analyzed"] == 20 and r["sufficiency"]["health_mentions"] == 0
    assert r["level"] == "판단 보류" and r["sufficiency"]["reason"] == "수면·식사·증상·감정 관련 언급이 없습니다"
    assert "특이 신호 없음" not in r["summary"]


def test_health_mentions_without_signals_is_clear_with_basis(db):
    say(db, "오늘 점심 먹었어")
    say(db, "잘 잤어")
    pad(db, 6)
    r = ins(db)
    assert r["level"] == "특이 신호 없음" and r["signals"] == []
    assert r["summary"] == "수면·식사·증상·감정 관련 언급 2회가 확인되었으나 설정된 주의 신호는 없었습니다."
    assert r["sufficiency"]["enough"] is True


def test_single_note_only_is_clear(db):
    say(db, "어젯밤에 잠을 잘 못 잤어", 0)
    say(db, "어젯밤에 잠을 잘 못 잤어", 3)
    pad(db, 6)
    r = ins(db)
    assert [s["level"] for s in r["signals"]] == ["참고"] and r["level"] == "특이 신호 없음"


def test_two_notes_is_watch(db):
    say(db, "어젯밤에 잠을 잘 못 잤어", 0)
    say(db, "어젯밤에 잠을 잘 못 잤어", 3)
    say(db, "요즘 무릎이 좀 아프네", 0)
    say(db, "요즘 무릎이 좀 아프네", 3)
    pad(db, 6)
    r = ins(db)
    assert sorted(s["level"] for s in r["signals"]) == ["참고", "참고"] and r["level"] == "관찰 필요"


def test_one_warn_is_watch_three_warns_is_check(db):
    for off in (0, 2, 4):
        say(db, "어젯밤에 잠을 잘 못 잤어", off)               # 수면 주의
    pad(db, 6)
    assert ins(db)["level"] == "관찰 필요"
    for off in (0, 2, 4):
        say(db, "요즘 무릎이 좀 아프네", off)                   # 증상 주의
    assert ins(db)["level"] == "관찰 필요"
    say(db, "아직 아침을 안 먹었어", 0)
    say(db, "아직 아침을 안 먹었어", 2)                         # 식사 주의 → 주의 3개
    r = ins(db)
    assert r["level"] == "확인 권장" and "주의 신호가 3가지" in r["summary"]


# ───────── 알림 ─────────
def test_alerts_separate_from_level(db):
    say(db, "오늘 점심 먹었어")
    pad(db, 6)
    before = ins(db)
    db.alert_create(HID, "a-001", NOW - 5000, "no_response", "응답 없음", {}, {})
    db.alert_create(HID, "a-002", NOW - 4000, "", "", {}, {})
    db.alert_create(HID, "a-003", NOW - 9 * DAY, "danger", "", {}, {})          # 직전 기간 (세지 않음)
    r = ins(db)
    assert r["level"] == before["level"] == "특이 신호 없음" and r["signals"] == before["signals"]
    assert r["alerts"]["count"] == 2 and r["alerts"]["note"] == ri.ALERT_NOTE
    assert r["alerts"]["items"] == [{"ts": NOW - 5000, "type": "no_response"}, {"ts": NOW - 4000}]


def test_alerts_included_with_little_data(db):
    db.alert_create(HID, "a-001", NOW - 100, "crisis", "", {}, {})
    r = ins(db)
    assert r["level"] == "판단 보류" and r["alerts"]["count"] == 1 and r["alerts"]["items"][0]["type"] == "crisis"


# ───────── 문장 금지 표현 ─────────
BANNED = ["진단", "질환", "우울증", "치매", "불면증", "될 것", "할 것", "일 것", "위험하다", "위험합니다", "안정",
          "정상", "때문", "원인", "% 동안", "동안 부정", "부정적이었",
          "악화", "호전", "나빠", "좋아졌", "해졌", "급히", "위험", "을(를)"]                         # 보완 3·4 추가


def test_no_banned_phrases(db):
    for off in (0, 1, 2):
        say(db, "어젯밤에 잠을 잘 못 잤어", off)
        say(db, "요즘 무릎이 좀 아프네", off)
        say(db, "너무 외로워", off)
        say(db, "아직 아침을 안 먹었어", off)
    say(db, "의욕이 없어")
    say(db, "만사가 귀찮아")
    pad(db, 40, off=9)                                       # 직전 기간 40건 → 현재 15건: 대화량 감소
    for i in range(12):
        face(db, "fear")
    scenarios = [ins(db)]
    say(db, "가슴이 너무 아파")
    scenarios.append(ins(db))
    empty = DB(os.path.join(tempfile.mkdtemp(), "e.db"))
    scenarios.append(ri.compute(empty, HID, 7, NOW))
    for r in scenarios:
        for t in texts(r):
            for b in BANNED:
                assert b not in t, (b, t)
    assert {s["key"] for s in scenarios[1]["signals"]} >= {"sleep_bad", "symptom:무릎", "emotion_text", "meal_not",
                                                          "talk_volume", "camera_negative_changes", "symptom_danger"}


# ───────── 응답 형식 ─────────
def test_response_shape_and_daily(db):
    say(db, "어젯밤에 잠을 잘 못 잤어", 1)
    say(db, "아직 아침을 안 먹었어", 1)
    say(db, "너무 외로워", 0)
    r = ins(db)
    assert set(r) == {"days", "from", "to", "level", "summary", "signals", "alerts", "changes", "recommendations",
                      "sufficiency", "daily", "basis", "version", "disclaimer", "comparison"}          # 보완 4: comparison
    assert r["basis"] == "user_said+camera_events+alert_history" and r["version"] == "report_insights/2"
    assert r["disclaimer"] == "참고용 주의 신호이며 진단이나 예측이 아닙니다. 임계값은 검증되지 않은 개발용 초기값입니다."
    assert (r["from"], r["to"], len(r["daily"])) == ("2026-11-03", "2026-11-09", 7)
    assert r["daily"][-2] == {"day": "2026-11-08", "user_msgs": 2, "sleep_bad": 1, "tired": 0, "meal_not": 1,
                              "symptom_mentions": 0, "negative_emotion_mentions": 0}
    assert r["daily"][-1]["negative_emotion_mentions"] == 1
    assert r["level"] in ri.LEVELS
    assert set(r["sufficiency"]) == {"analyzed", "min_required", "days_with_data", "min_days_required",
                                     "health_mentions", "enough", "reason"}                          # 보완 1
    assert all(isinstance(s["affects_level"], bool) for s in r["signals"])                         # 보완 2
    assert r["sufficiency"]["days_with_data"] == 2
    assert all(set(c) == {"label", "current", "previous"} for c in r["changes"])
    json.dumps(r, ensure_ascii=False)                        # 직렬화 가능 (set 이 새지 않음)


def test_empty_db(db):
    r = ins(db)
    assert r["level"] == "판단 보류" and r["signals"] == [] and r["alerts"]["count"] == 0 and r["recommendations"] == []


def test_stats_response_unchanged(db):
    say(db, "요즘 무릎이 좀 아프네")
    r = rs.compute(db, HID, 7, NOW)
    assert set(r) == {"days", "from", "to", "analyzed", "conversations", "sleep", "meal", "symptoms", "emotion_text",
                      "topics", "wants", "basis", "version", "skipped"}
    assert r["symptoms"] == [{"part": "무릎", "kind": "pain", "count": 1, "days": 1, "danger": False}]
    assert set(r["conversations"][0]) == {"day", "user_msgs", "robot_msgs"}


def test_daily_sum_matches_stats(db):
    for off in (0, 1, 3):
        say(db, "어젯밤에 잠을 잘 못 잤어", off)
        say(db, "아직 아침을 안 먹었어", off)
    st = rs.compute(db, HID, 7, NOW)
    r = ins(db)
    assert sum(d["sleep_bad"] for d in r["daily"]) == st["sleep"]["bad"] == 3
    assert sum(d["meal_not"] for d in r["daily"]) == st["meal"]["not"] == 3
    assert [d["user_msgs"] for d in r["daily"]] == [c["user_msgs"] for c in st["conversations"]]


# ───────── 엔드포인트 ─────────
@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from server import config as cfgmod
    from server.auth import COOKIE
    from server.main import create_app
    d = tempfile.mkdtemp()
    cfg = cfgmod.load(None, mock=True, overrides={"logs": {"dir": d}, "modules": {"dialogue": {"mode": "off"}}})
    app = create_app(cfg)
    core = app.state.core
    u = core.db.create_user("i@x.com", "1234", "보호자")
    h = core.db.create_home("우리 집", u["id"])
    c = TestClient(app)
    c.cookies.set(COOKIE, core.db.create_session(u["id"]))
    return c, core.db, h["id"]


def test_endpoint_ok_and_400(client):
    c, db, hid = client
    db.record_event(hid, "dialogue", {"who": "user", "text": "가슴이 너무 아파"}, time.time() - 60)
    r = c.get(f"/api/homes/{hid}/report/insights")
    assert r.status_code == 200 and r.json()["days"] == 7 and r.json()["level"] == "확인 권장"
    for bad in ("0", "31", "abc", ""):
        r = c.get(f"/api/homes/{hid}/report/insights", params={"days": bad})
        assert r.status_code == 400 and "error" in r.json(), bad
    assert c.get(f"/api/homes/{hid}/report/insights", params={"days": "30"}).status_code == 200


def test_endpoint_requires_member(client):
    c, db, hid = client
    c.cookies.clear()
    assert c.get(f"/api/homes/{hid}/report/insights").status_code in (401, 403)


# ───────── 보완 1~4 ─────────
def _same_day_24(db, off_for_neutral):
    say(db, "어젯밤에 잠을 잘 못 잤어", 0)
    say(db, "어젯밤에 잠을 잘 못 잤어", 0)
    say(db, "아직 아침을 안 먹었어", 0)
    for i in range(21):
        say(db, NEUTRAL[i % len(NEUTRAL)], off_for_neutral(i))


def test_one_day_of_data_is_pending_but_signals_shown(db):
    _same_day_24(db, lambda i: 0)                               # 분석 24건이 모두 하루
    r = ins(db)
    sf = r["sufficiency"]
    assert (sf["analyzed"], sf["days_with_data"], sf["min_days_required"], sf["enough"]) == (24, 1, 2, False)
    assert sf["reason"] == "분석된 날이 1일뿐이라 주간 경향을 판단하기 어렵습니다"
    assert r["level"] == "판단 보류"
    assert r["summary"] == ("최근 7일간 분석된 날이 1일이라 주간 경향을 판단하기 어렵습니다(최소 2일). "
                            "아래 신호는 참고로만 살펴봐 주세요.")
    assert {s["key"] for s in r["signals"]} == {"sleep_bad", "meal_not"}
    assert "하루에 2회" in sig(r, "sleep_bad")["detail"] and "하루에 1회" in sig(r, "meal_not")["detail"]


def test_two_days_single_meal_not_counted_in_level(db):
    _same_day_24(db, lambda i: i % 2)                           # 분석된 날 2일
    r = ins(db)
    assert r["sufficiency"]["enough"] is True and r["sufficiency"]["days_with_data"] == 2
    s, m = sig(r, "sleep_bad"), sig(r, "meal_not")
    assert (s["level"], s["affects_level"]) == ("참고", True)
    assert (m["level"], m["affects_level"], m["current"]) == ("참고", False, 1)
    assert r["level"] == "특이 신호 없음"                         # 단계에 반영되는 참고 1개뿐
    assert "참고 신호 2가지는 아래에서 확인할 수 있습니다" in r["summary"]


def test_meal_twice_affects(db):
    say(db, "아직 아침을 안 먹었어", 0)
    say(db, "아직 아침을 안 먹었어", 1)
    pad(db, 6)
    m = sig(ins(db), "meal_not")
    assert (m["level"], m["affects_level"]) == ("주의", True) and "2일에 걸쳐 2회" in m["detail"]


def test_danger_one_day_new_wording(db):
    say(db, "머리가 어지러워", 0)
    pad(db, 3, off=0)
    db.record_event(HID, "dialogue", {"who": "user", "text": "비가 오네"}, NOW - 50)
    r = ins(db)
    d = sig(r, "symptom_danger")
    assert r["level"] == "확인 권장" and r["sufficiency"]["enough"] is False
    assert d["detail"] == "몸이 불편하다고 말씀하신 기록이 있습니다(머리 어지러움). 현재 상태를 직접 확인해 보시길 권합니다."
    assert d["title"] == "몸이 불편하다고 말씀하신 기록" and d["affects_level"] is True
    assert r["recommendations"][0] == "어르신께 연락해 현재 몸 상태를 직접 확인해 보시길 권합니다."


def test_no_previous_data_comparison_unavailable(db):
    for off in (0, 1, 2):
        say(db, "어젯밤에 잠을 잘 못 잤어", off)
    say(db, "요즘 무릎이 좀 아프네", 0)
    say(db, "요즘 무릎이 좀 아프네", 1)
    pad(db, 6)
    say(db, "비가 오네", 9)                                     # 직전 기간 1건뿐
    r = ins(db)
    assert r["comparison"] == {"available": False, "previous_analyzed": 1, "previous_days_with_data": 1,
                               "reason": "직전 7일에 비교할 만큼의 기록이 없습니다"}
    assert r["signals"] and all(s["previous"] is None for s in r["signals"])
    assert all(c["previous"] is None for c in r["changes"])
    assert sig(r, "talk_volume") is None
    assert not any("직전" in t for t in texts(r))


def test_previous_14_comparison_numbers_only(db):
    pad(db, 12, off=8)
    say(db, "어젯밤에 잠을 잘 못 잤어", 10)
    say(db, "어젯밤에 잠을 잘 못 잤어", 11)                      # 직전 14건 (수면 부족 2회)
    for off in (0, 1, 3):
        say(db, "어젯밤에 잠을 잘 못 잤어", off)
    pad(db, 2)                                                # 현재 5건
    r = ins(db)
    assert r["comparison"]["available"] is True and r["comparison"]["previous_analyzed"] == 14
    s = sig(r, "sleep_bad")
    assert (s["current"], s["previous"]) == (3, 2) and "(직전 7일 2회 → 이번 7일 3회)" in s["detail"]
    v = sig(r, "talk_volume")
    assert v["detail"] == "직전 7일보다 대화가 줄었습니다(14건 → 5건)." and v["previous"] == 14
    ch = {c["label"]: (c["current"], c["previous"]) for c in r["changes"]}
    assert ch["어르신 발화"] == (5, 14) and ch["수면 부족 언급"] == (3, 2)
    for t in texts(r):
        for b in ("악화", "호전", "나빠", "좋아졌", "해졌"):
            assert b not in t, (b, t)
