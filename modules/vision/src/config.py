"""비전 모듈 설정값.

낙상 판별 임계값은 카메라 높이/각도에 따라 달라지므로,
로봇 디자인(카메라 시점) 확정 후 실측으로 튜닝할 것.
"""
from dataclasses import dataclass, field


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
    # '누움' 판정에 하반신(엉덩이 + 무릎/발목) 키포인트가 보여야 하는지.
    # 폰을 가까이 두면 상반신만 잡혀 bbox 가 납작해지고(종횡비↑) 서 있어도 '누움'으로 오판한다 (2026-10-18 실측).
    # True 면 하반신이 안 보일 때 posture="unknown" 으로 두고 낙상 판정을 보류한다.
    require_lower_body_for_lying: bool = False
    # '급락 후 소실' 보조 낙상 신호 (LABELS.md 2절): 급락(drop_memory_sec 내) 직후 사람이 화면에서 사라져
    # 이 시간(초) 이상 보이지 않으면 fall_detected (근거리에서 넘어져 화면 밖/카메라 아래로 사라지는 경우).
    # 0 이면 끔. 켜도 current_pred 는 사라진 동안 uncertain 으로 융합에 넘긴다.
    lost_after_drop_sec: float = 0.0


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
    # 분류 1회마다 expression_debug 이벤트 발행 (학습 데이터 수집·튜닝용. 부하는 작지만 로그가 많음)
    publish_expression: bool = True
    # 2층 '기분(mood)' 집계 설정 (LABELS.md 4.2)
    mood: "MoodConfig" = field(default_factory=lambda: MoodConfig())


@dataclass
class MoodConfig:
    """순간 표정(expression) → 기분(mood) 집계 규칙. 값의 근거는 LABELS.md 4.2.

    부정 기분(sad/anxious/angry)은 비율·확신·지속 세 조건을 모두 넘겨야 내보낸다.
    무표정이 슬픔으로 새는 문제를 모델(7종 재학습)과 이 게이트 두 겹으로 막는다.
    """
    window_sec: float = 30.0            # 집계 창
    pred_interval_sec: float = 1.0      # mood_pred 발행 주기
    min_samples: int = 3                # 창 안 표본이 이보다 적으면 판단 보류(이전 값 유지)
    unknown_after_sec: float = 5.0      # 이 시간 동안 얼굴이 없으면 unknown
    # 부정 기분 게이트
    negative_ratio_min: float = 0.5
    negative_conf_min: float = 0.6
    negative_sustain_sec: float = 3.0
    # 상처(hurt)는 무표정과 겹친다 (7종 평가에서 neutral 23장 중 4장이 hurt로) → 더 높은 확신일 때만 sad 로 센다
    hurt_conf_min: float = 0.7
    # 얼굴이 이보다 작으면(px) 표본으로 세지 않는다 — 원거리 40px 얼굴에서 conf 0.2 짜리 판정이 기분을 정하지 않게
    min_face_px: int = 48
    # 긍정·놀람 (기쁨도 확신 하한을 둔다: 멀리서 흐릿한 happy 로 로봇이 따라 웃지 않게)
    happy_ratio_min: float = 0.4
    happy_conf_min: float = 0.5
    surprised_ratio_min: float = 0.5
    surprised_window_sec: float = 5.0   # 놀람은 짧은 창으로 본다
    # 개인 기준선: 처음 baseline_sec 동안의 표정 분포를 '평소 얼굴'로 저장.
    # 부정 기분은 기준선 대비 초과분(excess)이 baseline_excess_min 이상이어야 인정
    # (평소 입꼬리가 처진 분은 sad 비율이 늘 높게 나오므로)
    baseline_sec: float = 120.0
    baseline_min_samples: int = 20
    baseline_excess_min: float = 0.25
    # 피곤(tired): 분류기가 아니라 규칙. 고개 숙임(코-어깨 상대 위치)이 기준선보다 tired_droop_delta 이상
    # 낮아진 채 tired_sustain_sec 지속, 또는 창 안 눈 감김 비율 ≥ tired_eye_closed_ratio (눈 감김 신호가 있을 때만)
    tired_droop_delta: float = 0.25
    tired_sustain_sec: float = 10.0
    tired_eye_closed_ratio: float = 0.3
    # 확신 구간 → intensity
    intensity_mid: float = 0.6
    intensity_high: float = 0.8


