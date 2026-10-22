"""선택적 LLM 클라이언트 (표준 라이브러리만): Gemini / Claude / OpenAI REST API를 같은 인터페이스로 부른다.

    client.generate(system, messages, timeout) -> str | None
        messages: [{"role": "user" | "assistant", "content": "..."}] (오래된 것부터)
        실패(타임아웃·HTTP 오류·빈 응답·예외)는 모두 None. 실패 종류는 client.last_error 에만 남긴다 (키·응답 본문 없음)

환경변수 (기본 OFF, 모델명은 코드에 정해 두지 않는다)
    DIALOGUE_LLM          off | gemini | claude | openai   (미설정이면 off)
    DIALOGUE_LLM_MODEL    사용할 모델 이름 (필수. 없으면 off + 경고 1줄)
    DIALOGUE_LLM_TIMEOUT  초 (기본 1.2)
    GEMINI_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY   제공자별 키
API 키는 로그·예외 메시지·repr에 쓰지 않는다.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Optional

log = logging.getLogger("daon.dialogue.llm")

KEY_ENV = {"gemini": "GEMINI_API_KEY", "claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
OFF_VALUES = {"", "off", "0", "false", "no", "none"}
DEFAULT_TIMEOUT = 1.2
MAX_OUTPUT_TOKENS = 200


def _normalize(messages) -> list:
    """역할을 user/assistant로 맞추고, 같은 역할이 이어지면 합치고, 첫 메시지가 user가 되게 한다 (Gemini·Claude 규칙)."""
    out = []
    for m in messages:
        role = "assistant" if m.get("role") in ("assistant", "ai", "model") else "user"
        text = str(m.get("content", "")).strip()
        if not text:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n" + text
        else:
            out.append({"role": role, "content": text})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


class LLMClient:
    def __init__(self, provider: str, model: str, api_key: str, timeout: float = DEFAULT_TIMEOUT):
        if provider not in KEY_ENV:
            raise ValueError("unknown provider")
        self.provider, self.model, self.timeout = provider, model, float(timeout)
        self._key = api_key
        self.last_error: Optional[str] = None

    def __repr__(self) -> str:                      # 키를 보여 주지 않는다
        return f"LLMClient(provider={self.provider!r}, model={self.model!r}, timeout={self.timeout})"

    # ---------- 제공자별 요청 ----------
    def _request(self, system: str, messages: list):
        if self.provider == "claude":
            url = "https://api.anthropic.com/v1/messages"
            headers = {"x-api-key": self._key, "anthropic-version": "2023-06-01"}
            body = {"model": self.model, "max_tokens": MAX_OUTPUT_TOKENS, "system": system, "messages": messages}
        elif self.provider == "openai":
            url = "https://api.openai.com/v1/chat/completions"
            headers = {"Authorization": "Bearer " + self._key}
            body = {"model": self.model, "messages": [{"role": "system", "content": system}] + messages}
        else:                                       # gemini: 키는 URL이 아니라 헤더로 (URL이 로그에 남아도 키가 없게)
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
            headers = {"x-goog-api-key": self._key}
            body = {"systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "model" if m["role"] == "assistant" else "user",
                                  "parts": [{"text": m["content"]}]} for m in messages],
                    "generationConfig": {"maxOutputTokens": MAX_OUTPUT_TOKENS}}
        return url, {**headers, "content-type": "application/json"}, body

    def _parse(self, data: dict) -> Optional[str]:
        if self.provider == "claude":
            parts = [c.get("text", "") for c in data.get("content", []) if c.get("type") == "text"]
        elif self.provider == "openai":
            parts = [(data.get("choices") or [{}])[0].get("message", {}).get("content") or ""]
        else:
            cand = (data.get("candidates") or [{}])[0]
            parts = [p.get("text", "") for p in cand.get("content", {}).get("parts", [])]
        text = "".join(parts).strip()
        return text or None

    def generate(self, system: str, messages, timeout: Optional[float] = None) -> Optional[str]:
        self.last_error = None
        try:
            msgs = _normalize(messages)
            if not msgs:
                self.last_error = "no_messages"
                return None
            url, headers, body = self._request(system, msgs)
            req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = self._parse(data)
            if text is None:
                self.last_error = "empty"
            return text
        except urllib.error.HTTPError as e:         # 응답 본문은 읽지 않는다 (키가 섞여 있을 가능성 차단)
            self.last_error = f"http_{e.code}"
        except (socket.timeout, TimeoutError):
            self.last_error = "timeout"
        except urllib.error.URLError as e:
            self.last_error = "timeout" if isinstance(e.reason, (socket.timeout, TimeoutError)) else "network"
        except Exception as e:                      # 파싱 오류 등: 종류 이름만
            self.last_error = "error_" + type(e).__name__
        return None


def client_from_env(env=None) -> Optional[LLMClient]:
    """환경변수로 클라이언트를 만든다. 꺼져 있거나 설정이 모자라면 None (지금과 똑같이 템플릿만)."""
    env = os.environ if env is None else env
    provider = str(env.get("DIALOGUE_LLM", "")).strip().lower()
    if provider in OFF_VALUES:
        return None
    if provider not in KEY_ENV:
        log.warning("[dialogue] DIALOGUE_LLM=%s 는 지원하지 않는 값 → LLM off (gemini|claude|openai)", provider)
        return None
    model = str(env.get("DIALOGUE_LLM_MODEL", "")).strip()
    if not model:
        log.warning("[dialogue] DIALOGUE_LLM_MODEL 미설정 → LLM off")
        return None
    key = str(env.get(KEY_ENV[provider], "")).strip()
    if not key:
        log.warning("[dialogue] %s 미설정 → LLM off", KEY_ENV[provider])
        return None
    try:
        timeout = float(env.get("DIALOGUE_LLM_TIMEOUT", DEFAULT_TIMEOUT))
        if timeout <= 0:
            raise ValueError
    except (TypeError, ValueError):
        log.warning("[dialogue] DIALOGUE_LLM_TIMEOUT 값이 올바르지 않음 → %.1f초", DEFAULT_TIMEOUT)
        timeout = DEFAULT_TIMEOUT
    return LLMClient(provider, model, key, timeout)


def timed_generate(client, system: str, messages, timeout: float) -> tuple:
    """(text | None, ms, error) — 작업 스레드에서 부른다"""
    t0 = time.monotonic()
    try:
        text = client.generate(system, messages, timeout)
        err = getattr(client, "last_error", None) if text is None else None
    except Exception as e:                          # 가짜 클라이언트 등이 예외를 던져도 None으로
        text, err = None, "error_" + type(e).__name__
    return text, int((time.monotonic() - t0) * 1000), (err or ("empty" if text is None else None))
