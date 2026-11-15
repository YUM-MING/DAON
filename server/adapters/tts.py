"""TTS 어댑터: 로봇이 할 말을 cedar 목소리 mp3 로 돌려준다. (음성 AI 담당)

왜 이게 필요한가
-----------------------------------------------------------------
응답 음성은 web/tts/<목소리>/ 에 미리 만들어 두었다. 하지만 로봇이 할 말이
정해진 목록이 아니다.

  · LLM 이 켜져 있으면(DIALOGUE_LLM=openai) 일상대화는 매번 새 문장이다
  · 템플릿도 빈칸이 그때 채워진다 — "{rel}이 놀러 오신다니…", "지금은 {time}이에요"

없는 문장은 폰 내장 음성으로 떨어지고, 그러면 한 대화 안에서 목소리가
바뀐다. 그래서 미리 만든 것에 없는 문장은 여기서 그때 만들어 주고,
만든 것은 파일로 남겨 둔다.

  처음 나오는 문장   OpenAI 호출 → 0.5~1초
  한 번 나온 문장    저장해 둔 파일 → 즉시
  시연 전 리허설 한 번 돌리면 전부 저장돼서 시연 때는 전부 즉시다.

쓰는 쪽 (web/js/ws-client.js)
-----------------------------------------------------------------
    GET /api/tts?token=<로봇토큰>&voice=cedar&tone=warm&text=<할 말>
        200  audio/mpeg
        503  만들 수 없음 → 폰 내장 음성으로 되돌아간다

토큰을 받는 이유: 공개 주소로 열려 있으면 아무나 불러서 API 를 쓸 수 있다.
로봇 페이지는 이미 로봇 토큰을 갖고 있으므로 그걸 그대로 쓴다.

저장 위치
-----------------------------------------------------------------
    web/tts/<목소리>/cache/<문장 해시>.mp3      (git 에 올리지 않는다)

미리 만들어 둔 것(web/tts/<목소리>/*.mp3)과 섞이지 않게 따로 둔다.
지워도 된다. 다음에 그 문장이 나오면 다시 만든다.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response

from server import config as cfgmod

log = logging.getLogger("daon.tts")
router = APIRouter()

API = "https://api.openai.com/v1/audio/speech"
MODEL = "gpt-4o-mini-tts"
MAX_CHARS = 300                      # 로봇 응답은 이보다 길지 않다. 넘으면 거절한다
VOICE_OK = re.compile(r"^[a-z]{3,16}$")

# 말투 — tts_생성.py (미리 만드는 쪽) 와 글자까지 같아야 목소리가 이어진다
TONES = {
    "warm": ("혼자 사시는 어르신에게 말하듯, 따뜻하고 차분하게. "
             "또박또박 천천히, 다정하지만 과장되지 않게."),
    "calm": ("침착하고 또렷하게. 걱정하는 마음은 묻어나되 겁주지 않게. "
             "서두르지 말고, 낮고 안정된 목소리로."),
}

_lock = threading.Lock()             # 같은 문장이 동시에 두 번 들어올 때 중복 호출 방지
_making: set[str] = set()


def _key() -> str | None:
    k = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if k:
        return k
    env = Path(cfgmod.ROOT) / "modules" / "dialogue" / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            name, val = line.split("=", 1)
            if name.strip() == "OPENAI_API_KEY":
                return val.strip().strip('"').strip("'") or None
    return None


def _make(text: str, voice: str, tone: str) -> bytes:
    import requests
    r = requests.post(
        API,
        headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"},
        json={"model": MODEL, "voice": voice, "input": text,
              "response_format": "mp3", "instructions": TONES[tone]},
        timeout=20,
    )
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} {r.text[:160]}")
    if len(r.content) < 500:
        raise RuntimeError(f"받은 파일이 너무 작다 ({len(r.content)}바이트)")
    return r.content


def register(app, db) -> None:
    """main.py 에서 정적 파일 마운트 전에 한 번 부른다."""

    @app.get("/api/tts")
    def tts(token: str = Query(...), text: str = Query(...),
            voice: str = Query("cedar"), tone: str = Query("warm")):
        if not db.robot_by_token(token):
            raise HTTPException(401, "유효하지 않은 로봇 토큰")
        text = (text or "").strip()
        if not text:
            raise HTTPException(400, "text 가 비어 있습니다")
        if len(text) > MAX_CHARS:
            raise HTTPException(413, f"{MAX_CHARS}자까지만 됩니다")
        if not VOICE_OK.match(voice):
            raise HTTPException(400, "목소리 이름이 올바르지 않습니다")
        if tone not in TONES:
            tone = "warm"

        base = Path(cfgmod.WEB) / "tts" / voice
        if not base.exists():
            raise HTTPException(404, f"목소리 폴더가 없습니다 ({voice})")
        cache = base / "cache"
        cache.mkdir(parents=True, exist_ok=True)

        name = hashlib.sha1(f"{voice}|{tone}|{text}".encode("utf-8")).hexdigest()[:16] + ".mp3"
        path = cache / name
        hit = path.exists() and path.stat().st_size > 500
        if not hit:
            if not _key():
                # 키가 없으면 만들 수 없다. 503 으로 알려서 폰이 내장 음성으로 가게 한다.
                raise HTTPException(503, "OPENAI_API_KEY 가 없어 음성을 만들 수 없습니다")
            with _lock:
                busy = name in _making
                if not busy:
                    _making.add(name)
            if busy:                                 # 다른 요청이 지금 만들고 있다
                raise HTTPException(503, "같은 문장을 만들고 있습니다")
            try:
                data = _make(text, voice, tone)
                tmp = cache / (name + ".part")
                tmp.write_bytes(data)
                tmp.replace(path)                    # 반쯤 쓰인 파일이 남지 않게
                log.info("TTS 생성 %s %s %s %.0fKB  %s",
                         voice, tone, name, len(data) / 1024, text[:40])
            except Exception as e:                   # noqa: BLE001
                log.warning("TTS 생성 실패 (%s) — 폰 내장 음성으로 넘어갑니다: %s", text[:30], e)
                raise HTTPException(503, "음성을 만들지 못했습니다") from e
            finally:
                with _lock:
                    _making.discard(name)

        return FileResponse(path, media_type="audio/mpeg",
                            headers={"Cache-Control": "public, max-age=31536000",
                                     "X-Daon-Tts": "cache" if hit else "new"})

    @app.get("/api/tts/stats")
    def tts_stats(voice: str = Query("cedar")):
        """미리 만든 것 / 그때 만든 것이 각각 몇 개인지. 리허설 뒤 확인용."""
        base = Path(cfgmod.WEB) / "tts" / voice
        pre = list(base.glob("*.mp3")) if base.exists() else []
        made = list((base / "cache").glob("*.mp3")) if (base / "cache").exists() else []
        mb = lambda fs: round(sum(f.stat().st_size for f in fs) / 1024 / 1024, 1)   # noqa: E731
        return {"voice": voice, "key": bool(_key()),
                "미리_만든_것": {"개수": len(pre), "MB": mb(pre)},
                "그때_만든_것": {"개수": len(made), "MB": mb(made)}}

    _ = Response            # (import 유지용)
