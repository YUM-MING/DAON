"""
inference.py — 실시간 소리 감지 · 분리 → 대화면 STT

흐름
  마이크(16kHz) → 1초 윈도우(0.5초 간격) → 분류기
      ├ fall     : 낙상·충격음 이벤트 출력  (팀 파이프라인에 전달)
      └ speech   : 발화 구간을 모아두었다가, 말이 끝나면 STT 로 텍스트 변환
      └ ambient  : 무시

사용 예)
  python inference.py                      # 마이크 실시간
  python inference.py --no-stt             # 분류만 (STT 모델 없이 동작 확인)
  python inference.py --file sample.wav    # 마이크 없이 wav 파일로 테스트

출력은 한 줄 JSON 이라 다른 모듈이 그대로 받아쓸 수 있다.
  {"type": "fall",   "confidence": 0.93, "time": 12.5}
  {"type": "speech", "text": "오늘 날씨가 춥네", "time": 15.2}
"""
from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
import torch

from model import CLASSES, CLASS_KO, N_SAMPLES, SAMPLE_RATE, CKPT_PATH, load_model

ROOT = Path(__file__).resolve().parent
HOP_SEC = 0.5                     # 분류 간격
#   낙상 판단 확률. val split 에서 고른 값 (eval_threshold.py --split val).
#   τ=0.60 / K=1  (낙상 원본 12개, 비낙상 음원 38개 기준)
#     val  : 이벤트 재현율 100% (12/12), 오탐 음원 5/38
#     test : 이벤트 재현율 100% (12/12), 오탐 음원 4/38
#   올리면 오탐은 줄지만 미탐이 늘어난다 (test 기준 τ=0.90 → 재현율 66.7%).
#   낙상은 놓치는 쪽이 더 위험하고 뒤에 포즈 교차검증과 음성 확인이 있으므로
#   재현율을 우선했다. 근거: outputs/threshold_val.txt
FALL_THRESHOLD = 0.60
#   판정 규칙: 최근 FALL_K 개 윈도우를 어떻게 볼지. eval_threshold.py 로 고른 값.
#   K=1 이면 윈도우 하나만 보고 판정한다(가장 빠름).
#   낙상음은 0.2~0.5초로 짧아 윈도우 하나에만 담기는 경우가 많으므로,
#   "K개가 모두 넘어야 한다"(all)는 조건은 오히려 재현율을 크게 깎는다.
#   실측 결과 K=1 이 가장 좋았다 → outputs/threshold_val.txt 참고
FALL_K = 1
FALL_RULE = "avg"                 # "avg"(최근 K개 평균) 또는 "all"(최근 K개 모두)
FALL_REFRACTORY_SEC = 5.0         # 한 번 알린 뒤 이 시간 동안은 중복 알림을 막는다
#   재알림 억제가 있으므로 한 번의 낙상 = 한 번의 이벤트가 된다.
#   받는 쪽에서 디바운스를 따로 구현할 필요가 없다.
SPEECH_THRESHOLD = 0.50
SILENCE_SEC = 1.0                 # 이 시간만큼 말이 없으면 발화 종료로 보고 STT 실행
MAX_UTTERANCE_SEC = 15.0
VOICE_UNCLEAR_MIN_SEC = 0.8       # 사람 소리는 났는데 말로 인식되지 않은 최소 길이
#   STT 모델 크기. benchmark.py 로 base / small 을 같은 한국어 문장으로 비교한 결과,
#   받아쓰기 정확도는 차이가 없는데 base 가 3배 빨랐다 (고정 지연 0.7초 vs 2.3초).
#   체감 응답 지연 1.7초 vs 3.3초. 디스크도 140MB vs 500MB.
#   더 어려운 발화를 다뤄야 하면 "small" 로 올린다. 근거: outputs/benchmark.txt
STT_MODEL = "base"
FALL_IDX = CLASSES.index("fall")


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


