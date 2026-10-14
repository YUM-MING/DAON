"""팀 통합용 이벤트 인터페이스.

비전 모듈은 아래 이벤트만 발행하고, 소비 측(대화 알고리즘/UI/하드웨어)은
subscribe로 콜백을 등록한다. 추후 FastAPI·WebSocket·시리얼 등 실제 전송이
정해지면 어댑터(콜백 하나)만 추가하면 된다.

이벤트 종류
- emotion        : {label, confidence}            표정 인식 결과 (안정화된 변화 시)
- fall_detected  : {confidence}                   낙상 확정 (1차 음성 확인 트리거)
- fall_recovered : {}                             낙상 후 스스로 일어남
- fall_alert     : {elapsed_sec}                  무응답 지속 → 보호자 긴급 알림
- person_status  : {present, posture}             주기적 상태 (standing/sitting/lying/unknown)
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class Event:
    type: str
    data: dict[str, Any]
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(
            {"type": self.type, "timestamp": self.timestamp, **self.data},
            ensure_ascii=False,
        )


Subscriber = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    def subscribe(self, fn: Subscriber) -> None:
        self._subscribers.append(fn)

    def publish(self, type: str, **data: Any) -> Event:
        event = Event(type=type, data=data)
        for fn in self._subscribers:
            try:
                fn(event)
            except Exception as e:  # 구독자 오류가 파이프라인을 멈추면 안 됨
                print(f"[events] subscriber error on {type}: {e}")
        return event


def console_sink(event: Event) -> None:
    """디버그용: 이벤트를 콘솔에 출력."""
    print(f"[EVENT] {event.to_json()}")


class JsonlSink:
    """이벤트를 .jsonl 파일로 기록 (데모/평가용 로그)."""

    def __init__(self, path: str | Path) -> None:
        self._file = open(path, "a", encoding="utf-8")

    def __call__(self, event: Event) -> None:
        self._file.write(event.to_json() + "\n")
        self._file.flush()
