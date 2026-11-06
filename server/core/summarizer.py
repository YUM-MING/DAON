"""리포트 '대화 요약' 의 AI 요약 — 보호자가 버튼을 눌렀을 때만 LLM 을 부른다.

흐름 (날짜별로 따로 요약)
    1. 그날(로컬 자정~자정)의 dialogue 이벤트를 시간순으로 가져온다 (최대 max_messages, 넘으면 오래된 것부터 버림)
    2. min_messages 보다 적으면 요약하지 않는다 (LLM 호출 없음)
    3. 저장된 요약이 있고 그 뒤로 새 대화가 없으면 저장본을 그대로 쓴다 (LLM 호출 없음)
    4. 새 대화가 있어도 cooldown_sec 안이면 저장본을 쓴다 / 집당 하루 daily_limit 를 넘으면 호출하지 않는다
    5. 로봇 발화는 앞 robot_text_chars 자만, 전체는 max_chars 이하로 줄여 LLM 에 보내고 결과를 DB 에 저장한다

LLM 제공자·모델·키는 대화 모듈과 같은 modules/dialogue/.env 를 쓴다 (server/adapters/dialogue.py 와 동일).
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timedelta
from typing import Callable

from server.config import MODULES

log = logging.getLogger("daon.summary")
DIALOGUE_SRC = MODULES / "dialogue" / "src"

SCOPES = {"today": 1, "3": 3, "7": 7}      # 요청 값 → 일 수

SYSTEM_PROMPT = (
    "당신은 독거 어르신을 돌보는 보호자를 위해 하루 대화를 요약하는 도우미입니다. "
    "대화는 '어르신'과 '로봇'이 나눈 것입니다. 대화에 없는 내용은 지어내지 말고, 의학적 진단이나 판단은 하지 마세요. "
    "한국어로, 아래 형식 그대로 짧게 쓰세요.\n"
    "■ 주요 화제: (한 줄)\n"
    "■ 기분·건강 관련 언급: (통증, 식사, 수면, 약, 기분 등 어르신이 직접 말한 것. 없으면 '특별한 언급 없음')\n"
    "■ 보호자가 확인하면 좋을 점: (없으면 '특별히 없음')"
)


def default_client():
    """modules/dialogue/.env 를 읽어 LLM 클라이언트를 만든다. 꺼져 있거나 설정이 모자라면 None."""
    if str(DIALOGUE_SRC) not in sys.path:
        sys.path.insert(0, str(DIALOGUE_SRC))
    from llm_client import client_from_env  # noqa: E402
    from notifier import load_env  # noqa: E402
    load_env(str(MODULES / "dialogue" / ".env"))
    return client_from_env()


def _day_range(now: float, offset: int) -> tuple[str, float, float]:
    """오늘에서 offset 일 전 날짜의 (YYYY-MM-DD, 시작 ts, 끝 ts) — 서버 로컬 시간 기준."""
    today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    d = today - timedelta(days=offset)
    return d.strftime("%Y-%m-%d"), d.timestamp(), (d + timedelta(days=1)).timestamp()


def day_bounds(day: str | None = None, now: float | None = None) -> tuple[str, float, float]:
    """'YYYY-MM-DD' (없으면 오늘) → (날짜, 시작 ts, 끝 ts). 서버 로컬 시간 기준. 형식이 틀리면 ValueError."""
    if day is None:
        return _day_range(time.time() if now is None else now, 0)
    d = datetime.strptime(day, "%Y-%m-%d")
    return d.strftime("%Y-%m-%d"), d.timestamp(), (d + timedelta(days=1)).timestamp()


class Summarizer:
    def __init__(self, db, cfg: dict, client_factory: Callable[[], object] = default_client,
                 clock: Callable[[], float] = time.time) -> None:
        self.db, self.cfg, self.client_factory, self.clock = db, cfg, client_factory, clock

    def available(self) -> bool:
        """LLM 이 설정돼 있는지 (버튼 활성화 여부)."""
        try:
            return self.client_factory() is not None
        except Exception:
            return False

    def build_text(self, events: list[dict]) -> str:
        """대화 → LLM 입력 텍스트. 로봇 발화는 줄이고, max_chars 를 넘으면 오래된 줄부터 버린다."""
        robot_n = int(self.cfg["robot_text_chars"])
        lines = []
        for e in events:
            d = e["data"]
            text = str(d.get("text", "")).strip()
            if not text:
                continue
            if d.get("who") == "robot":
                text = text[:robot_n] + ("…" if len(text) > robot_n else "")
                lines.append(f"로봇: {text}")
            else:
                lines.append(f"어르신: {text}")
        total, keep = 0, []
        for ln in reversed(lines):
            if total + len(ln) + 1 > int(self.cfg["max_chars"]) and keep:
                break
            keep.append(ln)
            total += len(ln) + 1
        return "\n".join(reversed(keep))

    def summarize(self, hid: int, scope: str) -> dict:
        """scope: today | 3 | 7. 날짜별 결과 목록을 돌려준다 (최신 날짜 먼저). 블로킹 호출이 있으므로 스레드에서 부를 것."""
        if scope not in SCOPES:
            raise ValueError("scope 는 today, 3, 7 중 하나")
        now = self.clock()
        days = [self._one_day(hid, *_day_range(now, off), now) for off in range(SCOPES[scope])]
        return {"scope": scope, "days": days}

    def _one_day(self, hid: int, day: str, start: float, end: float, now: float) -> dict:
        c = self.cfg
        events = self.db.dialogues_between(hid, start, min(end, now + 1), int(c["max_messages"]))
        n = len(events)
        if n < int(c["min_messages"]):
            return {"day": day, "status": "too_few", "count": n, "text": None}
        last_id = max(e["id"] for e in events)
        cached = self.db.summary_get(hid, day)
        if cached and cached["last_event_id"] >= last_id:
            return {"day": day, "status": "cached", "count": n, "text": cached["text"], "created_ts": cached["created_ts"]}
        if cached and now - cached["created_ts"] < float(c["cooldown_sec"]):
            return {"day": day, "status": "cooldown", "count": n, "text": cached["text"], "created_ts": cached["created_ts"],
                    "retry_after": int(float(c["cooldown_sec"]) - (now - cached["created_ts"]))}
        stale = {"text": cached["text"], "created_ts": cached["created_ts"]} if cached else {"text": None}
        midnight = _day_range(now, 0)[1]
        if self.db.summary_calls_since(hid, midnight) >= int(c["daily_limit"]):
            return {"day": day, "status": "limit", "count": n, **stale}
        try:
            client = self.client_factory()
        except Exception as e:                       # 설정 오류도 요약 실패로만 취급 (서버는 계속)
            log.warning("[summary] LLM 클라이언트 생성 실패: %s", type(e).__name__)
            client = None
        if client is None:
            return {"day": day, "status": "unavailable", "count": n, **stale}
        self.db.summary_call_log(hid, now)           # 실패해도 호출로 센다 (연타 방지)
        text = client.generate(SYSTEM_PROMPT, [{"role": "user", "content": self.build_text(events)}],
                               timeout=float(c["timeout_sec"]), max_tokens=int(c["max_tokens"]))
        if not text:
            log.warning("[summary] 요약 실패: %s", getattr(client, "last_error", None))
            return {"day": day, "status": "failed", "count": n, **stale}
        self.db.summary_put(hid, day, last_id, n, text, now)
        return {"day": day, "status": "ok", "count": n, "text": text, "created_ts": now}
