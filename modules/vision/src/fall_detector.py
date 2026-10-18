"""규칙 기반 낙상 판별 상태머신.

판별 로직 (슬라이드 시나리오 B와 대응):
1. 급락 감지  : 엉덩이 중심의 하강 속도가 임계값 초과 → "급격한 무게중심 하강"
2. 누움 감지  : bbox 종횡비 or 상체 각도로 "비정상적 바닥 밀착" 판정
3. 낙상 확정  : 급락 직후(drop_memory_sec 내) 누운 자세가 fallen_confirm_sec 지속
               → fall_detected 이벤트 (로봇이 음성으로 1차 확인하는 트리거)
4. 긴급 알림  : 낙상 후 alert_timeout_sec 동안 움직임 없음
               → fall_alert 이벤트 (보호자 웹/앱 긴급 알림)
5. 회복       : 다시 서면 fall_recovered 후 정상 복귀

침대에 눕는 동작과의 구분: '급락'이 선행되어야만 낙상으로 판정.
천천히 눕는 것은 posture=lying 상태로만 보고된다.
"""
from __future__ import annotations

import time
from enum import Enum

import numpy as np

from .config import FallConfig
from .events import EventBus
from .pose_tracker import PersonPose


class FallState(Enum):
    MONITORING = "monitoring"   # 정상
    FALLEN = "fallen"           # 낙상 확정, 응답 대기
    ALERTED = "alerted"         # 보호자 알림 발송됨


