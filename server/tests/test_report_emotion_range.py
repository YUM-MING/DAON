"""주간 감정 통계의 기준일 선택 (GET /report?end=&days=, DB.report_between). 임시 SQLite, LLM·하드웨어 없음.
- end 가 없으면 기존 db.report 그대로 (지금 시각부터 days×24시간).
- end 가 있으면 기준일 포함 days 일 (서버 로컬 날짜, [시작, 끝)). 감정 집계 규칙은 db.report 와 같다:
  저장된 emotion 이벤트를 한 건씩 그대로 센다 (기간 밖 직전 감정과 비교하지 않음 → 기간 첫 이벤트도 1회)."""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
import types
from datetime import datetime, timedelta

import pytest

import server.db as dbmod
from server.db import DB

NOW = datetime(2026, 11, 15, 15, 0, 0).timestamp()      # 서버 로컬 오늘 11/15(일) 오후 3시
H1, H2 = 1, 2


def ts(m, d, hh=12, mm=0, ss=0):
    return datetime(2026, m, d, hh, mm, ss).timestamp()


def day_start(date):
    return datetime.strptime(date, "%Y-%m-%d").timestamp()


def rng(end, days):
    """기준일 포함 days 일의 [시작, 끝) (report_stats.period 와 같은 정의)"""
    stop = day_start(end) + 86400
    return datetime.fromtimestamp(stop - days * 86400).replace(hour=0).timestamp(), stop


@pytest.fixture
def db():
    d = tempfile.mkdtemp()
    return DB(os.path.join(d, "t.db"))


@pytest.fixture
def frozen(monkeypatch):
    """db.report 의 '지금 시각'을 NOW 로 고정 (db 모듈의 time 만 바꿈)"""
    monkeypatch.setattr(dbmod, "time", types.SimpleNamespace(time=lambda: NOW, localtime=time.localtime,
                                                             strftime=time.strftime))


def emo(db, hid, label, t):
    db.record_event(hid, "emotion", {"label": label, "confidence": 0.9} if label is not None else {"confidence": 0.9}, t)


def by_weekday(rows):
    return {r["day"]: {k: v for k, v in r.items() if k not in ("day", "date")} for r in rows}


# ───────── 집계 규칙: db.report 와 같은 이벤트 집합일 때만 비교 ─────────
def test_same_events_same_counts_as_db_report(db, frozen):
    # db.report(7일) 범위: 11/8 15:00 ~ / 새 방식(11/15 포함 7일): 11/9 00:00 ~ 11/16 00:00 → 11/9~11/15 14:59 는 둘 다 포함
    for t, lab in [(ts(11, 9, 1), "happy"), (ts(11, 10, 9), "sad"), (ts(11, 10, 10), "happy"), (ts(11, 12, 8), "fear"),
                   (ts(11, 13, 20), "angry"), (ts(11, 14, 7), "surprised"), (ts(11, 15, 14, 59), "neutral"),
                   (ts(11, 15, 9), None)]:
        emo(db, H1, lab, t)
    old = db.report(H1, 7)
    s, e = rng("2026-11-15", 7)
    new = db.report_between(H1, s, e, 7)
    assert by_weekday(new["emotion_week"]) == by_weekday(old["emotion_week"])
    assert sum(sum(v for k, v in r.items() if k not in ("day", "date")) for r in new["emotion_week"]) == 8
    assert by_weekday(new["emotion_week"])["목"]["fear"] == 1            # 기본 5종 밖 라벨도 기존처럼 추가
    assert by_weekday(new["emotion_week"])["일"]["neutral"] == 2         # 라벨 없는 이벤트 = neutral (기존 기본값)
    assert [r["date"] for r in new["emotion_week"]] == sorted(r["date"] for r in new["emotion_week"])
    assert all(set(r) >= {"day", "date", "happy", "sad", "angry", "neutral", "surprised"} for r in new["emotion_week"])


@pytest.mark.parametrize("prev,first", [("sad", "sad"), ("happy", "sad")])
def test_first_event_in_period_counted_like_db_report(db, frozen, prev, first):
    """기간 밖 직전 감정이 같든 다르든, 기간 첫 이벤트는 기존 db.report 처럼 1회로 센다"""
    emo(db, H1, prev, ts(11, 8, 10))                 # 두 방식 모두 기간 밖 (db.report 시작 11/8 15:00, 새 방식 11/9 00:00)
    emo(db, H1, first, ts(11, 10, 9))                # 기간 첫 이벤트
    emo(db, H1, "happy", ts(11, 11, 9))
    old = db.report(H1, 7)
    s, e = rng("2026-11-15", 7)
    new = db.report_between(H1, s, e, 7)
    assert by_weekday(new["emotion_week"]) == by_weekday(old["emotion_week"])
    assert by_weekday(new["emotion_week"])["화"][first] == 1
    assert "일" not in by_weekday(new["emotion_week"])                    # 기간 밖 이벤트(11/8 일)는 어느 쪽에도 없음
    assert "일" not in by_weekday(old["emotion_week"])
    assert sum(by_weekday(new["emotion_week"])["화"].values()) == 1      # 기간 첫날 행에는 첫 이벤트 1건뿐


