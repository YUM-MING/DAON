"""
audio_module.py — 팀 통합용 공개 API

다른 모듈(대화 로직, 웹앱 서버)이 이 파일만 import 하면 됩니다.
표준출력을 파싱할 필요가 없고, 콜백으로 이벤트를 받습니다.

사용법 (혜원님 대화 로직 쪽)
---------------------------------------------------------------
    from audio_module import AudioSensor

    def on_event(ev: dict):
        if ev["type"] == "fall":
            print("낙상 감지!", ev["confidence"])
        elif ev["type"] == "speech":
            print("사용자 발화:", ev["text"])

    sensor = AudioSensor(on_event=on_event)   # 가중치/STT 자동 로드
    sensor.start()                            # 백그라운드 스레드에서 마이크 청취
    ...
    sensor.stop()

컨텍스트 매니저로도 쓸 수 있습니다.

    with AudioSensor(on_event=on_event):
        time.sleep(60)

이벤트 스키마
---------------------------------------------------------------
낙상
    {"type": "fall", "confidence": 0.93, "time": 12.5,
     "probs": {"speech": 0.02, "fall": 0.93, "ambient": 0.05}}
발화(STT 완료 시점에 1회)
    {"type": "speech", "text": "오늘 날씨가 춥네", "duration": 2.1, "time": 15.2}

주의
---------------------------------------------------------------
- 콜백은 **오디오 처리 스레드에서** 호출됩니다. 콜백 안에서 오래 걸리는 작업
  (TTS 재생, 네트워크 전송)을 직접 하면 다음 윈도우 처리가 밀립니다.
  큐에 넣고 다른 스레드에서 처리하세요. (queue_mode=True 를 쓰면 대신 해 줍니다)
- 모든 경로는 이 파일 기준 상대경로입니다.
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from config import CKPT_PATH, CLASSES, CLASS_KO, N_SAMPLES, SAMPLE_RATE
from inference import (FALL_K, FALL_REFRACTORY_SEC, FALL_RULE, FALL_THRESHOLD,
                       HOP_SEC, SPEECH_THRESHOLD, STT_MODEL, AudioRouter,
                       SpeechToText)
from model import load_model

ROOT = Path(__file__).resolve().parent

__all__ = ["AudioSensor", "CLASSES", "CLASS_KO", "SAMPLE_RATE"]


class AudioSensor:
    """마이크를 듣고 낙상 이벤트 / STT 결과를 콜백으로 넘기는 센서.

    Parameters
    ----------
    on_event : callable(dict) -> None
        이벤트가 생길 때마다 호출됩니다. None 이면 이벤트를 내부 큐에만 쌓고,
        `poll()` 로 꺼내 쓸 수 있습니다.
    ckpt : str | Path
        소리 분류 가중치. 기본값 ./checkpoints/sound_cls.pt
    stt : bool
        False 로 두면 STT 없이 분류만 합니다 (낙상 감지만 쓸 때 메모리 절약).
    stt_model : str
        faster-whisper 모델 크기. "tiny" / "base" / "small" / "medium"
        기본값은 "base". 같은 한국어 문장으로 base/small 을 비교했을 때
        받아쓰기 정확도는 같은데 base 가 3배 빨랐습니다 (benchmark.py 참고).
    device : str
        "cpu" / "cuda". 기본값은 CUDA 가 있으면 CUDA.
    fall_threshold, speech_threshold : float
        판정 확률 임계값. 낙상을 더 민감하게 하려면 fall_threshold 를 낮춥니다.
    fall_k : int
        최근 K개 윈도우(0.5초 간격)를 보고 판정합니다. 1 이면 윈도우 하나.
        K를 키우면 판정이 (K-1)×0.5초 늦어집니다.
        실측으로는 K=1 이 가장 좋았습니다 (낙상음이 짧아 윈도우 하나에만 담김).
        근거: outputs/threshold_val.txt
    fall_rule : str
        K>1 일 때의 판정 방식. "avg"=최근 K개 평균, "all"=최근 K개 모두.
    fall_refractory_sec : float
        한 번 알린 뒤 이 시간 동안 중복 낙상 이벤트를 보내지 않습니다.
        받는 쪽에서 디바운스를 따로 구현할 필요가 없습니다.
    input_device : int | str | None
        sounddevice 입력 장치 번호. None 이면 시스템 기본 마이크.
    queue_mode : bool
        True 면 콜백을 별도 스레드에서 호출합니다. 콜백이 느려도
        오디오 처리가 밀리지 않습니다. (권장)
    """

    def __init__(
        self,
        on_event: Optional[Callable[[dict], None]] = None,
        ckpt: str | Path = CKPT_PATH,
        stt: bool = True,
        stt_model: str = STT_MODEL,
        device: Optional[str] = None,
        fall_threshold: float = FALL_THRESHOLD,
        speech_threshold: float = SPEECH_THRESHOLD,
        fall_k: int = FALL_K,
        fall_rule: str = FALL_RULE,
        fall_refractory_sec: float = FALL_REFRACTORY_SEC,
        input_device: Optional[int | str] = None,
        queue_mode: bool = True,
    ) -> None:
        import torch

        ckpt_path = Path(ckpt)
        if not ckpt_path.is_absolute():
            ckpt_path = ROOT / ckpt_path
        if not ckpt_path.exists():
            raise FileNotFoundError(
                f"가중치를 찾을 수 없습니다: {ckpt_path}\n"
                "제출 패키지의 ./checkpoints/sound_cls.pt 를 같은 위치에 두거나 "
                "ckpt= 인자로 경로를 지정하세요."
            )

        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.model = load_model(ckpt_path, self.device)
        self._stt = SpeechToText(stt_model, enabled=stt)
        self.stt_enabled = self._stt.model is not None

        self.input_device = input_device
        self.events: queue.Queue[dict] = queue.Queue()
        self._user_cb = on_event
        self._queue_mode = queue_mode and on_event is not None

        # AudioRouter 가 결과를 표준출력이 아니라 우리 핸들러로 보내게 한다
        self._router = AudioRouter(
            self.model, self._stt, self.device,
            emit_fn=self._handle,
            fall_threshold=fall_threshold,
            speech_threshold=speech_threshold,
            fall_k=fall_k,
            fall_rule=fall_rule,
            fall_refractory_sec=fall_refractory_sec,
            # STT(약 0.7초)가 오디오 처리를 막으면 그 사이 낙상 감지가 늦어진다.
            # 별도 스레드로 분리해 낙상 경로를 항상 열어둔다.
            stt_async=True,
        )

        self._stream = None
        self._worker: Optional[threading.Thread] = None
        self._cb_worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._audio_q: queue.Queue[np.ndarray] = queue.Queue()

    # ------------------------------------------------------------------ 이벤트
    def _handle(self, ev: dict) -> None:
        self.events.put(ev)
        if self._user_cb is None:
            return
        if self._queue_mode and self._cb_worker is not None and self._cb_worker.is_alive():
            return                      # _cb_loop 가 큐에서 꺼내 호출한다
        self._safe_cb(ev)               # start() 전(process_file 등)에는 바로 호출

    def _safe_cb(self, ev: dict) -> None:
        try:
            self._user_cb(ev)           # type: ignore[misc]
        except Exception as e:          # noqa: BLE001 — 콜백 오류가 센서를 죽이면 안 된다
            print(f"[AudioSensor] 콜백 오류: {e!r}", file=sys.stderr)

    def _cb_loop(self) -> None:
        while not self._stop.is_set():
            try:
                ev = self.events.get(timeout=0.2)
            except queue.Empty:
                continue
            self._safe_cb(ev)

    def poll(self, timeout: float = 0.0) -> Optional[dict]:
        """콜백 대신 직접 꺼내 쓸 때. 이벤트가 없으면 None."""
        try:
            return self.events.get(timeout=timeout) if timeout else self.events.get_nowait()
        except queue.Empty:
            return None

    # ------------------------------------------------------------------ 수명주기
    def start(self) -> "AudioSensor":
        if self._worker is not None:
            return self
        import sounddevice as sd

        hop = int(HOP_SEC * SAMPLE_RATE)
        self._stop.clear()
        self._router.start_stt()        # stop() 후 다시 start() 해도 비동기 유지

        def callback(indata, frames, t, status):      # noqa: ANN001
            if status:
                print(f"[AudioSensor] 오디오 상태: {status}", file=sys.stderr)
            self._audio_q.put(indata[:, 0].copy())

        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            blocksize=hop, callback=callback, device=self.input_device,
        )
        self._stream.start()

        self._worker = threading.Thread(target=self._loop, name="audio-sensor", daemon=True)
        self._worker.start()
        if self._queue_mode:
            self._cb_worker = threading.Thread(target=self._cb_loop, name="audio-cb", daemon=True)
            self._cb_worker.start()
        return self

    def _loop(self) -> None:
        ring = np.zeros(N_SAMPLES, dtype="float32")
        filled = 0          # 링 버퍼가 실제 입력으로 다 채워질 때까지 판정하지 않는다
        while not self._stop.is_set():
            try:
                chunk = self._audio_q.get(timeout=0.2)
            except queue.Empty:
                continue
            n = len(chunk)
            ring = np.concatenate([ring[n:], chunk.astype("float32")])
            if filled < N_SAMPLES:
                # 시작 직후 1초는 0으로 채운 버퍼와 섞여 가짜 신호가 된다.
                # 그대로 판정하면 센서를 켜자마자 낙상 오탐이 난다.
                filled += n
                continue
            self._router.push(ring, chunk.astype("float32"))

    def stop(self) -> None:
        self._stop.set()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None
        self._router.close()            # 남은 발화 flush
        if self._cb_worker is not None:
            # flush 로 생긴 마지막 이벤트까지 넘긴 뒤 종료
            time.sleep(0.3)
            self._cb_worker.join(timeout=2.0)
            self._cb_worker = None

    # 컨텍스트 매니저
    def __enter__(self) -> "AudioSensor":
        return self.start()

    def __exit__(self, *exc) -> None:                # noqa: ANN002
        self.stop()

    # ------------------------------------------------------------------ 파일 입력
    def process_file(self, path: str | Path) -> list[dict]:
        """마이크 없이 wav/mp3 파일로 동작을 확인할 때 (통합 테스트용)."""
        from inference import run_file
        drained: list[dict] = []
        while self.poll() is not None:
            pass
        run_file(Path(path), self._router)
        while True:
            ev = self.poll()
            if ev is None:
                break
            drained.append(ev)
        return drained


# 단독 실행하면 간단한 데모가 돈다
if __name__ == "__main__":
    def show(ev: dict) -> None:
        if ev["type"] == "fall":
            print(f"  ⚠ 낙상 감지  확률 {ev['confidence']:.2f}  ({ev['time']}s)")
        else:
            print(f"  💬 \"{ev['text']}\"  ({ev['time']}s)")

    print("AudioSensor 데모 — Ctrl+C 로 종료")
    sensor = AudioSensor(on_event=show)
    print(f"  device={sensor.device}  STT={'on' if sensor.stt_enabled else 'off'}")
    try:
        with sensor:
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n종료")
