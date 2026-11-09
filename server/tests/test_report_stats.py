"""리포트 '대화 내용 통계' (server/core/report_stats.py, GET /report/stats). 로컬 전용 테스트.
임시 SQLite, 외부 LLM 호출 없음. 기대값은 실제 분석기(text_analyzer.analyze) 결과에 맞춘다."""
from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime

import pytest

from server.core import report_stats as rs
from server.db import DB

HID = 1
NOW = datetime(2026, 11, 9, 15, 0, 0).timestamp()      # 서버 로컬 시간 기준 오늘 오후 3시
TODAY, YESTERDAY = "2026-11-09", "2026-11-08"


@pytest.fixture
def db():
    d = tempfile.mkdtemp()
    return DB(os.path.join(d, "t.db"))


def say(db, text, ts, who="user"):
    db.record_event(HID, "dialogue", {"who": who, "text": text}, ts)


def stats(db, days=7):
    return rs.compute(db, HID, days, NOW)


def total_user(r):
    return sum(c["user_msgs"] for c in r["conversations"])


# ───────── 실제 STT 전사 문장 ─────────
def test_real_transcripts(db):
    t = NOW - 3600
    for i, s in enumerate(["보져빼면 잠을 잘 못 잤어.", "새벽에 잡고 깨서 오늘 너무 피곤하네", "다 놓고 죽이 먹고 싶네",
                           "근데 요즘 무릎이 좀 아프네", "오오오오오", "탄호폭축"]):
        say(db, s, t + i * 10)
    r = stats(db)
    assert r["sleep"] == {"bad": 1, "good": 0, "tired": 1}
    assert r["symptoms"] == [{"part": "무릎", "kind": "pain", "count": 1, "days": 1, "danger": False}]
    assert r["wants"]["food"] == []                                              # '죽'은 한 글자라 item 규칙(2~8자) 밖
    assert r["analyzed"] == 6 and r["skipped"]["stt_unclear"] == 0                # "오오오오오"·"탄호폭축"은 분석기가 오류로 안 봄
    assert r["basis"] == "user_said" and r["version"] == "report_stats/1"


# ───────── 좋아하는 음식·먹고 싶은 것 ─────────
@pytest.mark.parametrize("text,cat", [
    ("사실 나는 단호박죽을 참 좋아해", "like_food"), ("나는 따뜻한 단호박죽을 좋아해", "like_food"),
    ("단호박죽 좋아해", "like_food"), ("단호박죽 먹고 싶어", "food"),
])
def test_wants_counted(db, text, cat):
    say(db, text, NOW - 100)
    assert stats(db)["wants"][cat] == [{"item": "단호박죽", "count": 1}]


@pytest.mark.parametrize("text", ["우리 딸은 단호박죽을 좋아해", "딸이 단호박죽 먹고 싶대", "예전에는 단호박죽을 좋아했어",
                                  "단호박죽 안 좋아해", "단호박죽 먹고 싶다고 했잖아", "뭐 먹고 싶어", "약 먹고 싶어"])
def test_wants_excluded(db, text):
    say(db, text, NOW - 100)
    w = stats(db)["wants"]
    assert w["food"] == [] and w["like_food"] == [], text


def test_place_and_person(db):
    say(db, "바다에 가고 싶어", NOW - 300)
    say(db, "아들이 보고 싶어", NOW - 200)
    w = stats(db)["wants"]
    assert w["place"] == [{"item": "바다", "count": 1}] and w["person"] == [{"item": "아들", "count": 1}]


def test_top_five_and_counts(db):
    for i, f in enumerate(["단호박죽", "단호박죽", "팥죽", "냉면", "비빔밥", "불고기", "김치찌개"]):
        say(db, f"{f} 먹고 싶어", NOW - 1000 + i * 10)
    food = stats(db)["wants"]["food"]
    assert len(food) == 5 and food[0] == {"item": "단호박죽", "count": 2}


# ───────── 그 밖의 카테고리 ─────────
def test_other_categories(db):
    for i, s in enumerate(["어젯밤에 잘 잤어", "잘 자", "밥 먹었어", "밥 먹어야겠다", "옛날에 파전 많이 먹었는데", "요즘 너무 외로워",
                           "돌아가신 영감이 보고 싶어", "가슴이 너무 아파"]):
        say(db, s, NOW - 1000 + i * 10)
    r = stats(db)
    assert r["sleep"]["good"] == 1                                               # "잘 자"(인사)는 빼고
    assert r["meal"] == {"done": 1, "not": 0, "ask": 0, "memory": 1}            # "밥 먹어야겠다"는 완료 아님
    assert r["emotion_text"]["lonely"] == 1 and r["emotion_text"]["grief"] == 1
    assert {"part": "가슴", "kind": "pain", "count": 1, "days": 1, "danger": True} in r["symptoms"]
    assert r["topics"]["lonely"] == 1


def test_same_category_once_per_message(db):
    say(db, "무릎이 아파 무릎이 아파", NOW - 100)
    assert stats(db)["symptoms"][0]["count"] == 1


def test_symptom_days_counted(db):
    say(db, "무릎이 좀 아프네", NOW - 100)
    say(db, "무릎이 좀 아프네", NOW - 86400)
    s = stats(db)["symptoms"][0]
    assert (s["count"], s["days"]) == (2, 2)


