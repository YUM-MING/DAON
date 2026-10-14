"""실시간 비전 파이프라인 진입점.

웹캠(또는 영상 파일) → 포즈 추적 → 낙상 판별 + 표정 인식 → 이벤트 발행.

사용법:
    python -m src.main                     # 기본 웹캠(0)
    python -m src.main --source video.mp4  # 녹화 영상으로 테스트
    python -m src.main --no-show           # 화면 없이 (서버 모드)
    python -m src.main --log events.jsonl  # 이벤트 로그 저장
종료: q
"""
from __future__ import annotations

import argparse
import time

import cv2

from .config import default_config
from .emotion_recognizer import EmotionRecognizer
from .events import EventBus, JsonlSink, console_sink
from .fall_detector import FallDetector
from .pose_tracker import PoseTracker
from .viz import draw_overlay


def main() -> None:
    parser = argparse.ArgumentParser(description="실버케어 비전 모듈 (낙상 감지 + 표정 인식)")
    parser.add_argument("--source", default="0", help="카메라 인덱스 또는 영상 파일 경로")
    parser.add_argument("--no-show", action="store_true", help="화면 출력 끄기")
    parser.add_argument("--log", default=None, help="이벤트 jsonl 로그 파일 경로")
    args = parser.parse_args()

    config = default_config()
    bus = EventBus()
    bus.subscribe(console_sink)
    if args.log:
        bus.subscribe(JsonlSink(args.log))

    tracker = PoseTracker(config.pose)
    fall = FallDetector(config.fall, bus)
    emotion = EmotionRecognizer(config.emotion, bus)

    source = int(args.source) if args.source.isdigit() else args.source
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise SystemExit(f"영상 소스를 열 수 없습니다: {args.source}")

    is_file = not isinstance(source, int)
    fps = cap.get(cv2.CAP_PROP_FPS) if is_file else 0
    frame_idx = 0
    last_status = 0.0
    last_pred = 0.0
    last_emotion: str | None = None

    print("[main] 시작. 종료하려면 q")
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frame_idx += 1
            # 영상 파일이면 영상 자체의 시간축 사용 (재생 속도와 무관하게 판별 일관성 유지)
            now = frame_idx / fps if (is_file and fps > 0) else time.time()

            pose = tracker.process(frame)
            posture = fall.update(pose, frame.shape[0], now=now)

            if pose is not None:
                result = emotion.process(frame, pose)
                if result is not None:
                    last_emotion = result[0]

            # 카메라 단독 낙상 판정 스트림 (융합 입력용, 유진 평가 로그 규약)
            if now - last_pred >= config.pred_interval_sec:
                pred, conf = fall.current_pred(now=now)
                bus.publish("pose_pred", pred=pred, conf=round(conf, 2))
                last_pred = now

            # 주기적 상태 보고 (모니터링 UI용)
            if now - last_status >= config.status_interval_sec:
                bus.publish("person_status", present=pose is not None, posture=posture)
                last_status = now

            if not args.no_show:
                draw_overlay(frame, pose, fall, posture, emotion.current or last_emotion)
                cv2.imshow("SilverCare Vision", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    finally:
        cap.release()
        cv2.destroyAllWindows()
    print("[main] 종료")


if __name__ == "__main__":
    main()
