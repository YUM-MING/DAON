"""YOLOv8-pose 래퍼: 프레임 → 사람 포즈(키포인트 + bbox).

COCO 17 키포인트 인덱스:
0 nose, 1 l_eye, 2 r_eye, 3 l_ear, 4 r_ear,
5 l_shoulder, 6 r_shoulder, 7 l_elbow, 8 r_elbow, 9 l_wrist, 10 r_wrist,
11 l_hip, 12 r_hip, 13 l_knee, 14 r_knee, 15 l_ankle, 16 r_ankle
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from ultralytics import YOLO

from .config import PoseConfig

NOSE, L_EYE, R_EYE, L_EAR, R_EAR = 0, 1, 2, 3, 4
L_SHOULDER, R_SHOULDER = 5, 6
L_HIP, R_HIP = 11, 12


@dataclass
class PersonPose:
    keypoints: np.ndarray   # (17, 2) 픽셀 좌표
    kp_conf: np.ndarray     # (17,) 키포인트 confidence
    bbox: np.ndarray        # (4,) x1, y1, x2, y2
    conf: float             # 사람 검출 confidence

    def valid(self, idx: int, min_conf: float = 0.3) -> bool:
        return float(self.kp_conf[idx]) >= min_conf

    def midpoint(self, a: int, b: int) -> np.ndarray | None:
        """두 키포인트의 중점 (둘 다 유효할 때만)."""
        if self.valid(a) and self.valid(b):
            return (self.keypoints[a] + self.keypoints[b]) / 2.0
        if self.valid(a):
            return self.keypoints[a].copy()
        if self.valid(b):
            return self.keypoints[b].copy()
        return None

    @property
    def hip_center(self) -> np.ndarray | None:
        return self.midpoint(L_HIP, R_HIP)

    @property
    def shoulder_center(self) -> np.ndarray | None:
        return self.midpoint(L_SHOULDER, R_SHOULDER)

    @property
    def aspect_ratio(self) -> float:
        """bbox 가로/세로 비율. 서 있으면 <1, 누우면 >1."""
        x1, y1, x2, y2 = self.bbox
        h = max(float(y2 - y1), 1.0)
        return float(x2 - x1) / h

    @property
    def torso_angle_deg(self) -> float | None:
        """상체 축(엉덩이→어깨)이 수직에서 벗어난 각도. 0=직립, 90=수평(누움)."""
        hip, shoulder = self.hip_center, self.shoulder_center
        if hip is None or shoulder is None:
            return None
        dx, dy = shoulder[0] - hip[0], shoulder[1] - hip[1]
        norm = float(np.hypot(dx, dy))
        if norm < 1e-6:
            return None
        # 수직 벡터 (0, -1)과의 사잇각
        cos = -dy / norm
        return float(np.degrees(np.arccos(np.clip(cos, -1.0, 1.0))))

    def face_bbox(self, frame_shape: tuple[int, ...]) -> tuple[int, int, int, int] | None:
        """머리 키포인트(코/눈/귀)로 얼굴 영역 추정 → 표정 인식 입력 크롭.

        별도 얼굴 검출 모델 없이 포즈 모델 하나로 해결하기 위한 방식.
        """
        head_idx = [i for i in (NOSE, L_EYE, R_EYE, L_EAR, R_EAR) if self.valid(i)]
        if len(head_idx) < 2:
            return None
        pts = self.keypoints[head_idx]
        cx, cy = pts[:, 0].mean(), pts[:, 1].mean()
        # 머리 포인트 퍼짐 정도로 얼굴 크기 추정 (최소한 눈 사이 거리 기반)
        spread = max(pts[:, 0].max() - pts[:, 0].min(), pts[:, 1].max() - pts[:, 1].min())
        half = max(spread * 1.4, 20.0)
        h, w = frame_shape[:2]
        x1 = int(max(cx - half, 0))
        y1 = int(max(cy - half * 1.2, 0))  # 이마 포함하도록 위쪽 여유
        x2 = int(min(cx + half, w - 1))
        y2 = int(min(cy + half * 1.1, h - 1))
        if x2 - x1 < 10 or y2 - y1 < 10:
            return None
        return x1, y1, x2, y2


class PoseTracker:
    def __init__(self, config: PoseConfig) -> None:
        self.config = config
        # 동봉 가중치(./weights/) 우선, 없으면 모델명으로 자동 다운로드
        from pathlib import Path

        bundled = Path("./weights") / config.model_name
        self.model = YOLO(str(bundled) if bundled.exists() else config.model_name)

    def process(self, frame: np.ndarray) -> PersonPose | None:
        """프레임에서 가장 confidence 높은 사람 1명의 포즈 반환.

        독거노인 1인 가구 시나리오 기준. 다인 지원이 필요해지면
        track id 기반으로 확장.
        """
        results = self.model.predict(
            frame,
            conf=self.config.conf_threshold,
            device=self.config.device or None,
            verbose=False,
        )
        r = results[0]
        if r.keypoints is None or r.boxes is None or len(r.boxes) == 0:
            return None
        best = int(r.boxes.conf.argmax())
        kp = r.keypoints.xy[best].cpu().numpy()          # (17, 2)
        kp_conf = (
            r.keypoints.conf[best].cpu().numpy()
            if r.keypoints.conf is not None
            else np.ones(len(kp))
        )
        return PersonPose(
            keypoints=kp,
            kp_conf=kp_conf,
            bbox=r.boxes.xyxy[best].cpu().numpy(),
            conf=float(r.boxes.conf[best]),
        )
