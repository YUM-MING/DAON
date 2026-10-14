"""TTS 어댑터: 대화 텍스트 → 음성 출력. (실제 TTS 엔진은 이 저장소가 아니라 TTS 모듈의 몫)

대화 로직은 speak(text, on_done)만 호출한다. TTS 모듈이 연결되면
CallbackTTS(fn)에 그 함수를 꽂기만 하면 되고, 대화 로직은 손대지 않는다.

on_done(ok: bool): 재생이 끝나면(실패 포함) 한 번 호출. 나중에 '재생 중 STT 멈춤' 등에 쓴다.
on_done을 받지 않는 이전 방식 TTS(speak(text)만 있음)도 speak_with_done()으로 그대로 쓸 수 있다.
"""

from __future__ import annotations

import inspect
from typing import Callable, Optional

DoneCallback = Optional[Callable[[bool], None]]


def _done(on_done: DoneCallback, ok: bool) -> None:
    if on_done is not None:
        on_done(ok)


def _accepts_on_done(speak) -> bool:
    try:
        params = inspect.signature(speak).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "on_done" or p.kind == p.VAR_KEYWORD for p in params)


def speak_with_done(tts, text: str, on_done: DoneCallback = None) -> None:
    """어떤 TTS든 on_done까지 보장해서 호출한다. 실패하면 on_done(False) 후 예외를 그대로 올린다."""
    if _accepts_on_done(tts.speak):
        tts.speak(text, on_done=on_done)
        return
    try:
        tts.speak(text)
    except Exception:
        _done(on_done, False)
        raise
    _done(on_done, True)


class TTS:
    def speak(self, text: str, on_done: DoneCallback = None) -> None:
        raise NotImplementedError


class ConsoleTTS(TTS):
    """소리 대신 화면에 출력 (개발/데모/테스트용)."""

    def __init__(self, printer: Callable[[str], None] = print):
        self.printer = printer

    def speak(self, text: str, on_done: DoneCallback = None) -> None:
        self.printer(f"🔊 {text}")
        _done(on_done, True)


class CallbackTTS(TTS):
    """함수 하나를 TTS로 쓴다. TTS 모듈 연결 예:

    tts = CallbackTTS(lambda text: tts_module.speak(text))     # 재생이 끝날 때 반환하는 함수
    tts = CallbackTTS(lambda text: ws.send(text))              # 다른 장치로 문장 전송
    fn이 예외를 내면 실패로 보고(on_done(False))하고 예외를 그대로 올린다.
    """

    def __init__(self, fn: Callable[[str], None]):
        self.fn = fn

    def speak(self, text: str, on_done: DoneCallback = None) -> None:
        try:
            self.fn(text)
        except Exception:
            _done(on_done, False)
            raise
        _done(on_done, True)


class Pyttsx3TTS(TTS):
    """임시 TTS (모델이 오기 전 소리 확인용). `pip install pyttsx3` 필요. Windows는 설치된 한국어 음성을 쓴다."""

    def __init__(self, rate: int = 150):
        try:
            import pyttsx3
        except ImportError as e:
            raise ImportError("pyttsx3가 필요해요: pip install pyttsx3") from e
        self.engine = pyttsx3.init()
        self.engine.setProperty("rate", rate)
        for v in self.engine.getProperty("voices"):
            blob = f"{getattr(v, 'name', '')} {getattr(v, 'id', '')} {getattr(v, 'languages', '')}".lower()
            if "ko" in blob or "korean" in blob or "heami" in blob:
                self.engine.setProperty("voice", v.id)
                break

    def speak(self, text: str) -> None:
        self.engine.say(text)
        self.engine.runAndWait()


class SafeTTS(TTS):
    """주 TTS가 실패해도(모델 오류, 장치 없음) 시스템이 멈추지 않고 대체 출력으로 넘긴다."""

    def __init__(self, primary: TTS, fallback: Optional[TTS] = None):
        self.primary, self.fallback = primary, fallback or ConsoleTTS()
        self.failures = 0

    def speak(self, text: str, on_done: DoneCallback = None) -> None:
        try:
            speak_with_done(self.primary, text)
        except Exception:
            self.failures += 1
            speak_with_done(self.fallback, text)
            _done(on_done, False)          # 화면 대체 출력은 했지만 실제 재생은 실패
            return
        _done(on_done, True)


def make_tts(kind: str = "console", printer: Callable[[str], None] = print) -> TTS:
    """kind: console | pyttsx3 (pyttsx3가 없으면 콘솔로 대체). printer: 콘솔 출력 함수 (출력 잠금 공유용)"""
    if kind == "pyttsx3":
        try:
            return SafeTTS(Pyttsx3TTS(), ConsoleTTS(printer))
        except ImportError:
            return ConsoleTTS(printer)
    return ConsoleTTS(printer)