"""리포트 기준일 선택 (GET /report/stats·/report/insights 의 end 파라미터). 임시 SQLite, 외부 LLM 호출 없음.
기간 = 기준일 포함 days 일 (서버 로컬 날짜, [시작, 끝) 경계). end 가 없으면 기존(오늘 기준) 동작 그대로."""
from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime, timedelta

import pytest

from server.core import report_insights as ri
from server.core import report_stats as rs
from server.db import DB

HID = 1
NOW = datetime(2026, 11, 15, 15, 0, 0).timestamp()       # 서버 로컬 오늘 11/15 오후 3시
TODAY, YESTERDAY = "2026-11-15", "2026-11-14"


def ts(y, m, d, hh=12, mm=0, ss=0.0):
    return datetime(y, m, d, hh, mm, 0).timestamp() + ss


@pytest.fixture
def db():
    return DB(os.path.join(tempfile.mkdtemp(), "t.db"))


def say(db, text, t, who="user"):
    db.record_event(HID, "dialogue", {"who": who, "text": text}, t)


def users(r):
    return {c["day"]: c["user_msgs"] for c in r["conversations"]}


# ───────── parse_end ─────────
def test_parse_end_none_today_and_past():
    assert rs.parse_end(None, NOW) is None
    assert rs.parse_end(TODAY, NOW) == TODAY                          # 오늘은 허용
    assert rs.parse_end(" 2026-11-14 ", NOW) == YESTERDAY
    assert rs.parse_end("2024-02-29", NOW) == "2024-02-29"            # 윤년의 실제 날짜


@pytest.mark.parametrize("raw", ["", " ", "2026/11/14", "20261114", "2026-1-5", "26-11-14", "abc", "2026-11-14T00:00",
                                 "2026-11-14x"])
def test_parse_end_format_error(raw):
    with pytest.raises(ValueError, match="형식"):
        rs.parse_end(raw, NOW)


@pytest.mark.parametrize("raw", ["2026-02-30", "2026-02-29", "2026-13-01", "2026-11-31", "2026-00-10", "2026-04-31"])
def test_parse_end_nonexistent_date(raw):
    with pytest.raises(ValueError, match="실제로 있는 날짜"):
        rs.parse_end(raw, NOW)


@pytest.mark.parametrize("raw", ["2026-11-16", "2027-01-01"])
def test_parse_end_future(raw):
    with pytest.raises(ValueError, match="오늘"):
        rs.parse_end(raw, NOW)


def test_parse_end_future_follows_injected_now():
    late = datetime(2026, 11, 15, 23, 59, 59).timestamp()             # 같은 날 밤: 11/16 은 아직 미래
    with pytest.raises(ValueError):
        rs.parse_end("2026-11-16", late)
    assert rs.parse_end("2026-11-16", late + 1) == "2026-11-16"       # 자정이 지나면 오늘


# ───────── period · previous_period ─────────
def test_period_default_equals_today_and_explicit_today():
    assert rs.period(7, NOW) == rs.period(7, NOW, None) == rs.period(7, NOW, TODAY)
    assert rs.previous_period(7, NOW) == rs.previous_period(7, NOW, TODAY)
    f, t, start, stop, names = rs.period(7, NOW)
    assert (f, t, len(names)) == ("2026-11-09", TODAY, 7)
    assert (start, stop) == (ts(2026, 11, 9, 0), ts(2026, 11, 16, 0))


@pytest.mark.parametrize("end,days,frm", [(YESTERDAY, 7, "2026-11-08"), ("2026-11-08", 7, "2026-11-02"),
                                          (YESTERDAY, 1, YESTERDAY), (YESTERDAY, 3, "2026-11-12"),
                                          ("2026-03-01", 3, "2026-02-27")])