class SpeechToText:
    """faster-whisper 래퍼. 모델이 없으면 비활성 상태로 동작한다."""

    def __init__(self, model_size: str = STT_MODEL, enabled: bool = True):
        self.model = None
        if not enabled:
            return
        try:
            from faster_whisper import WhisperModel
            device = "cuda" if torch.cuda.is_available() else "cpu"
            compute = "float16" if device == "cuda" else "int8"
            self.model = WhisperModel(model_size, device=device, compute_type=compute)
            print(f"[STT] faster-whisper '{model_size}' 로드 ({device}/{compute})", file=sys.stderr)
        except Exception as e:                                   # noqa: BLE001
            print(f"[STT] 비활성 ({e})", file=sys.stderr)

    def transcribe(self, wav: np.ndarray) -> str:
        if self.model is None:
            return ""
        segments, _ = self.model.transcribe(
            wav, language="ko", beam_size=1, vad_filter=True,
            # 어르신 발화는 느리고 쉼이 길어 기본값보다 여유를 둔다
            no_speech_threshold=0.5,
        )
        return " ".join(s.text.strip() for s in segments).strip()


class AudioRouter:
    """분류 결과에 따라 낙상 이벤트와 발화 구간을 나누는 상태 기계.

    emit 을 넘기면 결과를 표준출력이 아니라 그 함수로 보낸다.
    (팀 통합용 audio_module.AudioSensor 가 이 방식으로 콜백을 연결한다)
    """

    def __init__(self, model, stt: SpeechToText, device: str = "cpu", verbose: bool = False,
                 emit_fn=None, fall_threshold: float = FALL_THRESHOLD,
                 speech_threshold: float = SPEECH_THRESHOLD,
                 fall_k: int = FALL_K, fall_rule: str = FALL_RULE,
                 fall_refractory_sec: float = FALL_REFRACTORY_SEC,
                 clock=None, stt_async: bool = False):
        self.model = model
        self.stt = stt
        self.device = device
        self.verbose = verbose
        self.emit = emit_fn if emit_fn is not None else emit
        self.fall_threshold = fall_threshold
        self.speech_threshold = speech_threshold
        self.fall_k = max(1, int(fall_k))
        self.fall_rule = fall_rule
        self.fall_refractory_sec = fall_refractory_sec
        self._fall_hist: deque[float] = deque(maxlen=self.fall_k)   # 최근 K개 P(fall)
        self._last_fall_t = -1e9          # 마지막 낙상 알림 시각
        self.speech_buf: list[np.ndarray] = []
        self.silence = 0.0
        # 시계를 주입할 수 있게 둔다. 파일 입력 테스트에서는 실제 시간이 아니라
        # 오디오 위치를 시간으로 써야 재알림 억제가 올바르게 동작한다.
        self.clock = clock if clock is not None else time.time
        self.t0 = self.clock()
        self._now = self.t0

        # STT 는 0.7초쯤 걸린다. 같은 스레드에서 돌리면 그동안 오디오 처리가 멈춰
        # 낙상 감지가 늦어진다. 마이크 입력에서는 별도 스레드로 보낸다.
        # (파일 입력은 결과 순서를 보장해야 하므로 동기 처리)
        self.stt_async = stt_async
        self._stt_q: queue.Queue | None = None
        self._stt_thread: threading.Thread | None = None
        self.start_stt()

    def start_stt(self) -> None:
        """STT 워커 스레드를 띄운다. 이미 돌고 있으면 아무것도 하지 않는다.
        close() 로 내린 뒤 다시 쓰려면 이 메서드를 호출한다."""
        if not self.stt_async or self._stt_thread is not None:
            return
        self._stt_q = queue.Queue()
        self._stt_thread = threading.Thread(target=self._stt_loop,
                                            name="stt-worker", daemon=True)
        self._stt_thread.start()

    def _stt_loop(self) -> None:
        while True:
            item = self._stt_q.get()                    # type: ignore[union-attr]
            if item is None:
                return
            wav, t = item
            try:
                self._transcribe_and_emit(wav, t)
            except Exception as e:                      # noqa: BLE001
                print(f"[STT] 변환 실패: {e!r}", file=sys.stderr)

    def _classify(self, window: np.ndarray) -> tuple[str, float, np.ndarray]:
        x = torch.from_numpy(window).unsqueeze(0).to(self.device)
        with torch.no_grad():
            prob = torch.softmax(self.model(x), dim=1)[0].cpu().numpy()
        i = int(prob.argmax())
        return CLASSES[i], float(prob[i]), prob

    def _transcribe_and_emit(self, wav: np.ndarray, t: float) -> None:
        dur = round(len(wav) / SAMPLE_RATE, 2)
        text = self.stt.transcribe(wav)
        if text:
            self.emit({"type": "speech", "text": text, "duration": dur,
                       "time": round(t - self.t0, 2)})
        elif self.stt.model is not None and dur >= VOICE_UNCLEAR_MIN_SEC:
            # 사람 목소리로 분류됐는데 텍스트가 안 나온 구간.
            # 신음·비명·울음처럼 '말이 아닌 소리'일 가능성이 있다.
            # 낙상 직후에 이 이벤트가 오면 긴급도가 높다고 보는 근거가 된다.
            # (음향으로 비명 클래스를 따로 학습하지 않고도 쓸 수 있는 신호)
            self.emit({"type": "voice_unclear", "duration": dur,
                       "time": round(t - self.t0, 2)})

    def _flush_speech(self) -> None:
        if not self.speech_buf:
            return
        wav = np.concatenate(self.speech_buf)
        self.speech_buf.clear()
        if len(wav) < SAMPLE_RATE * 0.4:        # 너무 짧으면 버림
            return
        if self._stt_q is not None:
            self._stt_q.put((wav, self._now))   # 별도 스레드로 넘기고 바로 복귀
        else:
            self._transcribe_and_emit(wav, self._now)

    def push(self, window: np.ndarray, hop: np.ndarray, t: float | None = None) -> None:
        """window: 분류용 1초, hop: 새로 들어온 구간(발화 버퍼에 쌓을 부분)
        t: 이 윈도우의 시각. None 이면 self.clock() 을 쓴다."""
        self._now = self.clock() if t is None else t
        label, conf, prob = self._classify(window)
        if self.verbose:                     # 디버깅용: 매 윈도우 판정 결과를 보여준다
            print(f"  [{self._now - self.t0:6.1f}s] {CLASS_KO[label]:<8} {conf:.2f}"
                  f"  P(fall)={float(prob[FALL_IDX]):.2f}", file=sys.stderr)

        # ---- 낙상 판정: 최근 K개 규칙 + 재알림 억제 ----------------------------
        # argmax 가 아니라 P(fall) 을 직접 본다 (eval_threshold.py 와 같은 기준).
        p_fall = float(prob[FALL_IDX])
        self._fall_hist.append(p_fall)
        if len(self._fall_hist) < self.fall_k:
            score = 0.0                                   # 아직 K개가 안 모였다
        elif self.fall_k == 1:
            score = p_fall
        elif self.fall_rule == "all":
            score = min(self._fall_hist)
        else:                                             # "avg"
            score = sum(self._fall_hist) / len(self._fall_hist)

        now = self._now
        if (score >= self.fall_threshold
                and now - self._last_fall_t >= self.fall_refractory_sec):
            self._last_fall_t = now
            self.emit({"type": "fall", "confidence": round(p_fall, 3),
                       "score": round(score, 3),
                       "time": round(now - self.t0, 2),
                       "probs": {c: round(float(p), 3) for c, p in zip(CLASSES, prob)}})
            # 낙상 직후에는 "괜찮으세요?" 응답을 받아야 하므로 음성 버퍼를 비우지 않는다

        if label == "speech" and conf >= self.speech_threshold:
            self.speech_buf.append(hop)
            self.silence = 0.0
            if sum(len(b) for b in self.speech_buf) > MAX_UTTERANCE_SEC * SAMPLE_RATE:
                self._flush_speech()
        else:
            if self.speech_buf:
                self.silence += HOP_SEC
                self.speech_buf.append(hop)          # 끝말이 잘리지 않도록 조금 더 담는다
                if self.silence >= SILENCE_SEC:
                    self._flush_speech()
                    self.silence = 0.0

    def close(self, timeout: float = 10.0) -> None:
        """남은 발화를 변환하고 STT 스레드를 정리한다."""
        self._flush_speech()
        if self._stt_q is not None:
            self._stt_q.put(None)                       # 종료 신호
            if self._stt_thread is not None:
                self._stt_thread.join(timeout=timeout)
            self._stt_q = None
            self._stt_thread = None


