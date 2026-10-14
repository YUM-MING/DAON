"""내부 이벤트 버스.

모든 모듈 어댑터는 여기로만 발행하고, 조정자(coordinator)와 다른 어댑터는 여기서만 구독한다.
모듈끼리는 서로를 모른다.

- publish()는 어느 스레드에서 불러도 안전하다. asyncio 루프가 붙어 있으면 루프 스레드에서 구독자를 호출한다.
- 루프가 없으면(테스트) 즉시 동기 호출한다.

내부 이벤트 종류 (type → data)
    pose_pred       pred: fall|no_fall|uncertain, conf                 ← 비전 (0.5초 주기)
    person_status   present: bool, posture: standing|sitting|lying|unknown ← 비전 (2초 주기)
    emotion         label, raw, confidence                              ← 비전 (변화 시)
    fall_detected / fall_recovered / fall_alert                         ← 비전 (참고용, 판정은 대화 모듈)
    audio_fall      confidence, probs                                   ← 음성
    speech          text, duration                                      ← 음성(STT)
    voice_unclear   duration                                            ← 음성
    radar           motion: moving|still|none, distance_cm              ← 레이더
    device          camera|mic|radar: ok|error|mock|off, detail         ← 워커들
    dialogue_output DialogueOutput.to_dict()                            ← 대화 어댑터
    alert           혜원 AlertMessage.to_dict()                         ← 대화 어댑터(알림 채널)
    tts_done        utterance_id, ok                                    ← 로봇 폰
    mic_mute        muted: bool                                         ← 조정자 → 음성 어댑터
    alert_cleared   alert_id, reason                                    ← 조정자 → 대화/비전 어댑터
    guardian_resolve alert_id, resolution, handled_by                   ← 보호자 웹
    guardian_message text                                               ← 보호자 웹
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("daon.bus")


@dataclass
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def __getattr__(self, name: str) -> Any:  # ev.pred 처럼 data 필드를 바로 읽기
        try:
            return self.data[name]
        except KeyError as e:
            raise AttributeError(name) from e

    def get(self, name: str, default: Any = None) -> Any:
        return self.data.get(name, default)


Subscriber = Callable[[Event], Any]


class Bus:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self._subs: dict[str, list[Subscriber]] = defaultdict(list)
        self._all: list[Subscriber] = []
        self.history: list[Event] = []       # 최근 이벤트 (디버그 API용)
        self.history_max = 500

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def subscribe(self, type: str | None, fn: Subscriber) -> None:
        (self._all if type is None else self._subs[type]).append(fn)

    def publish(self, type: str, ts: float | None = None, **data: Any) -> Event:
        ev = Event(type, data, ts if ts is not None else time.time())
        if self.loop is None or self._on_loop():
            self._dispatch(ev)
        else:
            self.loop.call_soon_threadsafe(self._dispatch, ev)
        return ev

    def _on_loop(self) -> bool:
        try:
            return asyncio.get_running_loop() is self.loop
        except RuntimeError:
            return False

    def _dispatch(self, ev: Event) -> None:
        self.history.append(ev)
        if len(self.history) > self.history_max:
            del self.history[: len(self.history) - self.history_max]
        for fn in list(self._subs.get(ev.type, ())) + list(self._all):
            try:
                r = fn(ev)
                if inspect.isawaitable(r):
                    asyncio.ensure_future(r)
            except Exception:  # 구독자 하나의 오류가 전체를 멈추면 안 된다
                log.exception("subscriber error on %s", ev.type)
