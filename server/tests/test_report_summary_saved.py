"""저장된 AI 요약 읽기 전용 (Summarizer.saved, GET /report/summary/saved). 임시 SQLite, LLM 은 Mock (호출 0회 단언).
기간 = 기준일 포함 days 일 (서버 로컬 날짜, [시작, 끝)). 상태: cached(저장본) / too_few / none."""
from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import datetime, timedelta

import pytest

from server import config as cfgmod
from server.core.summarizer import Summarizer
from server.db import DB

NOW = datetime(2026, 11, 15, 15, 0, 0).timestamp()      # 서버 로컬 오늘 11/15 오후 3시
TODAY = "2026-11-15"
H1, H2 = 1, 2


def ts(m, d, hh=12, mm=0, ss=0):
    return datetime(2026, m, d, hh, mm, ss).timestamp()


class NoLLM:
    """client_factory Mock: 불리면 횟수만 센다 (saved 는 절대 부르면 안 됨)"""
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        raise AssertionError("LLM 클라이언트를 만들면 안 됨")


@pytest.fixture
def env():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.db")
    db = DB(path)
    cfg = cfgmod.load(None, mock=True, overrides={"logs": {"dir": d}})["summary"]
    llm = NoLLM()
    return db, Summarizer(db, cfg, client_factory=llm, clock=lambda: NOW), llm, path, cfg


def talk(db, hid, n, t0, gap=30):
    for i in range(n):
        db.record_event(hid, "dialogue", {"who": "user" if i % 2 == 0 else "robot", "text": f"대화 {i}"}, t0 + i * gap)


def by_day(r):
    return {it["day"]: it for it in r["items"]}


def seed(db):
    talk(db, H1, 5, ts(11, 14, 10))                 # 11/14 저장본 있음
    db.summary_put(H1, "2026-11-14", 999, 5, "11/14 요약", ts(11, 14, 22))
    talk(db, H1, 6, ts(11, 13, 10))                 # 11/13 대화 충분 · 저장본 없음 → none
    talk(db, H1, 2, ts(11, 12, 10))                 # 11/12 대화 적지만 저장본 있음 → cached 우선
    db.summary_put(H1, "2026-11-12", 999, 2, "11/12 요약", ts(11, 12, 22))
    talk(db, H1, 3, ts(11, 11, 10))                 # 11/11 대화 3건 → too_few (min 4)
    db.summary_put(H1, "2026-11-05", 999, 9, "기간 밖 요약", ts(11, 5, 22))
    talk(db, H2, 8, ts(11, 13, 10))                 # 다른 집
    db.summary_put(H2, "2026-11-13", 999, 8, "다른 집 요약", ts(11, 13, 22))
    db.summary_put(H2, "2026-11-14", 999, 8, "다른 집 요약 2", ts(11, 14, 22))


# ───────── 기간 · 상태 ─────────
def test_items_and_statuses(env):
    db, sm, llm, _, cfg = env
    seed(db)
    r = sm.saved(H1, 7, NOW, "2026-11-14")
    assert (r["today"], r["start"], r["end"], r["is_today"], r["days"]) == (TODAY, "2026-11-08", "2026-11-14", False, 7)
    assert [it["day"] for it in r["items"]] == [(datetime(2026, 11, 14) - timedelta(days=i)).strftime("%Y-%m-%d")
                                                for i in range(7)]          # 최신 날짜 먼저
    d = by_day(r)
    assert (d["2026-11-14"]["status"], d["2026-11-14"]["text"], d["2026-11-14"]["count"]) == ("cached", "11/14 요약", 5)
    assert d["2026-11-14"]["created_ts"] == ts(11, 14, 22) and d["2026-11-14"]["saved"] is True
    assert (d["2026-11-13"]["status"], d["2026-11-13"]["count"], d["2026-11-13"]["text"]) == ("none", 6, None)
    assert (d["2026-11-12"]["status"], d["2026-11-12"]["count"]) == ("cached", 2)        # 저장본 우선
    assert (d["2026-11-11"]["status"], d["2026-11-11"]["count"]) == ("too_few", 3)
    assert (d["2026-11-08"]["status"], d["2026-11-08"]["count"]) == ("too_few", 0)
    assert llm.calls == 0