def run_file(path: Path, router: AudioRouter) -> None:
    import soundfile as sf
    from scipy.signal import resample_poly
    wav, sr = sf.read(str(path), dtype="float32", always_2d=True)
    wav = wav.mean(axis=1)
    if sr != SAMPLE_RATE:
        g = np.gcd(int(sr), SAMPLE_RATE)
        wav = resample_poly(wav, SAMPLE_RATE // g, sr // g).astype("float32")
    hop = int(HOP_SEC * SAMPLE_RATE)
    # 파일 입력에서는 '오디오 안에서의 위치'를 시간으로 쓴다
    router.t0 = 0.0
    for start in range(0, max(len(wav) - N_SAMPLES, 0) + 1, hop):
        window = wav[start:start + N_SAMPLES]
        t = (start + N_SAMPLES) / SAMPLE_RATE
        router.push(window, wav[start + N_SAMPLES - hop: start + N_SAMPLES], t=t)
    router.close()


def run_mic(router: AudioRouter) -> None:
    import sounddevice as sd
    hop = int(HOP_SEC * SAMPLE_RATE)
    q: queue.Queue[np.ndarray] = queue.Queue()
    ring = deque(maxlen=N_SAMPLES)
    ring.extend(np.zeros(N_SAMPLES, dtype="float32"))

    def callback(indata, frames, t, status):               # noqa: ANN001
        if status:
            print(f"[audio] {status}", file=sys.stderr)
        q.put(indata[:, 0].copy())

    print("[mic] 듣는 중… Ctrl+C 로 종료", file=sys.stderr)
    filled = 0          # 링 버퍼가 실제 입력으로 다 채워질 때까지 판정하지 않는다
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=hop, callback=callback):
        try:
            while True:
                chunk = q.get()
                ring.extend(chunk)
                if filled < N_SAMPLES:
                    # 시작 직후 1초는 0으로 채운 버퍼와 섞여 가짜 신호가 된다.
                    # 그대로 판정하면 마이크를 켜자마자 낙상 오탐이 난다.
                    filled += len(chunk)
                    continue
                router.push(np.array(ring, dtype="float32"), chunk.astype("float32"))
        except KeyboardInterrupt:
            print("\n[mic] 정리 중…", file=sys.stderr)
            router.close()
            print("[mic] 종료", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", default=str(CKPT_PATH.relative_to(ROOT)))
    ap.add_argument("--file", help="마이크 대신 사용할 wav 경로")
    ap.add_argument("--no-stt", action="store_true", help="STT 끄고 분류만")
    ap.add_argument("--stt-model", default=STT_MODEL,
                    help="tiny / base / small / medium")
    ap.add_argument("--fall-threshold", type=float, default=FALL_THRESHOLD)
    ap.add_argument("--fall-k", type=int, default=FALL_K,
                    help="최근 K개 윈도우를 보고 판정 (1이면 윈도우 하나)")
    ap.add_argument("--fall-rule", default=FALL_RULE, choices=["avg", "all"],
                    help="avg=최근 K개 평균, all=최근 K개 모두 임계값 이상")
    ap.add_argument("--verbose", action="store_true", help="매 윈도우 판정 결과 출력")
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    args = ap.parse_args()

    ckpt = (ROOT / args.ckpt).resolve()
    if not ckpt.exists():
        raise SystemExit(f"가중치가 없습니다: {ckpt}\n먼저 python train.py 를 실행하세요.")

    model = load_model(ckpt, args.device)
    print(f"[model] {ckpt.name} 로드, 클래스={[CLASS_KO[c] for c in CLASSES]}", file=sys.stderr)
    router = AudioRouter(model, SpeechToText(args.stt_model, not args.no_stt),
                         args.device, verbose=args.verbose,
                         fall_threshold=args.fall_threshold,
                         fall_k=args.fall_k, fall_rule=args.fall_rule,
                         stt_async=not args.file)   # 마이크일 때만 STT 를 분리
    rule = "윈도우 1개" if args.fall_k == 1 else f"최근 {args.fall_k}개 {args.fall_rule}"
    print(f"[설정] 낙상 임계값 {args.fall_threshold} / {rule} / "
          f"재알림 억제 {FALL_REFRACTORY_SEC}초", file=sys.stderr)

    if args.file:
        run_file(Path(args.file), router)
    else:
        run_mic(router)


if __name__ == "__main__":
    main()