def test_period_definition_differs_from_rolling_window(db, frozen):
    """의도된 차이: db.report 는 '지금부터 7×24시간'(11/8 15:00~), 새 방식은 '11/9 00:00~11/16 00:00'"""
    emo(db, H1, "sad", ts(11, 8, 20))                # 기존에는 들어가고 새 방식에는 안 들어감
    emo(db, H1, "happy", ts(11, 8, 10))              # 둘 다 안 들어감
    old = db.report(H1, 7)
    s, e = rng("2026-11-15", 7)
    new = db.report_between(H1, s, e, 7)
    assert by_weekday(old["emotion_week"]) == {"일": {"happy": 0, "sad": 1, "angry": 0, "neutral": 0, "surprised": 0}}  # 11/8(일)
    assert new["emotion_week"] == []


# ───────── 기간 · 경계 ─────────
def test_yesterday_and_week_ago(db):
    emo(db, H1, "happy", ts(11, 15, 9))
    emo(db, H1, "sad", ts(11, 14, 9))
    emo(db, H1, "angry", ts(11, 8, 9))
    emo(db, H1, "fear", ts(11, 7, 9))
    s, e = rng("2026-11-14", 7)
    r = db.report_between(H1, s, e, 7)
    assert [(x["date"], x["day"]) for x in r["emotion_week"]] == [("2026-11-08", "일"), ("2026-11-14", "토")]
    assert r["emotion_week"][0]["angry"] == 1 and r["emotion_week"][1]["sad"] == 1
    s, e = rng("2026-11-08", 1)
    assert [(x["date"], x["angry"]) for x in db.report_between(H1, s, e, 1)["emotion_week"]] == [("2026-11-08", 1)]


def test_midnight_before_after(db):
    emo(db, H1, "happy", ts(11, 14, 23, 59, 59))
    emo(db, H1, "sad", ts(11, 15, 0, 0, 0))
    emo(db, H1, "angry", ts(11, 12, 0, 0, 0))        # 3일 기간(11/12~11/14)의 첫날 0시: 포함
    emo(db, H1, "fear", ts(11, 11, 23, 59, 59))      # 그 직전: 제외
    s, e = rng("2026-11-14", 1)
    one = db.report_between(H1, s, e, 1)["emotion_week"]
    assert [(x["date"], x["happy"], x["sad"]) for x in one] == [("2026-11-14", 1, 0)]
    s, e = rng("2026-11-15", 1)
    assert [(x["date"], x["sad"]) for x in db.report_between(H1, s, e, 1)["emotion_week"]] == [("2026-11-15", 1)]
    s, e = rng("2026-11-14", 3)
    three = db.report_between(H1, s, e, 3)["emotion_week"]
    assert [x["date"] for x in three] == ["2026-11-12", "2026-11-14"] and "fear" not in three[0]


def test_empty_period(db):
    emo(db, H1, "happy", ts(11, 15, 9))
    db.record_event(H1, "dialogue", {"who": "user", "text": "안녕"}, ts(11, 15, 9))
    s, e = rng("2026-10-01", 7)
    r = db.report_between(H1, s, e, 7)
    assert r["emotion_week"] == [] and r["conversations"] == [] and r["alert_count"] == 0 and r["days"] == 7


def test_conversations_and_alert_count_follow_period(db):
    for i in range(35):
        db.record_event(H1, "dialogue", {"who": "user", "text": f"말 {i}"}, ts(11, 14, 9) + i)
    db.record_event(H1, "dialogue", {"who": "user", "text": "오늘 말"}, ts(11, 15, 9))
    db.alert_create(H1, "a-001", ts(11, 14, 10), "no_response", "t", {}, {})
    db.alert_create(H1, "a-002", ts(11, 15, 10), "danger", "t", {}, {})
    s, e = rng("2026-11-14", 1)
    r = db.report_between(H1, s, e, 1)
    assert len(r["conversations"]) == 30 and r["conversations"][-1]["data"]["text"] == "말 34"   # 기간 안 최신 30건, 시간순
    assert all(c["data"]["text"] != "오늘 말" for c in r["conversations"])
    assert r["alert_count"] == 1 and len(r["alerts"]) == 2                                   # alerts 목록은 기존처럼 최근 50건


