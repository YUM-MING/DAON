"""음성 워커: modules/audio_ai (한정연) 분류기 + STT 를 자기 루트(cwd)에서 실행하고 이벤트를 stdout JSON lines로 내보낸다.

    python server/workers/audio_worker.py --source phone --stt-model base     # 폰 브라우저 PCM (stdin)
    python server/workers/audio_worker.py --source mic                        # PC 마이크 (AudioSensor)

폰 모드: 집(home)마다 AudioRouter(분류 → 발화 구간 → STT) 하나. 모델과 whisper 는 공유.
stdin 명령
    {"cmd":"pcm","home":1,"ts":..,"b64":"<int16 LE 16kHz mono>"}
    {"cmd":"mute","home":1} / {"cmd":"unmute","home":1}   TTS 재생 중 speech/voice_unclear 만 버림 (낙상 감지는 계속)
    {"cmd":"stop"}
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AUDIO = ROOT / "modules" / "audio_ai"
sys.path.insert(0, str(AUDIO))
os.chdir(AUDIO)

TYPE_MAP = {"fall": "audio_fall", "speech": "speech", "voice_unclear": "voice_unclear"}
SILENCE_RMS = 0.0008      # -62 dBFS. 이 아래는 사실상 디지털 무음 (폰 AEC 억제 구간) → 분류하지 않는다


def emit(type: str, ts: float | None = None, **data) -> None:
    print(json.dumps({"type": type, "ts": ts if ts is not None else time.time(), **data}, ensure_ascii=False),
          flush=True)


def forward(home, ev: dict, muted: bool) -> None:
    t = TYPE_MAP.get(ev.get("type"))
    if not t:
        return
    if muted and t != "audio_fall":
        return                              # 로봇 자기 목소리(TTS) 에코 방지
    data = {k: v for k, v in ev.items() if k not in ("type", "time")}
    data["sensor_time"] = ev.get("time")
    emit(t, home=home, **data)              # ts = 수신 시각(epoch); 센서의 경과초는 sensor_time 으로 보존


class HomeAudio:
    """집 하나: 1초 링 버퍼 + 0.5초 hop 으로 AudioRouter.push (AudioSensor._loop 과 같은 규칙)."""

    def __init__(self, home, model, stt, device: str, debug: bool = False, record_dir: str | None = None) -> None:
        import numpy as np
        from config import N_SAMPLES, SAMPLE_RATE
        from inference import HOP_SEC, AudioRouter

        self.home, self.np, self.sr = home, np, SAMPLE_RATE
        self.hop = int(HOP_SEC * SAMPLE_RATE)
        self.n = N_SAMPLES
        self.ring = np.zeros(N_SAMPLES, dtype="float32")
        self.buf = np.zeros(0, dtype="float32")
        self.filled = 0
        self.muted = False
        self.debug, self.record_dir = debug, record_dir
        self.router = AudioRouter(model, stt, device, emit_fn=lambda ev: forward(home, ev, self.muted), stt_async=True)
        # 무음 게이트 + 디버그. 정연 모듈 내부 _classify 를 감싸기만 한다.
        # 실측(2026-10-18 폰 녹음): 값이 전부 0인 창(폰 에코 제거가 마이크를 누른 구간)을 분류기가 "낙상·충격음 0.89"로
        # 판정해 낙상 오탐이 연쇄로 났다. 거의 무음(rms < SILENCE_RMS) 이거나 절반 이상이 정확히 0이면 '일상 소음'으로 본다.
        orig = self.router._classify
        self.gated = 0

        def wrapped(window):
            rms = float(np.sqrt(np.mean(window ** 2))) if len(window) else 0.0
            zero_frac = float(np.mean(window == 0.0)) if len(window) else 1.0
            if rms < SILENCE_RMS or zero_frac > 0.9:
                label, conf, prob = "ambient", 1.0, np.array([0.0, 0.0, 1.0], dtype="float32")
                self.gated += 1
            else:
                label, conf, prob = orig(window)
            if debug:
                emit("audio_debug", home=home, label=label, conf=round(float(conf), 2),
                     p_fall=round(float(prob[1]), 2), muted=self.muted, rms=round(rms, 4),
                     gated=bool(rms < SILENCE_RMS or zero_frac > 0.9))
            return label, conf, prob
        self.router._classify = wrapped
        self._wav, self._wav_started, self._wav_bytes = None, 0.0, 0
        self.record_sec = 120                       # wav 파일 하나 길이 (초)
        self.keep_files = 15                        # 오래된 녹음은 지운다 (약 30분 보관)

    def _record(self, raw: bytes) -> None:
        """폰에서 온 소리를 그대로 wav 로 남긴다 → 정연 모듈 `python inference.py --file <wav>` 로 재현 가능."""
        import glob
        import wave
        now = time.time()
        if self._wav is None or now - self._wav_started >= self.record_sec:
            if self._wav is not None:
                self._wav.close()
            os.makedirs(self.record_dir, exist_ok=True)
            for old in sorted(glob.glob(os.path.join(self.record_dir, "mic_*.wav")))[:-self.keep_files]:
                try:
                    os.remove(old)
                except OSError:
                    pass
            path = os.path.join(self.record_dir, f"mic_{time.strftime('%Y%m%d_%H%M%S')}.wav")
            self._wav = wave.open(path, "wb")
            self._wav.setnchannels(1); self._wav.setsampwidth(2); self._wav.setframerate(self.sr)
            self._wav_started = now
        self._wav.writeframes(raw)

    def push_pcm16(self, raw: bytes) -> None:
        np = self.np
        if self.record_dir:
            self._record(raw)
        samples = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
        self.buf = np.concatenate([self.buf, samples])
        while len(self.buf) >= self.hop:
            chunk, self.buf = self.buf[:self.hop], self.buf[self.hop:]
            self.ring = np.concatenate([self.ring[self.hop:], chunk])
            if self.filled < self.n:          # 시작 직후 1초는 0으로 채운 버퍼라 판정하지 않는다
                self.filled += self.hop
                continue
            self.router.push(self.ring, chunk)

    def close(self) -> None:
        try:
            self.router.close(timeout=2.0)
        except Exception:
            pass
        if self._wav is not None:
            self._wav.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="phone", help="phone | mic")
    ap.add_argument("--stt-model", default="base")
    ap.add_argument("--no-stt", action="store_true")
    ap.add_argument("--input-device", default=None)
    ap.add_argument("--home", type=int, default=None)
    ap.add_argument("--debug", action="store_true", help="윈도우별 분류 결과(audio_debug) 이벤트")
    ap.add_argument("--record-dir", default=None, help="폰 마이크 소리를 wav 로 저장할 폴더 (logs/)")
    args = ap.parse_args()

    stop = threading.Event()

    if args.source != "phone":
        # ---- PC 마이크: 모듈의 AudioSensor 그대로 ----
        from audio_module import AudioSensor
        muted = threading.Event()
        dev = None
        if args.input_device is not None:
            dev = int(args.input_device) if args.input_device.lstrip("-").isdigit() else args.input_device
        print("[audio] 모델 로딩…", file=sys.stderr)
        try:
            sensor = AudioSensor(on_event=lambda ev: forward(args.home, ev, muted.is_set()),
                                 stt=not args.no_stt, stt_model=args.stt_model, input_device=dev)
            sensor.start()
        except Exception as e:
            emit("device", mic="error", detail=f"{type(e).__name__}: {e}", home=args.home)
            sys.exit(1)
        emit("device", mic="ok", stt=sensor.stt_enabled, device=sensor.device, home=args.home)

        def stdin_mic() -> None:
            for line in sys.stdin:
                try:
                    cmd = json.loads(line).get("cmd")
                except (json.JSONDecodeError, AttributeError):
                    continue
                if cmd == "mute":
                    muted.set()
                elif cmd == "unmute":
                    muted.clear()
                elif cmd == "stop":
                    break
            stop.set()

        threading.Thread(target=stdin_mic, daemon=True).start()
        try:
            while not stop.is_set():
                time.sleep(0.2)
        finally:
            sensor.stop()
        return

    # ---- 폰 PCM 스트림 ----
    import torch
    from config import CKPT_PATH
    from inference import SpeechToText
    from model import load_model

    print("[audio] 모델 로딩…", file=sys.stderr)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = load_model(CKPT_PATH, device)
    stt = SpeechToText(args.stt_model, enabled=not args.no_stt)
    emit("device", mic="worker_ready", stt=stt.model is not None, device=device)
    homes: dict = {}

    def home(h) -> HomeAudio:
        if h not in homes:
            rec = os.path.join(args.record_dir, f"home_{h}") if args.record_dir else None
            homes[h] = HomeAudio(h, model, stt, device, debug=args.debug, record_dir=rec)
        return homes[h]

    for line in sys.stdin:
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError:
            continue
        c, h = cmd.get("cmd"), cmd.get("home")
        try:
            if c == "pcm":
                home(h).push_pcm16(base64.b64decode(cmd["b64"]))
            elif c == "mute":
                home(h).muted = True
            elif c == "unmute":
                home(h).muted = False
            elif c == "stop":
                break
        except Exception as e:
            print(f"[audio] error: {e!r}", file=sys.stderr)
    for ha in homes.values():
        ha.close()


if __name__ == "__main__":
    main()
