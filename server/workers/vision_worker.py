"""비전 워커: modules/vision 을 자기 루트(cwd)에서 실행하고 이벤트를 stdout JSON lines로 내보낸다.

    python server/workers/vision_worker.py --source phone --fps 10      # 폰 브라우저 JPEG 프레임 (stdin)
    python server/workers/vision_worker.py --source 0                   # PC 웹캠 / 영상 파일

여러 집(home)을 동시에 처리한다: YOLO 포즈 추적기는 공유, 낙상 상태머신·표정 인식기는 집마다 하나.
stdin 명령
    {"cmd":"frame","home":1,"ts":..,"jpg":"<base64 jpeg>"}   폰 프레임 (최신 것만 처리, 밀리면 버림)
    {"cmd":"notify_response","home":1}                        어르신 응답 → 낙상 카운트다운 해제
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
VISION = ROOT / "modules" / "vision"
sys.path.insert(0, str(VISION))
os.chdir(VISION)                      # ./weights 상대경로


def emit(type: str, ts: float | None = None, **data) -> None:
    print(json.dumps({"type": type, "ts": ts if ts is not None else time.time(), **data}, ensure_ascii=False),
          flush=True)


class HomeVision:
    """집 하나의 낙상 상태머신 + 표정 인식기 (포즈 추적기는 밖에서 공유)."""

    def __init__(self, home: int, cfg, tracker, debug: bool = False, snap_dir: str | None = None) -> None:
        from src.emotion_recognizer import EmotionRecognizer
        from src.events import EventBus
        from src.fall_detector import FallDetector

        self.home, self.cfg, self.tracker, self.debug, self.snap_dir = home, cfg, tracker, debug, snap_dir
        bus = EventBus()
        bus.subscribe(self._on_module_event)
        self.fall = FallDetector(cfg.fall, bus)
        self.emotion = EmotionRecognizer(cfg.emotion, bus)
        self.last_pred = self.last_status = self.last_debug = 0.0
        self._frame = None
        self._last_emotion_sent = None

    def _on_module_event(self, e) -> None:
        emit(e.type, ts=e.timestamp, home=self.home, **e.data)
        if e.type == "emotion":
            self._last_emotion_sent = e.data.get("label")
        if e.type == "fall_detected" and self.debug and self.snap_dir is not None and self._frame is not None:
            import cv2
            os.makedirs(self.snap_dir, exist_ok=True)
            path = os.path.join(self.snap_dir, f"fall_{int(e.timestamp)}.jpg")
            cv2.imwrite(path, self._frame)        # 낙상으로 본 순간의 프레임 (오탐 분석용)
            print(f"[vision] 낙상 스냅샷 저장: {path}", file=sys.stderr)

    def process(self, frame, now: float) -> None:
        self._frame = frame
        pose = self.tracker.process(frame)
        posture = self.fall.update(pose, frame.shape[0], now=now)
        pred, conf = self.fall.current_pred(now=now)
        emo = self.emotion.process(frame, pose) if pose is not None else None
        if now - self.last_pred >= self.cfg.pred_interval_sec:
            emit("pose_pred", ts=now, home=self.home, pred=pred, conf=round(conf, 2), fall_state=self.fall.state.value)
            self.last_pred = now
        if now - self.last_status >= self.cfg.status_interval_sec:
            emit("person_status", ts=now, home=self.home, present=pose is not None, posture=posture)
            self.last_status = now
        if self.debug:
            if emo is not None:
                box = pose.face_bbox(frame.shape)
                face_px = int(min(box[2] - box[0], box[3] - box[1])) if box else 0
                emit("emotion_debug", ts=now, home=self.home, label=emo[0], raw=self.emotion.last_raw,
                     conf=round(float(emo[1]), 2), face_px=face_px, stable=(emo[0] == self.emotion.current))
            if now - self.last_debug >= 1.0:
                self.last_debug = now
                emit("vision_debug", ts=now, home=self.home, **self._fall_internals(pose, posture, pred, conf, now))

    def _fall_internals(self, pose, posture: str, pred: str, conf: float, now: float) -> dict:
        """낙상 판정 근거(모듈 내부값)를 읽기만 한다 — 오탐 튜닝용. src/config.py FallConfig 값과 비교."""
        import numpy as np
        f = self.fall
        vels = [v for _, v in f._vel_window]
        angle = pose.torso_angle_deg if pose is not None else None
        return {"pred": pred, "conf": round(conf, 2), "posture": posture, "fall_state": f.state.value,
                "median_vel": round(float(np.median(vels)), 3) if len(vels) >= 3 else None,
                "aspect": round(float(pose.aspect_ratio), 2) if pose is not None else None,
                "torso_angle": round(float(angle), 1) if angle is not None else None,
                "person_conf": round(float(pose.conf), 2) if pose is not None else None,
                "drop_recent": bool(f._last_drop_time > 0 and now - f._last_drop_time <= self.cfg.fall.drop_memory_sec),
                "lying_sec": round(now - f._lying_since, 1) if f._lying_since else 0,
                "scale_px": round(float(f._scale), 0) if f._scale else None}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="phone", help="phone | 카메라 인덱스 | 영상 파일")
    ap.add_argument("--fps", type=float, default=10)
    ap.add_argument("--home", type=int, default=None, help="PC 웹캠일 때 이 집의 센서로 보고 (없으면 첫 집)")
    ap.add_argument("--debug", action="store_true", help="판정 근거(vision_debug/emotion_debug) 이벤트 + 낙상 스냅샷")
    ap.add_argument("--log-dir", default=None, help="낙상 스냅샷 저장 폴더 (logs/)")
    args = ap.parse_args()

    import cv2
    import numpy as np
    from src.config import default_config, phone_config
    from src.pose_tracker import PoseTracker

    print("[vision] 모델 로딩…", file=sys.stderr)
    cfg = phone_config() if args.source == "phone" else default_config()   # 근거리 폰 프로파일 (src/config.py)
    tracker = PoseTracker(cfg.pose)
    homes: dict[int | None, HomeVision] = {}

    def home(h):
        if h not in homes:
            snap = os.path.join(args.log_dir, f"home_{h}") if args.log_dir else None
            homes[h] = HomeVision(h, cfg, tracker, debug=args.debug, snap_dir=snap)
        return homes[h]

    stop = threading.Event()
    latest: dict = {}
    lock = threading.Lock()

    def stdin_loop() -> None:
        for line in sys.stdin:
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                continue
            c = cmd.get("cmd")
            if c == "frame":
                with lock:
                    latest[cmd.get("home")] = (cmd.get("jpg"), float(cmd.get("ts") or time.time()))
            elif c == "notify_response":
                home(cmd.get("home")).fall.notify_response()
            elif c == "stop":
                break
        stop.set()

    threading.Thread(target=stdin_loop, daemon=True).start()
    interval = 1.0 / args.fps if args.fps > 0 else 0

    if args.source == "phone":
        emit("device", camera="worker_ready", source="phone")
        print("[vision] 폰 프레임 대기", file=sys.stderr)
        while not stop.is_set():
            with lock:
                items = list(latest.items())
                latest.clear()
            if not items:
                time.sleep(0.01)
                continue
            for h, (b64, ts) in items:
                try:
                    buf = np.frombuffer(base64.b64decode(b64), np.uint8)
                    frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
                    if frame is None:
                        continue
                    home(h).process(frame, time.time())
                except Exception as e:
                    print(f"[vision] frame error: {e!r}", file=sys.stderr)
        return

    # ---- PC 웹캠 / 영상 파일 ----
    source = int(args.source) if args.source.isdigit() else args.source
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        emit("device", camera="error", detail=f"영상 소스를 열 수 없음: {args.source}")
        sys.exit(1)
    emit("device", camera="ok", source=str(args.source), home=args.home)
    is_file = not isinstance(source, int)
    hv = home(args.home)
    try:
        while not stop.is_set():
            t0 = time.time()
            ok, frame = cap.read()
            if not ok:
                if is_file:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                emit("device", camera="error", detail="프레임 읽기 실패", home=args.home)
                break
            hv.process(frame, time.time())
            dt = time.time() - t0
            if interval and dt < interval:
                time.sleep(interval - dt)
    finally:
        cap.release()


if __name__ == "__main__":
    main()