def test_other_home_never_mixed(env):
    db, sm, llm, _, _ = env
    seed(db)
    for end in (None, "2026-11-14", "2026-11-13"):
        r = sm.saved(H1, 7, NOW, end)
        texts = [it["text"] for it in r["items"] if it["text"]]
        assert all("다른 집" not in x for x in texts), end
    d = by_day(sm.saved(H1, 7, NOW, "2026-11-14"))
    assert d["2026-11-13"]["count"] == 6                               # 다른 집 대화 8건이 섞이지 않음
    d2 = by_day(sm.saved(H2, 7, NOW, "2026-11-14"))
    assert d2["2026-11-13"]["text"] == "다른 집 요약" and d2["2026-11-12"]["status"] == "too_few"


def test_default_is_today(env):
    db, sm, llm, _, _ = env
    seed(db)
    talk(db, H1, 4, ts(11, 15, 9))
    r = sm.saved(H1, 7, NOW)
    assert (r["end"], r["is_today"], r["start"]) == (TODAY, True, "2026-11-09")
    assert r["items"][0] == {"day": TODAY, "status": "none", "saved": False, "count": 4, "text": None}
    assert sm.saved(H1, 7, NOW, TODAY) == r


def test_boundary_days(env):
    db, sm, llm, _, _ = env
    talk(db, H1, 4, ts(11, 14, 23, 59, 50), gap=2)                   # 23:59:50~23:59:56 → 11/14
    talk(db, H1, 4, ts(11, 15, 0, 0, 0), gap=2)                      # 00:00:00~ → 11/15
    db.summary_put(H1, "2026-11-11", 1, 4, "경계 밖", ts(11, 11, 22))
    db.summary_put(H1, "2026-11-12", 1, 4, "첫날", ts(11, 12, 22))
    r = sm.saved(H1, 3, NOW, "2026-11-14")
    assert [it["day"] for it in r["items"]] == ["2026-11-14", "2026-11-13", "2026-11-12"]
    d = by_day(r)
    assert d["2026-11-14"]["count"] == 4 and d["2026-11-12"]["text"] == "첫날"
    assert all(it["text"] != "경계 밖" for it in r["items"])
    one = sm.saved(H1, 1, NOW, TODAY)
    assert [(it["day"], it["count"]) for it in one["items"]] == [(TODAY, 4)]


def test_count_matches_summarize_card(env):
    """'대화 N건'이 summarize() 카드와 같은 계산 (저장본이 최신이면 summarize 도 LLM 없이 cached)"""
    db, sm, llm, _, cfg = env
    talk(db, H1, 7, ts(11, 15, 9))
    last = max(e["id"] for e in db.dialogues_between(H1, ts(11, 15, 0), ts(11, 16, 0), 1000))
    db.summary_put(H1, TODAY, last, 7, "오늘 요약", ts(11, 15, 14))
    gen = sm.summarize(H1, "today")["days"][0]
    got = sm.saved(H1, 1, NOW)["items"][0]
    assert gen["status"] == "cached" and (got["count"], got["text"], got["created_ts"]) == (gen["count"], gen["text"],
                                                                                            gen["created_ts"])
    assert llm.calls == 0


def test_count_capped_like_summarize(env):
    db, sm, llm, _, cfg = env
    talk(db, H1, int(cfg["max_messages"]) + 5, ts(11, 14, 1), gap=1)
    assert sm.saved(H1, 1, NOW, "2026-11-14")["items"][0]["count"] == int(cfg["max_messages"])


def test_too_few_threshold_matches_config(env):
    db, sm, llm, _, cfg = env
    k = int(cfg["min_messages"])
    talk(db, H1, k - 1, ts(11, 13, 9))
    talk(db, H1, k, ts(11, 14, 9))
    d = by_day(sm.saved(H1, 2, NOW, "2026-11-14"))
    assert d["2026-11-13"]["status"] == "too_few" and d["2026-11-14"]["status"] == "none"