# ───────── 중복·제외·건수 ─────────
def test_duplicate_within_window(db):
    say(db, "무릎이 좀 아프네", NOW - 100)
    say(db, "무릎이 좀 아프네.", NOW - 99)                                       # 1.0초 뒤 같은 문장 (문장부호만 다름)
    say(db, "무릎이 좀 아프네", NOW - 90)                                        # 10초 뒤: 의도적 반복은 따로
    r = stats(db)
    assert r["skipped"]["duplicate"] == 1 and total_user(r) == 2 and r["symptoms"][0]["count"] == 2


def test_recall_question_counted_in_user_msgs_but_skipped(db):
    say(db, "아까 어디가 아프다고 했지?", NOW - 100)
    say(db, "무릎이 좀 아프네", NOW - 50)
    r = stats(db)
    assert total_user(r) == 2 and r["skipped"]["recall_question"] == 1 and r["analyzed"] == 1


def test_stt_unclear_counted_in_user_msgs_but_skipped(db):
    say(db, "3 3 3", NOW - 100)                                                  # 분석기가 STT 오류(fragment)로 보는 입력
    say(db, "밥 먹었어", NOW - 50)
    r = stats(db)
    assert total_user(r) == 2 and r["skipped"]["stt_unclear"] == 1 and r["analyzed"] == 1


def test_analysis_error_skips_only_that_message(db):
    say(db, "폭탄문장", NOW - 100)
    say(db, "밥 먹었어", NOW - 50)
    real = rs.analyzer()

    def flaky(text):
        if text == "폭탄문장":
            raise RuntimeError("boom")
        return real(text)
    r = rs.compute(db, HID, 7, NOW, analyze=flaky)
    assert r["skipped"]["analysis_error"] == 1 and r["analyzed"] == 1 and r["meal"]["done"] == 1 and total_user(r) == 2


def test_analyzed_formula_and_robot_not_analyzed(db):
    say(db, "아까 어디가 아프다고 했지?", NOW - 400)
    say(db, "3 3 3", NOW - 300)
    say(db, "밥 먹었어", NOW - 200)
    say(db, "그렇군요. 무릎이 아프시군요.", NOW - 150, who="robot")
    say(db, "외로우시군요. 제가 곁에 있을게요.", NOW - 100, who="robot")
    say(db, "외로우시군요. 제가 곁에 있을게요.", NOW - 99, who="robot")               # 로봇은 원 건수 (중복 제거 안 함)
    r = stats(db)
    sk = r["skipped"]
    assert r["analyzed"] == total_user(r) - sk["stt_unclear"] - sk["recall_question"] - sk["analysis_error"]
    assert sum(c["robot_msgs"] for c in r["conversations"]) == 3
    assert r["symptoms"] == [] and r["emotion_text"] == {}                       # 로봇 발화는 분석하지 않음


def test_conversations_per_day_and_period(db):
    say(db, "밥 먹었어", NOW - 100)
    say(db, "밥 먹었어", NOW - 86400)
    say(db, "밥 먹었어", NOW - 86400 * 10)                                       # 기간 밖
    r = stats(db, days=2)
    assert (r["from"], r["to"]) == (YESTERDAY, TODAY)
    assert r["conversations"] == [{"day": YESTERDAY, "user_msgs": 1, "robot_msgs": 0},
                                  {"day": TODAY, "user_msgs": 1, "robot_msgs": 0}]


def test_empty_db(db):
    r = stats(db)
    assert r["analyzed"] == 0 and r["symptoms"] == [] and all(c["user_msgs"] == 0 for c in r["conversations"])
    assert len(r["conversations"]) == 7 and r["wants"] == {"food": [], "like_food": [], "place": [], "person": []}


def test_old_messages_not_truncated_by_limit(db):
    for i in range(30):
        say(db, f"대화 {i}", NOW - 86400 * 5 + i)                               # 5일 전
    say(db, "무릎이 좀 아프네", NOW - 86400 * 6)                                # 가장 오래된 발화
    assert stats(db)["symptoms"][0]["count"] == 1


# ───────── days 검증 ─────────
@pytest.mark.parametrize("raw", ["0", "31", "abc", "", None, "-1", "7.5"])
def test_parse_days_rejects(raw):
    with pytest.raises(ValueError):
        rs.parse_days(raw)


@pytest.mark.parametrize("raw,n", [("1", 1), ("7", 7), ("30", 30)])
def test_parse_days_accepts(raw, n):
    assert rs.parse_days(raw) == n


# ───────── 엔드포인트 (인증·400 형식) ─────────
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
    u = core.db.create_user("r@x.com", "1234", "보호자")
    h = core.db.create_home("우리 집", u["id"])
    c = TestClient(app)
    c.cookies.set(COOKIE, core.db.create_session(u["id"]))
    return c, core.db, h["id"]


def test_endpoint_ok_and_400(client):
    c, db, hid = client
    db.record_event(hid, "dialogue", {"who": "user", "text": "무릎이 좀 아프네"}, time.time() - 60)
    r = c.get(f"/api/homes/{hid}/report/stats")
    assert r.status_code == 200 and r.json()["days"] == 7 and r.json()["symptoms"][0]["part"] == "무릎"
    for bad in ("0", "31", "abc", ""):
        r = c.get(f"/api/homes/{hid}/report/stats", params={"days": bad})
        assert r.status_code == 400 and "error" in r.json(), bad                 # 형제 API와 같은 {"error": ...}
    assert c.get(f"/api/homes/{hid}/report/stats", params={"days": "30"}).status_code == 200


def test_endpoint_requires_member(client):
    c, db, hid = client
    c.cookies.clear()
    assert c.get(f"/api/homes/{hid}/report/stats").status_code in (401, 403)