def test_period_includes_end_day(end, days, frm):
    f, t, start, stop, names = rs.period(days, NOW, end)
    assert (f, t, len(names)) == (frm, end, days)
    assert stop - start == pytest.approx(days * 86400) and names[-1] == end
    assert datetime.fromtimestamp(stop).strftime("%Y-%m-%d %H:%M") == \
        (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d 00:00")


def test_previous_period_moves_with_end():
    assert rs.previous_period(7, NOW, YESTERDAY)[:2] == ("2026-11-01", "2026-11-07")
    assert rs.previous_period(7, NOW)[:2] == ("2026-11-02", "2026-11-08")
    assert rs.previous_period(1, NOW, YESTERDAY)[:2] == ("2026-11-13", "2026-11-13")
    _, _, ps, pe, _ = rs.previous_period(3, NOW, YESTERDAY)
    assert pe == rs.period(3, NOW, YESTERDAY)[2]                       # 직전 기간 끝 = 현재 기간 시작 (빈틈·겹침 없음)


# ───────── compute (통계) ─────────
def test_stats_default_unchanged(db):
    say(db, "무릎이 좀 아프네", ts(2026, 11, 15, 9))
    say(db, "밥 먹었어", ts(2026, 11, 10, 9))
    assert rs.compute(db, HID, 7, NOW) == rs.compute(db, HID, 7, NOW, end=None) == rs.compute(db, HID, 7, NOW, end=TODAY)


def test_stats_yesterday_and_week_ago(db):
    say(db, "밥 먹었어", ts(2026, 11, 15, 9))                           # 오늘 (어제 기준이면 제외)
    say(db, "무릎이 좀 아프네", ts(2026, 11, 14, 9))
    say(db, "잠을 잘 못 잤어", ts(2026, 11, 8, 9))
    say(db, "아직 밥 안 먹었어", ts(2026, 11, 7, 9))
    r = rs.compute(db, HID, 7, NOW, end=YESTERDAY)
    assert (r["from"], r["to"]) == ("2026-11-08", YESTERDAY)
    assert sum(users(r).values()) == 2 and r["analyzed"] == 2
    assert r["meal"]["done"] == 0 and r["meal"]["not"] == 0 and r["sleep"]["bad"] == 1
    assert r["symptoms"][0]["part"] == "무릎"
    r = rs.compute(db, HID, 1, NOW, end="2026-11-08")                   # 7일 전 하루
    assert users(r) == {"2026-11-08": 1} and r["sleep"]["bad"] == 1
    r = rs.compute(db, HID, 7, NOW, end="2026-11-08")
    assert (r["from"], r["to"]) == ("2026-11-02", "2026-11-08") and r["meal"]["not"] == 1


def test_stats_boundary_days(db):
    # 기간 첫날 00:00:00 포함, 그 직전 제외 / 기준일 23:59:59 포함, 다음 날 00:00:00 제외 (서로 다른 문장: 중복 제거 회피)
    say(db, "비가 오네", ts(2026, 11, 7, 23, 59, 59))
    say(db, "바람이 시원하네", ts(2026, 11, 8, 0, 0, 0))
    say(db, "꽃이 예쁘게 피었네", ts(2026, 11, 14, 23, 59, 59))
    say(db, "텔레비전 보고 있어", ts(2026, 11, 15, 0, 0, 0))
    r = rs.compute(db, HID, 7, NOW, end=YESTERDAY)
    assert users(r)["2026-11-08"] == 1 and users(r)["2026-11-14"] == 1 and sum(users(r).values()) == 2


def test_stats_midnight_before_after(db):
    say(db, "점심은 먹었어", ts(2026, 11, 14, 23, 59, 59) + 0.5)        # 11/14 23:59:59.5
    say(db, "어젯밤에 잠을 잘 못 잤어", ts(2026, 11, 15, 0, 0, 0))      # 11/15 00:00:00
    a = rs.compute(db, HID, 1, NOW, end=YESTERDAY)
    b = rs.compute(db, HID, 1, NOW, end=TODAY)
    assert users(a) == {YESTERDAY: 1} and a["meal"]["done"] == 1 and a["sleep"]["bad"] == 0
    assert users(b) == {TODAY: 1} and b["meal"]["done"] == 0 and b["sleep"]["bad"] == 1
    two = rs.compute(db, HID, 2, NOW)
    assert users(two) == {YESTERDAY: 1, TODAY: 1}


def test_stats_empty_range(db):
    say(db, "밥 먹었어", ts(2026, 11, 15, 9))
    r = rs.compute(db, HID, 7, NOW, end="2026-10-01")
    assert (r["from"], r["to"]) == ("2026-09-25", "2026-10-01") and len(r["conversations"]) == 7
    assert r["analyzed"] == 0 and sum(users(r).values()) == 0 and r["symptoms"] == []


def test_stats_data_preserved_after_queries(db):
    """조회는 읽기만 한다: 여러 기준일로 조회한 뒤에도 저장된 이벤트 수가 그대로"""
    for d in range(8, 16):
        say(db, "밥 먹었어", ts(2026, 11, d, 9))
    before = len(db.report_dialogue_events(HID, 0, ts(2027, 1, 1)))
    for end in (None, TODAY, YESTERDAY, "2026-11-08", "2026-10-01"):
        rs.compute(db, HID, 7, NOW, end=end)
        ri.compute(db, HID, 7, NOW, end=end)
    assert len(db.report_dialogue_events(HID, 0, ts(2027, 1, 1))) == before == 8


# ───────── insights ─────────
def test_insights_default_unchanged(db):
    for i, d in enumerate((10, 12, 14)):
        say(db, "어젯밤에 잠을 잘 못 잤어", ts(2026, 11, d, 9))
        say(db, f"비가 오네 {i}", ts(2026, 11, d, 10))
    assert ri.compute(db, HID, 7, NOW) == ri.compute(db, HID, 7, NOW, end=None) == ri.compute(db, HID, 7, NOW, end=TODAY)
    r = ri.compute(db, HID, 7, NOW)
    assert "최근 7일간" in r["summary"] or all("최근 7일간" in s["detail"] for s in r["signals"])
    assert all("선택한" not in s["detail"] for s in r["signals"]) and "선택한" not in r["summary"]


def test_insights_phrase_changes_when_not_today(db):
    r = ri.compute(db, HID, 7, NOW, end=YESTERDAY)                   # 빈 범위 → 판단 보류 문장
    assert r["level"] == "판단 보류"
    assert r["summary"].startswith("선택한 7일간(11/8~11/14) 분석된 어르신 발화가 0건")
    assert ri.compute(db, HID, 7, NOW)["summary"].startswith("최근 7일간 분석된 어르신 발화가 0건")
    assert ri.compute(db, HID, 7, NOW, end=TODAY)["summary"].startswith("최근 7일간")
    assert ri.compute(db, HID, 1, NOW, end=YESTERDAY)["summary"].startswith("선택한 1일간(11/14) ")


def test_insights_signal_detail_phrase_and_range(db):
    for d in (10, 11, 12):
        say(db, "어젯밤에 잠을 잘 못 잤어", ts(2026, 11, d, 9))
    say(db, "어젯밤에 잠을 잘 못 잤어", ts(2026, 11, 15, 9))           # 오늘: 어제 기준에서는 빠짐
    r = ri.compute(db, HID, 7, NOW, end=YESTERDAY)
    s = next(x for x in r["signals"] if x["key"] == "sleep_bad")
    assert s["current"] == 3 and s["detail"].startswith("선택한 7일간(11/8~11/14) 수면 부족을")
    assert (r["from"], r["to"]) == ("2026-11-08", YESTERDAY) and len(r["daily"]) == 7
    d = next(x for x in ri.compute(db, HID, 7, NOW)["signals"] if x["key"] == "sleep_bad")
    assert d["current"] == 4 and d["detail"].startswith("최근 7일간 수면 부족을")


def test_insights_previous_period_moves(db):
    for i in range(6):                                                  # 11/1 에만 분석 발화 6건
        say(db, ["비가 오네", "텔레비전 보고 있어", "오늘 하늘이 맑네", "바람이 시원하네", "꽃이 예쁘게 피었네",
                 "단호박죽 먹고 싶어"][i], ts(2026, 11, 1, 9, i))
    a = ri.compute(db, HID, 7, NOW, end=YESTERDAY)                     # 직전 기간 11/1~11/7 → 비교 가능
    b = ri.compute(db, HID, 7, NOW)                                    # 직전 기간 11/2~11/8 → 비교 불가
    assert a["comparison"]["available"] is True and a["comparison"]["previous_analyzed"] == 6
    assert b["comparison"]["available"] is False and b["comparison"]["previous_analyzed"] == 0


def test_insights_one_day_is_pending(db):
    lines = ["어젯밤에 잠을 잘 못 잤어", "아직 아침을 안 먹었어", "비가 오네", "텔레비전 보고 있어", "오늘 하늘이 맑네",
             "바람이 시원하네", "꽃이 예쁘게 피었네"]
    for i, s in enumerate(lines):
        say(db, s, ts(2026, 11, 14, 9, i))
    for end in (YESTERDAY, None):
        r = ri.compute(db, HID, 1, NOW if end else ts(2026, 11, 14, 20), end=end)
        assert r["level"] == "판단 보류" and r["sufficiency"]["analyzed"] == 7
        assert r["sufficiency"]["days_with_data"] == 1 and r["sufficiency"]["enough"] is False


def test_insights_empty_range(db):
    r = ri.compute(db, HID, 3, NOW, end="2026-10-01")
    assert r["level"] == "판단 보류" and r["signals"] == [] and r["alerts"]["count"] == 0
    assert (r["from"], r["to"]) == ("2026-09-29", "2026-10-01")


def test_insights_alerts_follow_range(db):
    db.alert_create(HID, "a-001", ts(2026, 11, 14, 10), "no_response", "t", {}, {})
    db.alert_create(HID, "a-002", ts(2026, 11, 15, 10), "danger", "t", {}, {})
    assert ri.compute(db, HID, 1, NOW, end=YESTERDAY)["alerts"]["count"] == 1
    assert ri.compute(db, HID, 1, NOW)["alerts"]["count"] == 1
    assert ri.compute(db, HID, 2, NOW)["alerts"]["count"] == 2


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
    u = core.db.create_user("e@x.com", "1234", "보호자")
    h = core.db.create_home("우리 집", u["id"])
    c = TestClient(app)
    c.cookies.set(COOKIE, core.db.create_session(u["id"]))
    return c, core.db, h["id"]


PATHS = ("stats", "insights")


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("bad", ["2026/11/14", "abc", "", "2026-02-30", "2026-13-01"])
def test_endpoint_bad_end_400(client, path, bad):
    c, db, hid = client
    r = c.get(f"/api/homes/{hid}/report/{path}", params={"end": bad})
    assert r.status_code == 400 and "error" in r.json(), bad


@pytest.mark.parametrize("path", PATHS)
def test_endpoint_future_end_400(client, path):
    c, db, hid = client
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    r = c.get(f"/api/homes/{hid}/report/{path}", params={"end": tomorrow})
    assert r.status_code == 400 and "error" in r.json()


@pytest.mark.parametrize("path", PATHS)
def test_endpoint_range_fields(client, path):
    c, db, hid = client
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    db.record_event(hid, "dialogue", {"who": "user", "text": "밥 먹었어"}, time.time() - 60)
    base = c.get(f"/api/homes/{hid}/report/{path}").json()
    assert (base["today"], base["end"], base["is_today"], base["to"], base["days"]) == (today, today, True, today, 7)
    same = c.get(f"/api/homes/{hid}/report/{path}", params={"end": today}).json()
    assert same == base                                                    # end=오늘 = 기본과 같은 응답
    r = c.get(f"/api/homes/{hid}/report/{path}", params={"end": yesterday, "days": "3"})
    assert r.status_code == 200
    j = r.json()
    assert (j["today"], j["end"], j["is_today"], j["to"], j["days"]) == (today, yesterday, False, yesterday, 3)


@pytest.mark.parametrize("path", PATHS)
def test_endpoint_days_still_validated_with_end(client, path):
    c, db, hid = client
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    for bad in ("0", "31", "x"):
        assert c.get(f"/api/homes/{hid}/report/{path}", params={"end": yesterday, "days": bad}).status_code == 400


def test_endpoint_old_fields_kept(client):
    c, db, hid = client
    st = c.get(f"/api/homes/{hid}/report/stats").json()
    assert {"days", "from", "to", "analyzed", "conversations", "sleep", "meal", "symptoms", "emotion_text", "topics",
            "wants", "basis", "version", "skipped"} <= set(st)
    ins = c.get(f"/api/homes/{hid}/report/insights").json()
    assert {"days", "from", "to", "level", "summary", "signals", "alerts", "changes", "recommendations", "sufficiency",
            "daily", "basis", "version", "disclaimer", "comparison"} <= set(ins)


def test_endpoint_requires_member_with_end(client):
    c, db, hid = client
    c.cookies.clear()
    for path in PATHS:
        assert c.get(f"/api/homes/{hid}/report/{path}", params={"end": "2026-11-01"}).status_code in (401, 403)
