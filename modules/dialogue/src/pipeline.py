"""연결 구조: 입력 → 대화 엔진 → (비상 알림 / 안심 음성) 과 실시간 루프.

입력(dict) ─► DialogueEngine ─► DialogueOutput ─┬─ emergency / notice / 해제 ─► AlertDispatcher ─► 보호자
                                                └─ response_text ─────► TTS ─► 어르신
일부 모듈이 실패해도(TTS, 알림 채널) 전체가 멈추지 않고 errors에 기록한다.
background=True면 보호자 알림 발송과 TTS 재생을 각각 별도 작업 스레드에서 실행해 메인 루프(판단·tick)를 막지 않는다.
외부 알림 채널(웹훅·텔레그램)의 전달 상태는 sent | failed | unconfigured로 구분하고(on_delivery, last_delivery),
실패한 발송은 retry_interval(기본 30초, 입력 시각 기준)마다 다시 보낸다.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Optional

from dialogue_algorithm import DialogueEngine, DialogueInput, DialogueOutput
from tts_adapter import speak_with_done


class BackgroundWorker:
    """작업 큐 + 스레드 1개. 넣은 순서대로 실행하고, 예외는 on_error로 보고한 뒤 계속 동작한다."""

    def __init__(self, name: str, on_error: Callable[[str], None]):
        self.on_error = on_error
        self._q: queue.Queue = queue.Queue()
        self._pending = 0
        self._cv = threading.Condition()
        threading.Thread(target=self._run, name=name, daemon=True).start()

    def submit(self, fn, *args) -> None:
        with self._cv:
            self._pending += 1
        self._q.put((fn, args))

    def _run(self) -> None:
        while True:
            fn, args = self._q.get()
            try:
                fn(*args)
            except Exception as e:
                self.on_error(f"백그라운드 작업 오류: {type(e).__name__}: {e}")
            finally:
                with self._cv:
                    self._pending -= 1
                    self._cv.notify_all()

    def wait_idle(self, timeout: float) -> bool:
        """큐가 빌 때까지 최대 timeout초 기다린다. 다 끝났으면 True."""
        with self._cv:
            return self._cv.wait_for(lambda: self._pending == 0, timeout)


class SilverCarePipeline:
    def __init__(self, engine: DialogueEngine, tts, dispatcher,
                 on_error: Optional[Callable[[str], None]] = None,
                 background: bool = False,
                 on_tts_done: Optional[Callable[[bool], None]] = None,
                 on_delivery: Optional[Callable[[str, DialogueOutput], None]] = None,
                 retry_interval: Optional[float] = 30.0):
        self.engine, self.tts, self.dispatcher = engine, tts, dispatcher
        self.on_error = on_error
        self.on_tts_done = on_tts_done            # TTS 재생이 끝나면(실패 포함) 성공 여부로 호출
        self.on_delivery = on_delivery            # 알림 발송 후 외부 채널 상태(sent/failed/unconfigured)로 호출
        self.retry_interval = retry_interval      # 실패한 알림 재전송 주기(초). None이면 끔
        self.last_delivery: Optional[str] = None
        self.errors: list = []
        self.input_warnings: list = []
        self._last_retry: Optional[float] = None
        self._alerts = BackgroundWorker("alerts", self._error) if background else None
        self._voice = BackgroundWorker("tts", self._error) if background else None

    def handle(self, raw) -> DialogueOutput:
        inp = raw if isinstance(raw, DialogueInput) else DialogueInput.from_dict(raw)
        out = self.engine.process(inp)
        self.input_warnings += out.warnings
        self._maybe_retry(inp.timestamp)

        # 보호자 알림을 먼저: 음성 출력(재생 시간)이 알림을 늦추지 않게
        if out.emergency or out.notice or out.alert_status == "resolved":
            self._run(self._alerts, self._send_alerts, out, inp.timestamp)
        if out.response_text:
            if self._voice:
                self._voice.submit(self._speak, out.response_text)
            else:
                self._speak(out.response_text)
        return out

    def tick(self, now: float) -> DialogueOutput:
        """새 입력이 없어도 시간을 흘려보낸다 (응답 대기, 재알림 등의 타이머)."""
        return self.handle(DialogueInput(timestamp=now))

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """백그라운드 작업(TTS 등)이 끝날 때까지 최대 timeout초 기다린다. 종료 직전·테스트용."""
        return all(w.wait_idle(timeout) for w in self._workers())

    def _workers(self) -> list:
        return [w for w in (self._alerts, self._voice) if w is not None]

    @staticmethod
    def _run(worker, fn, *args) -> None:
        if worker:
            worker.submit(fn, *args)
        else:
            fn(*args)

    def _send_alerts(self, out: DialogueOutput, now: float) -> None:
        try:
            results = self.dispatcher.dispatch(out, now)
        except Exception as e:
            self._error(f"알림 모듈 오류: {type(e).__name__}: {e}")
            return
        if not results:                       # 중복 방지 등으로 이번에는 보내지 않음
            return
        label = "비상 알림" if out.emergency else "보호자 알림"
        if not any(r.ok for r in results):
            self._error(f"{label} 전송 실패 (모든 채널): {[r.error for r in results]}")
        status = self._external_status(results)
        if status == "failed" and any(r.ok for r in results):
            failed = [f"{r.channel}: {r.error}" for r in results if not r.ok]
            self._error(f"{label} 전송 실패 (외부 알림 채널 전부, 콘솔·파일 기록만 성공): {failed}")
        self.last_delivery = status
        if self.on_delivery:
            self.on_delivery(status, out)

    def _external_status(self, results) -> str:
        fn = getattr(self.dispatcher, "external_status", None)
        return fn(results) if fn else ("sent" if any(r.ok for r in results) else "failed")

    def _maybe_retry(self, now: float) -> None:
        """실패해서 보관된 알림을 retry_interval마다 다시 보낸다 (입력 시각 기준)."""
        if self.retry_interval is None or not hasattr(self.dispatcher, "retry_failed"):
            return
        if self._last_retry is None or now < self._last_retry:
            self._last_retry = now
            return
        if now - self._last_retry >= self.retry_interval:
            self._last_retry = now
            if getattr(self.dispatcher, "failed", None):
                self._run(self._alerts, self._retry_failed)

    def _retry_failed(self) -> None:
        results = self.dispatcher.retry_failed()
        still = [f"{r.channel}: {r.error}" for r in results if not r.ok and not getattr(r, "gave_up", False)]
        gave = [f"{r.channel}: {r.error}" for r in results if getattr(r, "gave_up", False)]
        if still:
            self._error(f"알림 재전송 실패 {len(still)}건 (다음 주기에 다시 시도): {still}")
        if gave:
            self._error(f"알림 재전송 포기(gave_up) {len(gave)}건: 재전송 상한까지 실패: {gave}")

    def _speak(self, text: str) -> None:
        try:
            speak_with_done(self.tts, text, self.on_tts_done)
        except Exception as e:
            self._error(f"TTS 오류: {type(e).__name__}: {e}")

    def _error(self, msg: str):
        self.errors.append(msg)
        if self.on_error:
            self.on_error(msg)


class RealtimeRunner:
    """입력 큐 + 주기적 tick. 다른 모듈/스레드는 feed()로 데이터를 넣고, run()이 처리한다."""

    def __init__(self, pipeline: SilverCarePipeline, clock: Callable[[], float] = time.time,
                 tick_interval: float = 1.0):
        self.pipeline, self.clock, self.tick_interval = pipeline, clock, tick_interval
        self._q: queue.Queue = queue.Queue()
        self.last_input: Optional[dict] = None   # 마지막 poll_once가 처리한 입력 (tick이었으면 None, 로그 표시용)

    def feed(self, raw: dict) -> None:
        self._q.put(raw)

    def pending(self) -> int:
        """아직 처리하지 않은 입력 수"""
        return self._q.qsize()

    def poll_once(self, timeout: Optional[float] = None) -> DialogueOutput:
        wait = self.tick_interval if timeout is None else timeout
        try:
            raw = self._q.get(timeout=wait)
        except queue.Empty:
            self.last_input = None
            return self.pipeline.tick(self.clock())
        raw = dict(raw)
        raw.setdefault("timestamp", self.clock())
        self.last_input = raw
        return self.pipeline.handle(raw)

    def run(self, stop_event) -> None:
        while not stop_event.is_set():
            self.poll_once()