"""디버그용 화면 오버레이 (개발/데모 시연용)."""
from __future__ import annotations

import cv2
import numpy as np

from .fall_detector import FallDetector, FallState
from .pose_tracker import PersonPose

# COCO 스켈레톤 연결 (키포인트 인덱스 쌍)
SKELETON = [
    (5, 6), (5, 7), (7, 9), (6, 8), (8, 10),       # 팔
    (5, 11), (6, 12), (11, 12),                     # 몸통
    (11, 13), (13, 15), (12, 14), (14, 16),         # 다리
    (0, 5), (0, 6),                                 # 머리-어깨
]

STATE_COLOR = {
    FallState.MONITORING: (80, 200, 80),
    FallState.FALLEN: (0, 165, 255),
    FallState.ALERTED: (0, 0, 255),
}


def draw_overlay(
    frame: np.ndarray,
    pose: PersonPose | None,
    detector: FallDetector,
    posture: str,
    emotion: str | None,
) -> np.ndarray:
    color = STATE_COLOR[detector.state]

    if pose is not None:
        x1, y1, x2, y2 = pose.bbox.astype(int)
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        for a, b in SKELETON:
            if pose.valid(a) and pose.valid(b):
                pa = tuple(pose.keypoints[a].astype(int))
                pb = tuple(pose.keypoints[b].astype(int))
                cv2.line(frame, pa, pb, (255, 200, 100), 2)
        for i in range(len(pose.keypoints)):
            if pose.valid(i):
                cv2.circle(frame, tuple(pose.keypoints[i].astype(int)), 3, (0, 255, 255), -1)
        face = pose.face_bbox(frame.shape)
        if face is not None:
            fx1, fy1, fx2, fy2 = face
            cv2.rectangle(frame, (fx1, fy1), (fx2, fy2), (200, 100, 255), 1)

    lines = [
        f"state: {detector.state.value}",
        f"posture: {posture}",
        f"emotion: {emotion or '-'}",
    ]
    if pose is not None:
        angle = pose.torso_angle_deg
        lines.append(f"aspect: {pose.aspect_ratio:.2f}  torso: {angle:.0f}deg" if angle is not None else f"aspect: {pose.aspect_ratio:.2f}")

    y = 24
    for line in lines:
        cv2.putText(frame, line, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3)
        cv2.putText(frame, line, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 1)
        y += 24

    if detector.state == FallState.ALERTED:
        cv2.putText(frame, "!! FALL ALERT !!", (10, y + 10), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)

    return frame
