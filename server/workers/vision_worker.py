"""비전 워커: modules/vision 을 자기 루트(cwd)에서 실행하고 이벤트를 stdout JSON lines로 내보낸다.

    python server/workers/vision_worker.py --source 0 --fps 15
stdin 명령: {"cmd":"notify_response"} (어르신 응답 → 낙상 카운트다운 해제), {"cmd":"stop"}
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
VISION = ROOT / "modules" / "vision"
sys.path.insert(0, str(VISION))
os.chdir(VISION)                      # ./weights 상대경로


def emit(type: str, ts: float | None = None, **data) -> None:
    print(json.dumps({"type": type, "ts": ts if ts is not None else time.time(), **data}, ensure_ascii=False),
          flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="0")
    ap.add_argument("--fps", type=float, default=15)
    args = ap.parse_args()

    import cv2
    from model import SilverCareVision
    from src.events import EventBus

    bus = EventBus()
    bus.subscribe(lambda e: emit(e.type, ts=e.timestamp, **e.data))
    print("[vision] 모델 로딩…", file=sys.stderr)
    vision = SilverCareVision(bus=bus)

    source = int(args.source) if args.source.isdigit() else args.source
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        emit("device", camera="error", detail=f"영상 소스를 열 수 없음: {args.source}")
        sys.exit(1)
    emit("device", camera="ok", source=str(args.source))

    stop = threading.Event()

    def stdin_loop() -> None:
        for line in sys.stdin:
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cmd.get("cmd") == "notify_response":
                vision.fall.notify_response()
            elif cmd.get("cmd") == "stop":
                stop.set()
                return
        stop.set()

    threading.Thread(target=stdin_loop, daemon=True).start()

    is_file = not isinstance(source, int)
    file_fps = cap.get(cv2.CAP_PROP_FPS) if is_file else 0
    interval = 1.0 / args.fps if args.fps > 0 else 0
    frame_idx, last_pred, last_status = 0, 0.0, 0.0
    cfg = vision.config
    try:
        while not stop.is_set():
            t0 = time.time()
            ok, frame = cap.read()
            if not ok:
                if is_file:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)   # 영상 파일은 반복 재생
                    continue
                emit("device", camera="error", detail="프레임 읽기 실패")
                break
            frame_idx += 1
            now = time.time()
            r = vision.process_frame(frame, now=now)
            if now - last_pred >= cfg.pred_interval_sec:
                emit("pose_pred", ts=now, pred=r["pose"]["pred"], conf=r["pose"]["conf"], fall_state=r["fall_state"])
                last_pred = now
            if now - last_status >= cfg.status_interval_sec:
                emit("person_status", ts=now, present=r["present"], posture=r["posture"])
                last_status = now
            dt = time.time() - t0
            if interval and dt < interval:
                time.sleep(interval - dt)
    finally:
        cap.release()


if __name__ == "__main__":
    main()
