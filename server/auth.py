"""인증: 세션 쿠키(보호자) · 집 코드 페어링 토큰(로봇).

- 보호자: 이메일+비밀번호 → 세션 토큰을 HttpOnly 쿠키 `daon_session` 에 저장
- 로봇 폰: 집 코드(6자리) → 로봇 토큰 발급 → 폰 localStorage 에 저장, WebSocket 접속 시 ?token= 으로 전달
- 데모 계정: config.yaml 의 demo 항목으로 서버 시작 시 자동 생성 (시연용)
"""
from __future__ import annotations

from fastapi import HTTPException, Request

COOKIE = "daon_session"


def current_user(request: Request) -> dict | None:
    db = request.app.state.core.db
    return db.session_user(request.cookies.get(COOKIE))


def require_user(request: Request) -> dict:
    u = current_user(request)
    if not u:
        raise HTTPException(401, "로그인이 필요합니다")
    return u


def require_member(request: Request, home_id: int) -> dict:
    """해당 집의 보호자(멤버)인지 확인하고 집 정보를 돌려준다."""
    u = require_user(request)
    db = request.app.state.core.db
    if not db.is_member(u["id"], home_id):
        raise HTTPException(403, "이 집의 보호자가 아닙니다")
    h = db.get_home(home_id)
    if not h:
        raise HTTPException(404, "집을 찾을 수 없습니다")
    return h


def ensure_demo_account(db, demo: dict) -> dict | None:
    """시연용 개발자 계정 + 집을 보장한다. 이미 있으면 그대로."""
    if not demo or not demo.get("email"):
        return None
    u = db.get_user_by_email(demo["email"])
    if u is None:
        user = db.create_user(demo["email"], demo["password"], demo.get("name", "개발자"), is_dev=True)
    else:
        user = db.get_user(u["id"])
    code = str(demo.get("home_code") or "")
    home = db.home_by_code(code) if code else None
    if home is None:
        homes = db.homes_for_user(user["id"])
        home = homes[0] if homes else db.create_home(demo.get("home_name", "데모 집"), user["id"], code or None, is_demo=True)
    return {"user": user, "home": home}
