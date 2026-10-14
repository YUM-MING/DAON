"""비전 모듈 설정값.

낙상 판별 임계값은 카메라 높이/각도에 따라 달라지므로,
로봇 디자인(카메라 시점) 확정 후 실측으로 튜닝할 것.
"""
from dataclasses import dataclass


@dataclass
class PoseConfig:
    model_name: str = "yolov8n-pose.pt"  # 경량 모델. 정확도 필요 시 yolov8s-pose.pt
    conf_threshold: float = 0.5          # 사람 검출 최소 confidence
    device: str = ""                     # ""=자동 (mps/cuda/cpu)


@dataclass
class FallConfig:
    # 낙하 감지: 엉덩이 중심의 하강 속도 (사람 크기 대비 비율/초 — 카메라 거리 불변)
    # 중앙값 필터를 거친 속도의 임계값 (사람 키/초). 필터가 피크를 평활화하므로
    # 순간 속도보다 낮게 설정. UR Fall(저해상도·원거리) 실측 기준이며,
    # 로봇 카메라(근거리·고해상도) 확정 후 재보정 필요.
    drop_velocity_threshold: float = 0.45
    # 속도 중앙값 윈도(초): 키포인트 지터로 인한 단발 스파이크 제거
    drop_window_sec: float = 0.35
    # 이 confidence 미만의 사람 검출은 속도 계산에서 제외 (오검출 지터 방지)
    min_person_conf: float = 0.5
    # 누운 자세 판정: bbox 가로/세로 비율이 이 값 이상이면 '누움' 후보
    lying_aspect_ratio: float = 1.1
    # 누운 자세 판정: 상체(어깨-엉덩이) 축이 수직에서 벗어난 각도(도)
    lying_torso_angle_deg: float = 55.0
    # '급락 후 누움' 상태가 이 시간(초) 지속되면 fall_detected 확정
    # (짧게 설정: 로봇이 빨리 "괜찮으세요?"를 물어보고, 일어나면 즉시 해제되므로
    #  확정이 빨라도 과한 알림으로 이어지지 않음 — 보호자 알림은 별도 10초 대기)
    fallen_confirm_sec: float = 0.5
    # 보조 낙상 신호: 서있음 → 누움 전환이 이 시간(초) 이내면 급전도로 간주
    # (정상적으로 누울 때는 앉는 단계를 거쳐 전환이 더 오래 걸림)
    standing_to_lying_sec: float = 1.2
    # 급전도 보조 신호의 속도 조건: 최근 soft_drop_window_sec 안에 중앙값 속도가
    # soft_drop_velocity 이상이었던 경우에만 급전도 인정 (제자리 눕기 오탐 방지)
    soft_drop_velocity: float = 0.25
    soft_drop_window_sec: float = 2.0
    # 누움 판정 디바운스(초): 이 시간 미만의 순간적 '누움 아님' 판정은 무시
    # (키포인트 흔들림으로 posture가 깜빡여 확정 타이머가 리셋되는 것 방지)
    lying_debounce_sec: float = 0.5
    # 낙상 확정 후 이 시간(초) 동안 움직임/기립이 없으면 fall_alert (보호자 알림)
    alert_timeout_sec: float = 10.0
    # 움직임 판정: 키포인트 평균 이동량(사람 크기 대비)이 이 값 미만이면 '움직임 없음'
    motion_epsilon: float = 0.008
    # 급락 이벤트의 유효 시간(초): 이 시간 안에 누운 자세가 관측되어야 낙상으로 연결
    drop_memory_sec: float = 3.0


@dataclass
class EmotionConfig:
    model_name: str = "enet_b0_8_best_afew"  # HSEmotion 8-class (AffectNet)
    every_n_frames: int = 10                 # 매 프레임 돌릴 필요 없음 (부하 절약)
    min_face_size: int = 40                  # 얼굴 크롭 최소 픽셀 (너무 작으면 스킵)
    # 같은 감정이 이 횟수 연속 관측되어야 emotion 이벤트 발행 (깜빡임 방지)
    stable_count: int = 2
    # 전송 라벨에 surprised 포함 여부 (유진 UI 규약이 4종으로 확정되면 False)
    export_surprised: bool = True
    # 파인튜닝 모델 경로 (train.py 산출물). 존재하면 HSEmotion 대신 사용
    custom_model_path: str = "./weights/emotion_finetuned.onnx"
    custom_labels_path: str = "./weights/emotion_labels.json"


@dataclass
class AppConfig:
    pose: PoseConfig
    fall: FallConfig
    emotion: EmotionConfig
    status_interval_sec: float = 2.0  # person_status 이벤트 발행 주기
    pred_interval_sec: float = 0.5    # pose_pred(fall/no_fall/uncertain) 발행 주기 (융합 입력)


def default_config() -> AppConfig:
    return AppConfig(pose=PoseConfig(), fall=FallConfig(), emotion=EmotionConfig())
