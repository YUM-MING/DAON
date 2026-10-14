"""음성 워커: modules/audio_ai 의 AudioSensor 를 자기 루트(cwd)에서 실행하고 이벤트를 stdout JSON lines로 내보낸다.

    python server/workers/audio_worker.py --stt-model base [--no-stt] [--input-device N]
stdin 명령: {"cmd":"mute"} / {"cmd":"unmute"}  (TTS 재생 중 speech/voice_unclear 만 버림, 낙상 감지는 계속)
            {"cmd":"stop"}
"""
from __future__ import annotations

import argparse
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


def emit(type: str, ts: float | None = None, **data) -> None:
    print(json.dumps({"type": type, "ts": ts if ts is not None else time.time(), **data}, ensure_ascii=False),
          flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stt-model", default="base")
    ap.add_argument("--no-stt", action="store_true")
    ap.add_argument("--input-device", default=None)
    args = ap.parse_args()

    from audio_module import AudioSensor

    muted = threading.Event()
    stop = threading.Event()

    def on_event(ev: dict) -> None:
        t = TYPE_MAP.get(ev.get("type"))
        if not t:
            return
        if muted.is_set() and t != "audio_fall":
            return                      # 로봇 자기 목소리(TTS) 에코 방지
        data = {k: v for k, v in ev.items() if k not in ("type", "time")}
        data["sensor_time"] = ev.get("time")
        emit(t, **data)                 # ts = 수신 시각(epoch) — 센서의 time(경과초)은 sensor_time 으로 보존

    dev = None
    if args.input_device is not None:
        dev = int(args.input_device) if args.input_device.lstrip("-").isdigit() else args.input_device

    print("[audio] 모델 로딩…", file=sys.stderr)
    try:
        sensor = AudioSensor(on_event=on_event, stt=not args.no_stt, stt_model=args.stt_model, input_device=dev)
        sensor.start()
    except Exception as e:  # 마이크 없음, 가중치 없음 등
        emit("device", mic="error", detail=f"{type(e).__name__}: {e}")
        sys.exit(1)
    emit("device", mic="ok", stt=sensor.stt_enabled, device=sensor.device)

    def stdin_loop() -> None:
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

    threading.Thread(target=stdin_loop, daemon=True).start()
    try:
        while not stop.is_set():
            time.sleep(0.2)
    finally:
        sensor.stop()


if __name__ == "__main__":
    main()