@dataclass
class AppConfig:
    pose: PoseConfig
    fall: FallConfig
    emotion: EmotionConfig
    status_interval_sec: float = 2.0  # person_status 이벤트 발행 주기
    pred_interval_sec: float = 0.5    # pose_pred(fall/no_fall/uncertain) 발행 주기 (융합 입력)


def default_config() -> AppConfig:
    return AppConfig(pose=PoseConfig(), fall=FallConfig(), emotion=EmotionConfig())


def phone_config() -> AppConfig:
    """로봇 폰 전면 카메라(근거리, 320x240 전송) 프로파일.

    UR Fall(원거리) 기준 기본값은 근거리에서 오탐이 많다: 작은 움직임이 큰 '속도'로 보이고,
    상반신만 보이면 bbox 가 납작해 '누움'으로 본다.

    2026-10-23 재보정: GMDCSA-24(집·노트북 웹캠·책상 높이, 낙상 79/일상 81) + UR Fall(70) 을 480x360 으로
    합산 sweep (scripts/eval_fall.py --sweep, 1,080 조합). 이전 값(속도 0.7)은 GMDCSA 재현율 0.61 로 낙상을
    너무 많이 놓쳤다 → 속도 0.35. 하반신 필수는 유지(근거리 오탐 보호). 확정 1.0초·전환 2.0초는 재현율/오탐
    중간값(데모 2-2 '천천히 미끄러짐' 대비). 결과: GMDCSA F1 0.805 (R 0.76 / P 0.86), UR Fall F1 0.727.
    남은 미탐은 '침대 위로 쓰러짐'·'바닥에 앉았다 뒤로 넘어짐'(엉덩이 하강이 거의 없음) — 규칙 구조의 한계.

    2026-10-24 CAUCAFall(100) 합류 + '급락 후 소실' 보조 신호: 3종 330클립 합산 F1 0.782 (R 0.755 / P 0.811, FP 28;
    GMDCSA 0.813 / UR Fall 0.727 / CAUCA 0.769). 이전 폰값(속도 0.7)은 합산 0.70. 조정 없이 LE2I(298) F1 0.846, 가려짐 OCCU(113) 0.832 (소실 규칙 끄면 0.224).
    카메라 축 방향 낙상(원근 단축: UP_Fall 정면, EDF)·천장 어안(MCFD)은 2D 포즈 규칙으로 안 됨 —
    '높이 붕괴' 보조 신호·'하반신 조건 근거리 한정' 변형을 시험했으나 정면에서 오탐이 늘거나 효과가 없어 불채택.
    상세: vision_dev/docs/fall_benchmark_omnifall_2026-10-24.md
    """
    fall = FallConfig(
        drop_velocity_threshold=0.35,
        soft_drop_velocity=0.4,
        lying_aspect_ratio=1.25,
        lying_torso_angle_deg=65.0,
        fallen_confirm_sec=1.0,
        standing_to_lying_sec=2.0,
        min_person_conf=0.6,
        drop_memory_sec=2.0,
        require_lower_body_for_lying=True,
        # 급락 후 3초 이상 소실 → 낙상 (CAUCAFall 재현율 0.60→0.70, GMDCSA 0.76→0.77, UR Fall 오탐 증가 없음.
        # 1~2초로 줄이면 UR Fall 에서 '물건 줍고 화면 밖으로' 오탐이 늘어남)
        lost_after_drop_sec=3.0,
    )
    emotion = EmotionConfig(min_face_size=48, stable_count=3)
    return AppConfig(pose=PoseConfig(), fall=fall, emotion=emotion)
