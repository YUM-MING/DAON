"""모델 아키텍처 정의 및 통합용 파사드 (제출 규격: model.py).

구성 모델
1. 포즈 추적   : YOLOv8-pose (COCO 사전학습, 17 키포인트) — src/pose_tracker.py
2. 낙상 판별   : 규칙 기반 상태머신 (급락 속도 + 누운 자세 지속) — src/fall_detector.py
3. 표정 인식   : EfficientNet-B0 (AffectNet 사전학습 8클래스, ONNX)
                 파인튜닝 가중치(weights/emotion_finetuned.onnx)가 있으면 자동 사용
                 — src/emotion_recognizer.py / 파인튜닝은 train.py

통합(제3자) 사용 예시 — 프레임 단위 동기 API:

    import cv2
    from model import SilverCareVision

    vision = SilverCareVision()
    frame = cv2.imread("sample.jpg")          # BGR (OpenCV 기본)
    result = vision.process_frame(frame)
    print(result)
    # {'present': True, 'posture': 'standing',
    #  'pose': {'pred': 'no_fall', 'conf': 0.9},
    #  'emotion': {'label': 'happy', 'raw': 'Happiness', 'conf': 0.86},
    #  'fall_state': 'monitoring'}

이벤트 구독 방식(비동기)은 src/events.py 및 INTEGRATION.md 참고.
"""
from __future__ import annotations

import time
from typing import Any

import numpy as np

from src.config import AppConfig, default_config
from src.emotion_recognizer import EmotionRecognizer
from src.events import EventBus
from src.fall_detector import FallDetector
from src.pose_tracker import PoseTracker


class SilverCareVision:
    """비전 파이프라인 파사드: 프레임 입력 → 낙상/자세/감정 판정 출력."""

    def __init__(self, config: AppConfig | None = None, bus: EventBus | None = None) -> None:
        self.config = config or default_config()
        self.bus = bus or EventBus()
        self.tracker = PoseTracker(self.config.pose)
        self.fall = FallDetector(self.config.fall, self.bus)
        self.emotion = EmotionRecognizer(self.config.emotion, self.bus)

    def process_frame(self, frame_bgr: np.ndarray, now: float | None = None) -> dict[str, Any]:
        """BGR 프레임 1장 처리. 영상 파일 평가 시에는 now=프레임시각(초)을 넘길 것."""
        now = now if now is not None else time.time()
        pose = self.tracker.process(frame_bgr)
        posture = self.fall.update(pose, frame_bgr.shape[0], now=now)
        pred, conf = self.fall.current_pred(now=now)

        emotion_out = None
        if pose is not None:
            r = self.emotion.process(frame_bgr, pose)
            if r is not None:
                emotion_out = {
                    "label": r[0],
                    "raw": self.emotion.last_raw,
                    "conf": round(r[1], 3),
                }

        return {
            "present": pose is not None,
            "posture": posture,
            "pose": {"pred": pred, "conf": round(conf, 2)},
            "emotion": emotion_out,
            "fall_state": self.fall.state.value,
        }


__all__ = [
    "SilverCareVision",
    "PoseTracker",
    "FallDetector",
    "EmotionRecognizer",
    "EventBus",
    "default_config",
]