def test_other_home_not_mixed(db):
    emo(db, H1, "happy", ts(11, 14, 9))
    emo(db, H2, "sad", ts(11, 14, 9))
    emo(db, H2, "angry", ts(11, 13, 9))
    s, e = rng("2026-11-14", 7)
    r = db.report_between(H1, s, e, 7)
    assert [(x["date"], x["happy"], x["sad"], x["angry"]) for x in r["emotion_week"]] == [("2026-11-14", 1, 0, 0)]


def test_read_only(db):
    emo(db, H1, "happy", ts(11, 14, 9))
    db.record_event(H1, "dialogue", {"who": "user", "text": "안녕"}, ts(11, 14, 9))
    n = lambda: db._one("SELECT COUNT(*) AS n FROM events")["n"]   # noqa: E731
    before = n()
    h = hashlib.sha256(open(db.path, "rb").read()).hexdigest()
    for end, days in (("2026-11-14", 1), ("2026-11-15", 7), ("2026-10-01", 30)):
        s, e = rng(end, days)
        db.report_between(H1, s, e, days)
    assert n() == before and hashlib.sha256(open(db.path, "rb").read()).hexdigest() == h


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
    u = core.db.create_user("m@x.com", "1234", "보호자")
    other = core.db.create_user("n@x.com", "1234", "다른 보호자")
    h1 = core.db.create_home("우리 집", u["id"])
    h2 = core.db.create_home("다른 집", other["id"])
    c = TestClient(app)
    c.cookies.set(COOKIE, core.db.create_session(u["id"]))
    return c, core.db, h1["id"], h2["id"]


def _day(off):
    return (datetime.now() - timedelta(days=off)).strftime("%Y-%m-%d")


def test_endpoint_without_end_is_unchanged(client):
    c, db, h1, h2 = client
    db.record_event(h1, "emotion", {"label": "happy"}, time.time() - 3600)
    db.record_event(h1, "dialogue", {"who": "user", "text": "안녕"}, time.time() - 60)
    r = c.get(f"/api/homes/{h1}/report", params={"days": "7"})
    assert r.status_code == 200
    j = r.json()
    assert set(j) == {"days", "emotion_week", "conversations", "alerts", "alert_count"}       # 새 필드 없음
    assert j == {**db.report(h1, 7), "conversations": j["conversations"]} and len(j["conversations"]) == 1
    assert all("date" not in row for row in j["emotion_week"])
    assert c.get(f"/api/homes/{h1}/report").json()["days"] == 7
    assert c.get(f"/api/homes/{h1}/report", params={"days": "0"}).status_code == 200            # 기존: 범위 검사 없음


def test_endpoint_with_end_fields(client):
    c, db, h1, h2 = client
    db.record_event(h1, "emotion", {"label": "sad"}, day_start(_day(1)) + 3600)
    db.record_event(h2, "emotion", {"label": "angry"}, day_start(_day(1)) + 3600)
    j = c.get(f"/api/homes/{h1}/report", params={"end": _day(1), "days": "3"}).json()
    assert (j["today"], j["start"], j["end"], j["is_today"], j["days"]) == (_day(0), _day(3), _day(1), False, 3)
    assert [(x["date"], x["sad"], x["angry"]) for x in j["emotion_week"]] == [(_day(1), 1, 0)]   # 다른 집 섞이지 않음
    t = c.get(f"/api/homes/{h1}/report", params={"end": _day(0), "days": "7"}).json()
    assert (t["is_today"], t["end"], t["start"]) == (True, _day(0), _day(6))
    assert {"days", "emotion_week", "conversations", "alerts", "alert_count"} <= set(t)


@pytest.mark.parametrize("params", [{"end": "2026/11/14"}, {"end": "abc"}, {"end": ""}, {"end": "2026-02-30"},
                                    {"end": "2026-13-01"}, {"end": "2026-11-01", "days": "0"},
                                    {"end": "2026-11-01", "days": "31"}, {"end": "2026-11-01", "days": "-1"}])
def test_endpoint_400(client, params):
    c, db, h1, h2 = client
    r = c.get(f"/api/homes/{h1}/report", params=params)
    assert r.status_code == 400 and "error" in r.json(), params


def test_endpoint_future_400(client):
    c, db, h1, h2 = client
    r = c.get(f"/api/homes/{h1}/report", params={"end": _day(-1)})
    assert r.status_code == 400 and "error" in r.json()


def test_endpoint_login_and_other_home(client):
    c, db, h1, h2 = client
    assert c.get(f"/api/homes/{h2}/report", params={"end": _day(1)}).status_code in (401, 403)
    assert c.get(f"/api/homes/{h2}/report").status_code in (401, 403)
    c.cookies.clear()
    assert c.get(f"/api/homes/{h1}/report", params={"end": _day(1)}).status_code in (401, 403)