class FallDetector:
    def __init__(self, config: FallConfig, bus: EventBus) -> None:
        self.config = config
        self.bus = bus
        self.state = FallState.MONITORING

        self._last_hip_y: float | None = None      # 엉덩이 y (픽셀)
        self._last_time: float | None = None
        self._scale: float | None = None           # 사람 크기(픽셀) EMA — 거리 불변 정규화용
        self._vel_window: list[tuple[float, float]] = []  # (시각, 순간속도) — 중앙값 필터용
        self._last_drop_time: float = 0.0          # 마지막 급락 관측 시각
        self._lying_since: float | None = None     # 누운 자세 시작 시각
        self._nonlying_since: float | None = None  # 누움 해제 후보 시작 시각 (디바운스)
        self._last_standing_time: float = 0.0      # 마지막 기립 관측 시각 (급전도 판정용)
        self._last_softdrop_time: float = 0.0      # 중간 수준 하강 관측 시각 (급전도 속도 조건)
        self._fallen_since: float | None = None    # 낙상 확정 시각
        self._last_motion_time: float = 0.0
        self._prev_keypoints: np.ndarray | None = None

    # ---------- 자세 판정 ----------

    # COCO 키포인트: 11,12 엉덩이 / 13,14 무릎 / 15,16 발목
    _HIPS, _LEGS = (11, 12), (13, 14, 15, 16)

    def lower_body_visible(self, pose: PersonPose) -> bool:
        return any(pose.valid(i) for i in self._HIPS) and any(pose.valid(i) for i in self._LEGS)

    def posture(self, pose: PersonPose) -> str:
        if self.config.require_lower_body_for_lying and not self.lower_body_visible(pose):
            return "unknown"          # 상반신만 보임 → 누움/서있음을 판단하지 않는다 (근거리 폰 오탐 방지)
        angle = pose.torso_angle_deg
        lying = pose.aspect_ratio >= self.config.lying_aspect_ratio or (
            angle is not None and angle >= self.config.lying_torso_angle_deg
        )
        if lying:
            return "lying"
        if angle is not None and angle < 25.0:
            return "standing"
        return "sitting"

    # ---------- 메인 업데이트 ----------

    def update(self, pose: PersonPose | None, frame_height: int, now: float | None = None) -> str:
        """매 프레임 호출. 현재 posture 문자열 반환 (person_status용)."""
        now = now if now is not None else time.time()

        if pose is None:
            # 사람이 안 보임. 낙상 상태에서 사라지면 상태 유지 (가려짐일 수 있음)
            self._last_hip_y = None
            self._prev_keypoints = None
            self._check_alert(now)
            return "unknown"

        posture = self.posture(pose)
        self._update_scale(pose)
        self._update_drop(pose, now)
        self._update_motion(pose, now)

        if posture == "standing":
            self._last_standing_time = now

        # 누움 추적 (디바운스: 순간적인 posture 깜빡임으로 타이머가 리셋되지 않게)
        if posture == "lying":
            self._nonlying_since = None
            if self._lying_since is None:
                self._lying_since = now
                # 보조 낙상 신호: 서있음 → 누움 급전환 (속도 게이트를 피하는
                # 느린 붕괴형 낙상 — 무릎 꺾임 등 — 을 포착).
                # 단, 최근에 중간 수준 이상의 하강이 있었을 때만 (제자리 눕기 제외)
                fast_transition = (
                    self._last_standing_time > 0
                    and now - self._last_standing_time <= self.config.standing_to_lying_sec
                )
                recent_softdrop = (
                    self._last_softdrop_time > 0
                    and now - self._last_softdrop_time <= self.config.soft_drop_window_sec
                )
                if fast_transition and recent_softdrop:
                    self._last_drop_time = now
        elif self._lying_since is not None:
            if self._nonlying_since is None:
                self._nonlying_since = now
            elif now - self._nonlying_since >= self.config.lying_debounce_sec:
                self._lying_since = None
                self._nonlying_since = None

        if self.state == FallState.MONITORING:
            self._check_fall(now)
        elif self.state in (FallState.FALLEN, FallState.ALERTED):
            if posture == "standing":
                self.bus.publish("fall_recovered")
                self._reset()
            else:
                self._check_alert(now)

        return posture

    # ---------- 내부 로직 ----------

    def _update_scale(self, pose: PersonPose) -> None:
        """사람 크기(픽셀) 추정: bbox 장변의 지수이동평균.

        서 있으면 높이≈키, 누우면 너비≈키이므로 장변이 자세와 무관하게
        사람 크기를 근사 → 카메라 거리에 불변인 속도 정규화가 가능.
        """
        x1, y1, x2, y2 = pose.bbox
        size = float(max(x2 - x1, y2 - y1))
        if size <= 1:
            return
        self._scale = size if self._scale is None else 0.9 * self._scale + 0.1 * size

    def _update_drop(self, pose: PersonPose, now: float) -> None:
        hip = pose.hip_center
        # 저신뢰 검출은 지터(한 프레임 오검출 → 비현실적 속도 스파이크)가 심해 제외
        if hip is None or self._scale is None or pose.conf < self.config.min_person_conf:
            self._last_hip_y = None
            return
        hip_y = float(hip[1])
        if self._last_hip_y is not None and self._last_time is not None:
            dt = now - self._last_time
            if 0 < dt < 0.5:  # 프레임 드랍 시 속도 왜곡 방지
                # 사람 크기 대비 하강 속도 (키/초) — 아래로 갈수록 +
                velocity = (hip_y - self._last_hip_y) / dt / self._scale
                self._vel_window.append((now, velocity))
        self._last_hip_y = hip_y
        self._last_time = now

        # 윈도 내 속도의 중앙값으로 판정: 실제 낙상은 여러 프레임 연속 고속 하강,
        # 지터는 단발 스파이크라 중앙값에서 걸러짐
        self._vel_window = [
            (t, v) for t, v in self._vel_window if now - t <= self.config.drop_window_sec
        ]
        if len(self._vel_window) >= 3:
            median_vel = float(np.median([v for _, v in self._vel_window]))
            if median_vel >= self.config.drop_velocity_threshold:
                self._last_drop_time = now
            if median_vel >= self.config.soft_drop_velocity:
                self._last_softdrop_time = now

    def _update_motion(self, pose: PersonPose, now: float) -> None:
        kp = pose.keypoints
        if (
            self._scale is not None
            and self._prev_keypoints is not None
            and len(kp) == len(self._prev_keypoints)
        ):
            valid = pose.kp_conf >= 0.3
            if valid.any():
                disp = np.linalg.norm(kp[valid] - self._prev_keypoints[valid], axis=1)
                if float(disp.mean()) / self._scale >= self.config.motion_epsilon:
                    self._last_motion_time = now
        self._prev_keypoints = kp.copy()

    def _check_fall(self, now: float) -> None:
        if self._lying_since is None or self._last_drop_time <= 0:
            return
        lying_long_enough = (now - self._lying_since) >= self.config.fallen_confirm_sec
        # 급락이 누운 자세 시작 직전(drop_memory_sec 이내) 또는 도중에 있었어야 낙상
        drop_linked = self._last_drop_time >= self._lying_since - self.config.drop_memory_sec
        if lying_long_enough and drop_linked:
            self.state = FallState.FALLEN
            self._fallen_since = now
            self._last_motion_time = now
            self.bus.publish("fall_detected", confidence=0.9)

    def _check_alert(self, now: float) -> None:
        if self.state != FallState.FALLEN or self._fallen_since is None:
            return
        no_motion = (now - self._last_motion_time) >= self.config.alert_timeout_sec
        if no_motion:
            self.state = FallState.ALERTED
            self.bus.publish("fall_alert", elapsed_sec=round(now - self._fallen_since, 1))

    def _reset(self) -> None:
        self.state = FallState.MONITORING
        self._lying_since = None
        self._fallen_since = None
        self._last_drop_time = 0.0

    def current_pred(self, now: float | None = None) -> tuple[str, float]:
        """융합(혜원 파트) 입력용 카메라 단독 판정: (pred, conf).

        pred ∈ fall / no_fall / uncertain — 애매한 구간은 uncertain으로 내보내
        레이더·소리 등 다른 센서가 결정하도록 한다.
        """
        now = now if now is not None else time.time()
        if self.state in (FallState.FALLEN, FallState.ALERTED):
            return "fall", 0.9
        drop_seen = self._last_drop_time > 0
        if (
            drop_seen
            and self._lying_since is not None
            and self._last_drop_time >= self._lying_since - self.config.drop_memory_sec
        ):
            return "uncertain", 0.6  # 급락 후 누움 — 확정 대기 중
        if drop_seen and (now - self._last_drop_time) <= self.config.drop_memory_sec:
            return "uncertain", 0.5  # 급락 직후
        return "no_fall", 0.9

    def notify_response(self) -> None:
        """통합용 훅: STT가 어르신 음성 응답을 감지하면 호출 → 알림 취소."""
        if self.state == FallState.FALLEN:
            self.bus.publish("fall_recovered")
            self._reset()
