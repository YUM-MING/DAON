"""알림 이력 누락 수정 (server/db.py alert_create / alert_resolve). 로컬 전용 테스트.
서버 재시작 = 같은 DB 파일로 DB 객체를 새로 만드는 것. 임시 SQLite, 외부 호출 없음."""
from __future__ import annotations

import os
import tempfile

import pytest

from server.db import DB

HID = 1


@pytest.fixture
def path():
    return os.path.join(tempfile.mkdtemp(), "t.db")


def create(db, aid="a-001", ts=1000.0, cause="fall"):
    db.alert_create(HID, aid, ts, cause, "낙상", {"pose": "fall"}, {"pred": "fall"})


def rows(db, aid="a-001"):
    return [dict(r) for r in db._q("SELECT * FROM alerts WHERE home_id=? AND alert_id=? ORDER BY id", HID, aid)]


def test_resolved_then_same_id_makes_new_row(path):
    db = DB(path)
    create(db, ts=1000)
    db.alert_resolve(HID, "a-001", 1010, "elder_ok", "robot")
    create(db, ts=2000)                                     # 같은 실행 안에서도 해제된 뒤면 새 알림
    r = rows(db)
    assert len(r) == 2 and r[0]["resolved_ts"] == 1010 and r[1]["resolved_ts"] is None


def test_resolved_then_restart_same_id_makes_new_row(path):
    db = DB(path)
    create(db, ts=1000)
    db.alert_resolve(HID, "a-001", 1010, "elder_ok", "robot")
    db2 = DB(path)                                          # 재시작
    create(db2, ts=2000)
    assert len(rows(db2)) == 2


def test_open_old_session_kept_and_new_row(path):
    db = DB(path)
    create(db, ts=1000)                                     # 해제 안 된 채 서버 종료
    db2 = DB(path)
    create(db2, ts=2000)
    r = rows(db2)
    assert len(r) == 2 and r[0]["resolved_ts"] is None and r[1]["created_ts"] == 2000


def test_same_session_duplicate_one_row(path):
    db = DB(path)
    for i in range(4):                                      # raised → updated → realert … 같은 ID 반복 호출
        create(db, ts=1000 + i)
    r = rows(db)
    assert len(r) == 1 and r[0]["created_ts"] == 1000


def test_restart_then_duplicate_still_one_new_row(path):
    db = DB(path)
    create(db, ts=1000)
    db2 = DB(path)
    create(db2, ts=2000)
    create(db2, ts=2001)
    assert len(rows(db2)) == 2


def test_resolve_hits_current_session_row_only(path):
    db = DB(path)
    create(db, ts=1000)
    db2 = DB(path)
    create(db2, ts=2000)
    db2.alert_resolve(HID, "a-001", 2010, "guardian_confirmed", "guardian_1")
    old, new = rows(db2)
    assert old["resolved_ts"] is None and old["resolution"] is None
    assert new["resolved_ts"] == 2010 and new["resolution"] == "guardian_confirmed" and new["handled_by"] == "guardian_1"


def test_resolve_without_current_session_row_changes_nothing(path):
    db = DB(path)
    create(db, ts=1000)
    before = rows(db)
    db2 = DB(path)
    db2.alert_resolve(HID, "a-001", 2010, "false_alarm", "g")
    assert rows(db2) == before


def test_resolve_twice_keeps_first_resolution(path):
    db = DB(path)
    create(db, ts=1000)
    db.alert_resolve(HID, "a-001", 1010, "elder_ok", "robot")
    db.alert_resolve(HID, "a-001", 1020, "false_alarm", "g")  # 이미 해제됨 → 변화 없음 (기존 동작과 같음)
    r = rows(db)[0]
    assert (r["resolved_ts"], r["resolution"]) == (1010, "elder_ok")


def test_fake_clock_older_than_wall_time(path):
    """created_ts 가 실제 시각보다 과거여도(테스트 시계·시계 차이) 같은 실행이면 정상 동작."""
    db = DB(path)
    create(db, ts=1.0)
    create(db, ts=2.0)
    db.alert_resolve(HID, "a-001", 3.0, "elder_ok", "robot")
    r = rows(db)
    assert len(r) == 1 and r[0]["resolved_ts"] == 3.0


def test_other_home_and_other_id_independent(path):
    db = DB(path)
    create(db, aid="a-001")
    db.alert_create(2, "a-001", 1000, "fall", "낙상", {}, {})
    create(db, aid="a-002")
    db.alert_resolve(HID, "a-001", 1010, "elder_ok", "robot")
    assert rows(db, "a-002")[0]["resolved_ts"] is None
    assert db.alerts(2)[0]["resolved_ts"] is None


def test_existing_db_rows_preserved(path):
    db = DB(path)
    for i in range(3):
        create(db, aid=f"a-00{i + 1}", ts=1000 + i)
    db.alert_resolve(HID, "a-002", 1100, "elder_ok", "robot")
    before = db.alerts(HID)
    db2 = DB(path)                                          # 기존 파일 열기 (스키마 재실행)
    assert db2.alerts(HID) == before
    create(db2, aid="a-001", ts=2000)
    after = db2.alerts(HID)
    assert len(after) == 4 and after[1:] == before          # 새 행만 추가, 기존 행 그대로
