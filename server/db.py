"""SQLite 저장소: 계정 · 집(코드) · 로봇 토큰 · 세션 · 알림/이벤트 기록 (리포트용).

표준 라이브러리 sqlite3 만 사용. 파일 하나(`logs/daon.db`)에 저장.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL, salt TEXT NOT NULL,
    name TEXT NOT NULL, is_dev INTEGER DEFAULT 0, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS homes (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, code TEXT UNIQUE NOT NULL, owner_id INTEGER NOT NULL,
    is_demo INTEGER DEFAULT 0, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS members (
    user_id INTEGER NOT NULL, home_id INTEGER NOT NULL, role TEXT NOT NULL DEFAULT 'guardian',
    PRIMARY KEY (user_id, home_id));
CREATE TABLE IF NOT EXISTS robots (
    id INTEGER PRIMARY KEY, home_id INTEGER NOT NULL, token TEXT UNIQUE NOT NULL, name TEXT,
    created REAL NOT NULL, last_seen REAL);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created REAL NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY, home_id INTEGER NOT NULL, alert_id TEXT NOT NULL, created_ts REAL NOT NULL,
    resolved_ts REAL, resolution TEXT, handled_by TEXT, cause TEXT, title TEXT, sensors TEXT, fused TEXT);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, home_id INTEGER NOT NULL, ts REAL NOT NULL, type TEXT NOT NULL, data TEXT);
CREATE INDEX IF NOT EXISTS idx_events_home_ts ON events(home_id, ts);
CREATE INDEX IF NOT EXISTS idx_alerts_home ON alerts(home_id, created_ts);
"""

SESSION_TTL = 30 * 24 * 3600


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000).hex()