# ───────── 읽기 전용 ─────────
def test_read_only_no_writes_no_llm(env):
    db, sm, llm, path, _ = env
    seed(db)
    counts = lambda: tuple(db._one(f"SELECT COUNT(*) AS n FROM {t}")["n"] for t in ("summaries", "summary_calls", "events"))  # noqa: E731
    rows = lambda: [tuple(r) for r in db._q("SELECT * FROM summaries ORDER BY home_id, day")]  # noqa: E731
    before, rows_before = counts(), rows()
    h = hashlib.sha256(open(path, "rb").read()).hexdigest()
    for end in (None, "2026-11-14", "2026-11-01"):
        for days in (1, 3, 7, 30):
            sm.saved(H1, days, NOW, end)
    assert counts() == before and rows() == rows_before
    assert db.summary_calls_since(H1, 0) == 0                          # 하루 호출 한도 소모 없음
    assert hashlib.sha256(open(path, "rb").read()).hexdigest() == h    # 파일 자체도 그대로
    assert llm.calls == 0


# ───────── 엔드포인트 ─────────
@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from server.auth import COOKIE
    from server.main import create_app
    d = tempfile.mkdtemp()
    cfg = cfgmod.load(None, mock=True, overrides={"logs": {"dir": d}, "modules": {"dialogue": {"mode": "off"}}})
    app = create_app(cfg)
    core = app.state.core
    llm = NoLLM()
    core.summarizer.client_factory = llm
    u = core.db.create_user("s@x.com", "1234", "보호자")
    other = core.db.create_user("o@x.com", "1234", "다른 보호자")
    h1 = core.db.create_home("우리 집", u["id"])
    h2 = core.db.create_home("다른 집", other["id"])
    c = TestClient(app)
    c.cookies.set(COOKIE, core.db.create_session(u["id"]))
    return c, core.db, h1["id"], h2["id"], llm


def _day(off):
    return (datetime.now() - timedelta(days=off)).strftime("%Y-%m-%d")


def test_endpoint_ok_default_and_end(client):
    c, db, h1, h2, llm = client
    db.summary_put(h1, _day(1), 1, 5, "어제 요약", 1.0)
    db.summary_put(h2, _day(1), 1, 5, "다른 집 어제", 1.0)
    r = c.get(f"/api/homes/{h1}/report/summary/saved")
    assert r.status_code == 200
    j = r.json()
    assert (j["today"], j["end"], j["is_today"], j["days"], len(j["items"])) == (_day(0), _day(0), True, 7, 7)
    assert j["items"][1]["text"] == "어제 요약" and j["items"][1]["status"] == "cached"
    j = c.get(f"/api/homes/{h1}/report/summary/saved", params={"end": _day(1), "days": "1"}).json()
    assert (j["start"], j["end"], j["is_today"]) == (_day(1), _day(1), False)
    assert [(it["day"], it["text"]) for it in j["items"]] == [(_day(1), "어제 요약")]
    assert llm.calls == 0 and db.summary_calls_since(h1, 0) == 0


@pytest.mark.parametrize("params", [{"end": "2026/11/14"}, {"end": "abc"}, {"end": ""}, {"end": "2026-02-30"},
                                    {"end": "2026-13-01"}, {"days": "0"}, {"days": "31"}, {"days": "x"}])
def test_endpoint_400(client, params):
    c, db, h1, h2, llm = client
    r = c.get(f"/api/homes/{h1}/report/summary/saved", params=params)
    assert r.status_code == 400 and "error" in r.json(), params


def test_endpoint_future_400(client):
    c, db, h1, h2, llm = client
    r = c.get(f"/api/homes/{h1}/report/summary/saved", params={"end": _day(-1)})
    assert r.status_code == 400 and "error" in r.json()


def test_endpoint_requires_login_and_membership(client):
    c, db, h1, h2, llm = client
    assert c.get(f"/api/homes/{h2}/report/summary/saved").status_code in (401, 403)     # 다른 집
    c.cookies.clear()
    assert c.get(f"/api/homes/{h1}/report/summary/saved").status_code in (401, 403)     # 로그인 안 함


def test_existing_summary_api_unchanged(client):
    """기존 POST /report/summary 는 그대로 (대화가 적으면 LLM 없이 too_few, scope 검증 400)"""
    c, db, h1, h2, llm = client
    r = c.post(f"/api/homes/{h1}/report/summary", json={"scope": "today"})
    assert r.status_code == 200 and r.json()["scope"] == "today" and r.json()["days"][0]["status"] == "too_few"
    assert c.post(f"/api/homes/{h1}/report/summary", json={"scope": "5"}).status_code == 400
    assert llm.calls == 0