class DB:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # ---------------- 공통 ----------------
    def _q(self, sql: str, *args) -> list[sqlite3.Row]:
        with self._lock:
            cur = self.conn.execute(sql, args)
            rows = cur.fetchall()
            self.conn.commit()
            return rows

    def _one(self, sql: str, *args) -> Optional[sqlite3.Row]:
        rows = self._q(sql, *args)
        return rows[0] if rows else None

    def _insert(self, sql: str, *args) -> int:
        with self._lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return int(cur.lastrowid)

    @staticmethod
    def row(r: Optional[sqlite3.Row], *hide: str) -> Optional[dict]:
        if r is None:
            return None
        return {k: r[k] for k in r.keys() if k not in hide}

    # ---------------- 사용자 ----------------
    def create_user(self, email: str, password: str, name: str, is_dev: bool = False) -> dict:
        email = email.strip().lower()
        if self.get_user_by_email(email):
            raise ValueError("이미 가입된 이메일입니다")
        if len(password) < 4:
            raise ValueError("비밀번호는 4자 이상")
        salt = secrets.token_hex(8)
        uid = self._insert("INSERT INTO users(email,pw_hash,salt,name,is_dev,created) VALUES(?,?,?,?,?,?)",
                           email, _hash(password, salt), salt, name.strip() or email.split("@")[0], int(is_dev), time.time())
        return self.get_user(uid)

    def get_user(self, uid: int) -> Optional[dict]:
        return self.row(self._one("SELECT * FROM users WHERE id=?", uid), "pw_hash", "salt")

    def get_user_by_email(self, email: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM users WHERE email=?", email.strip().lower())

    def verify_login(self, email: str, password: str) -> Optional[dict]:
        r = self.get_user_by_email(email)
        if r is None or _hash(password, r["salt"]) != r["pw_hash"]:
            return None
        return self.row(r, "pw_hash", "salt")

    # ---------------- 세션 ----------------
    def create_session(self, uid: int) -> str:
        tok = secrets.token_urlsafe(32)
        now = time.time()
        self._insert("INSERT INTO sessions(token,user_id,created,expires) VALUES(?,?,?,?)", tok, uid, now, now + SESSION_TTL)
        return tok

    def session_user(self, token: str | None) -> Optional[dict]:
        if not token:
            return None
        r = self._one("SELECT * FROM sessions WHERE token=? AND expires>?", token, time.time())
        return self.get_user(r["user_id"]) if r else None

    def delete_session(self, token: str | None) -> None:
        if token:
            self._q("DELETE FROM sessions WHERE token=?", token)

    # ---------------- 집 ----------------
    def _new_code(self) -> str:
        while True:
            code = f"{secrets.randbelow(900000) + 100000}"
            if not self._one("SELECT 1 FROM homes WHERE code=?", code):
                return code

    def create_home(self, name: str, owner_id: int, code: str | None = None, is_demo: bool = False) -> dict:
        code = code or self._new_code()
        hid = self._insert("INSERT INTO homes(name,code,owner_id,is_demo,created) VALUES(?,?,?,?,?)",
                           name.strip() or "우리 집", code, owner_id, int(is_demo), time.time())
        self._q("INSERT OR IGNORE INTO members(user_id,home_id,role) VALUES(?,?,?)", owner_id, hid, "owner")
        return self.get_home(hid)

    def get_home(self, hid: int) -> Optional[dict]:
        return self.row(self._one("SELECT * FROM homes WHERE id=?", hid))

    def home_by_code(self, code: str) -> Optional[dict]:
        return self.row(self._one("SELECT * FROM homes WHERE code=?", code.strip()))

    def homes_for_user(self, uid: int) -> list[dict]:
        rows = self._q("SELECT h.*, m.role FROM homes h JOIN members m ON m.home_id=h.id WHERE m.user_id=? ORDER BY h.id", uid)
        return [self.row(r) for r in rows]

    def is_member(self, uid: int, hid: int) -> bool:
        return self._one("SELECT 1 FROM members WHERE user_id=? AND home_id=?", uid, hid) is not None

    def join_home(self, uid: int, code: str) -> dict:
        h = self.home_by_code(code)
        if not h:
            raise ValueError("존재하지 않는 집 코드입니다")
        self._q("INSERT OR IGNORE INTO members(user_id,home_id,role) VALUES(?,?,?)", uid, h["id"], "guardian")
        return h

    def all_homes(self) -> list[dict]:
        return [self.row(r) for r in self._q("SELECT * FROM homes ORDER BY id")]

    # ---------------- 로봇 ----------------
    def pair_robot(self, code: str, name: str = "robot") -> dict:
        h = self.home_by_code(code)
        if not h:
            raise ValueError("존재하지 않는 집 코드입니다")
        tok = secrets.token_urlsafe(24)
        self._insert("INSERT INTO robots(home_id,token,name,created) VALUES(?,?,?,?)", h["id"], tok, name, time.time())
        return {"token": tok, "home": h}

    def robot_by_token(self, token: str | None) -> Optional[dict]:
        if not token:
            return None
        r = self._one("SELECT * FROM robots WHERE token=?", token)
        if r:
            self._q("UPDATE robots SET last_seen=? WHERE id=?", time.time(), r["id"])
        return self.row(r)

    # ---------------- 기록 (리포트) ----------------
    def record_event(self, hid: int, type: str, data: dict | None = None, ts: float | None = None) -> None:
        self._insert("INSERT INTO events(home_id,ts,type,data) VALUES(?,?,?,?)", hid, ts or time.time(), type,
                     json.dumps(data or {}, ensure_ascii=False))

    def events(self, hid: int, since: float = 0, types: tuple[str, ...] | None = None, limit: int = 500) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE home_id=? AND ts>=?", [hid, since]
        if types:
            sql += f" AND type IN ({','.join('?' * len(types))})"
            args += list(types)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in self._q(sql, *args):
            d = self.row(r)
            d["data"] = json.loads(d["data"] or "{}")
            out.append(d)
        return out

    def alert_create(self, hid: int, alert_id: str, ts: float, cause: str, title: str, sensors: dict, fused: dict) -> None:
        if self._one("SELECT 1 FROM alerts WHERE home_id=? AND alert_id=?", hid, alert_id):
            return
        self._insert("INSERT INTO alerts(home_id,alert_id,created_ts,cause,title,sensors,fused) VALUES(?,?,?,?,?,?,?)",
                     hid, alert_id, ts, cause, title, json.dumps(sensors, ensure_ascii=False), json.dumps(fused))

    def alert_resolve(self, hid: int, alert_id: str, ts: float, resolution: str, handled_by: str) -> None:
        self._q("UPDATE alerts SET resolved_ts=?, resolution=?, handled_by=? WHERE home_id=? AND alert_id=? AND resolved_ts IS NULL",
                ts, resolution, handled_by, hid, alert_id)

    def alerts(self, hid: int, limit: int = 100) -> list[dict]:
        out = []
        for r in self._q("SELECT * FROM alerts WHERE home_id=? ORDER BY created_ts DESC LIMIT ?", hid, limit):
            d = self.row(r)
            d["sensors"] = json.loads(d["sensors"] or "{}")
            d["fused"] = json.loads(d["fused"] or "{}")
            out.append(d)
        return out

    def report(self, hid: int, days: int = 7) -> dict[str, Any]:
        """리포트 탭: 요일별 감정 횟수, 최근 대화, 알림 요약."""
        since = time.time() - days * 86400
        emotions = self.events(hid, since, ("emotion",), limit=5000)
        week: dict[str, dict[str, int]] = {}
        names = ["월", "화", "수", "목", "금", "토", "일"]
        for e in emotions:
            day = names[time.localtime(e["ts"]).tm_wday]
            week.setdefault(day, {"happy": 0, "sad": 0, "angry": 0, "neutral": 0, "surprised": 0})
            lab = e["data"].get("label", "neutral")
            week[day][lab] = week[day].get(lab, 0) + 1
        week_rows = [{"day": d, **week[d]} for d in names if d in week]
        convo = self.events(hid, since, ("dialogue",), limit=30)
        alerts = self.alerts(hid, 50)
        return {"days": days, "emotion_week": week_rows, "conversations": list(reversed(convo)),
                "alerts": alerts, "alert_count": len([a for a in alerts if a["created_ts"] >= since])}
