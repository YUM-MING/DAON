# 비전 모듈 통합 가이드 (이유민 파트)

> 제3자(통합 담당)가 이 모듈을 붙일 때 필요한 모든 정보.
> 질문은 이유민에게.

## 1. 모듈이 하는 일

웹캠(또는 영상) 프레임을 입력받아 **카메라 단독으로**:
1. 포즈(스켈레톤 17관절) 추적 → 자세 분류 (standing / sitting / lying)
2. 낙상 판별 (급락 속도 + 누운 자세 지속 상태머신)
3. 안면 표정 인식 → 감정 분류

를 수행하고, 결과를 **JSON 이벤트**로 내보낸다. 센서 융합(레이더·소리)은 이
모듈 밖(혜원 파트)에서 `pose_pred` 스트림을 받아 수행한다.

## 2. 설치 및 실행 (Windows)

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

python inference.py                     # 웹캠 실시간 (종료: q)
python inference.py --source test.mp4   # 영상 파일
python inference.py --no-show --log events.jsonl   # 서버 모드 + 로그
```

- Python 3.12 권장 (3.10+ 동작).
- 모델 가중치는 `weights/`에 동봉 → **인터넷 없이 구동 가능.**
  (weights가 없으면 첫 실행 시 자동 다운로드)
- GPU 없어도 동작 (CPU 기준 약 15~25 FPS). CUDA/Apple MPS 자동 사용.
- 모든 경로는 상대경로(`./`) — 프로젝트 루트에서 실행할 것.

## 3. 출력 이벤트 규약 (JSON)

모든 이벤트 공통: `type`, `timestamp`(unix epoch 초).

| type | 주기/시점 | 필드 | 소비자 |
|---|---|---|---|
| `pose_pred` | 0.5초마다 | `pred`: `fall`/`no_fall`/`uncertain`, `conf`: 0~1 | **융합(혜원)**, 평가 로그 |
| `emotion` | 감정 변화 시 | `label`: happy/sad/angry/surprised/neutral, `raw`: 원본 8종 라벨, `confidence` | **대화(혜원)**, 로봇 표정(유진) |
| `fall_detected` | 낙상 확정 시 1회 | `confidence` | 대화 — 음성 1차 확인 트리거 ("괜찮으세요?") |
| `fall_recovered` | 기립/응답 시 | — | 알림 해제 |
| `fall_alert` | 낙상 후 10초 무움직임 | `elapsed_sec` | **보호자 UI(유진) — 긴급 팝업** |
| `person_status` | 2초마다 | `present`: bool, `posture`: standing/sitting/lying/unknown | 모니터링 UI |

- `uncertain`은 "카메라만으로는 애매함" (급락 직후, 확정 대기 등) — 융합에서
  레이더·소리로 결정할 것.
- `emotion.label`은 5종. 유진 UI가 4종으로 확정되면 `src/config.py`의
  `export_surprised=False` 한 줄로 surprised→neutral 매핑됨 (`raw`는 유지).

예시:
```json
{"type": "pose_pred", "timestamp": 1760000000.1, "pred": "uncertain", "conf": 0.6}
{"type": "emotion", "timestamp": 1760000001.2, "label": "sad", "raw": "Sadness", "confidence": 0.78}
{"type": "fall_alert", "timestamp": 1760000015.0, "elapsed_sec": 10.2}
```

## 4. 연동 방법 2가지

### A. 이벤트 구독 (파이썬 프로세스 내)
```python
from model import SilverCareVision
from src.events import EventBus

bus = EventBus()
bus.subscribe(lambda e: print(e.to_json()))   # ← 여기서 WebSocket 전송 등
vision = SilverCareVision(bus=bus)
# 이후 프레임 루프에서 vision.process_frame(frame)
```

### B. 프레임 단위 동기 호출 (폴링)
```python
import cv2
from model import SilverCareVision

vision = SilverCareVision()
cap = cv2.VideoCapture(0)
while True:
    ok, frame = cap.read()
    result = vision.process_frame(frame)
    # result = {"present":, "posture":, "pose": {"pred","conf"}, "emotion":, "fall_state":}
```

### STT 연동 훅 (정연→혜원 파트에서 호출)
어르신이 음성으로 응답하면(낙상 오탐 해제):
```python
vision.fall.notify_response()   # 긴급 알림 카운트다운 취소 + fall_recovered 발행
```

## 5. 평가 로그 (유진 분석 탭 규약)

`scripts/eval_fall.py`가 유진이 정의한 한 줄 JSON 형식으로 로그를 생성한다:
```json
{"ts": 3.5, "scenario_id": "fall-01", "ground_truth": "fall",
 "pose": {"pred": "fall", "conf": 0.9}}
```
- UR Fall 공개 데이터셋 평가 결과: `data/urfall/eval_results.json`
  (혼동행렬, Precision/Recall/F1 — 분석 탭 그래프 B의 "카메라 단독" 수치)
- 평가 로그: `data/urfall/eval_log.jsonl` (그래프 A 타임라인용)
- **현재 카메라 단독 성능 (UR Fall 70개 영상, RGB 320x240 원거리 조건):**
  F1 0.687 / Recall 0.767 / Precision 0.622.
  미탐은 주로 "카메라 정면 방향 낙상"(원근 단축), 오탐은 "바닥·침대에 빠르게 눕기"
  — 둘 다 카메라 단독의 원리적 한계로, 레이더·소리 융합과 uncertain 판정의 근거.
  실제 로봇 환경(HD 근거리)에서는 이보다 좋을 것으로 예상, 카메라 각도 확정 후 재보정 필요.
- **2026-10-24 폰 프로파일 재보정 (480x360, `--profile phone`)**: GMDCSA-24(160) + CAUCAFall(100) + UR Fall(70) 합산
  **F1 0.782 / R 0.755 / P 0.811** (데이터셋별 0.813 / 0.769 / 0.727). 이전 폰값은 합산 0.70. 임계값 조정 없이 다른 환경
  LE2I(298) F1 0.846, 가려짐 전용 OCCU(113) 0.832('급락 후 소실' 규칙을 끄면 0.224). 남은 미탐은 카메라 축 방향 낙상(원근 단축)·침대 위로 쓰러짐·즉시 기립, 오탐은 운동·소파에서
  일어나기. `scripts/eval_fall.py --data a,b,c --profile phone --resize 480x360 [--sweep] [--set KEY=VALUE]`.

## 6. 표정 모델 파인튜닝 (완료됨)

- 포즈: YOLOv8n-pose (COCO keypoints 사전학습) 그대로 사용.
- 표정: **노인 적응 파인튜닝 완료 — `weights/emotion_finetuned.onnx`가 동봉되어
  있고 inference.py가 자동으로 사용함.**
  - 문제: AffectNet 사전학습 모델은 노인 얼굴에서 약함 (주름·처진 눈매를 sad/angry로 오인)
  - 방법: AIHub "한국인 감정인식" 데이터에서 40~50대 얼굴 5,583장으로 파인튜닝
    (라벨은 어노테이터 3인 다수결로 정제), 60대 이상 100장으로 평가 — 연령 분리로 인물 누수 방지
  - **결과: 노인(60+) 표정 정확도 38.0% → 69.0%** (happy 0.87 / sad 0.95 /
    angry 0.52 / neutral 0.65 / surprised 0.22)
  - 상세: `data/emotion_test/eval_emotion_results.json`
- 재학습: `python train.py --data ./data/emotion --epochs 12`
  (HSEmotion AffectNet 백본에서 시작, ONNX 자동 내보내기)
- 정확도 측정: `python scripts/eval_emotion.py --data ./data/emotion_test`

## 7. 설정값 (src/config.py)

낙상 임계값(하강 속도, 종횡비, 지속시간)과 알림 타임아웃(기본 10초)은 전부
`src/config.py`에 모여 있음. **로봇 카메라 높이/각도 확정 후 실측 튜닝 필요** —
UI의 10초 카운트다운과 `alert_timeout_sec`를 맞출 것.


## 8. 2026-10-18 추가 (통합 중 보강)

- `src/config.py phone_config()` — 로봇 폰 전면 카메라(근거리) 프로파일. 통합 서버가 `source: phone` 이면 자동 사용.
  `FallConfig.require_lower_body_for_lying=True`: 엉덩이+무릎/발목 키포인트가 안 보이면 posture="unknown" (상반신만 보일 때 '누움' 오판 방지).
- `FallDetector.motion_state(now)` → `"moving" | "still" | None`. 키포인트 이동량 기반 움직임 유무. 레이더가 없을 때 융합의 `motion` 입력으로 쓴다.
- `FallDetector.lower_body_visible(pose)`.
- 통합 서버 워커(`server/workers/vision_worker.py`)는 포즈 추적기를 공유하고 집마다 FallDetector/EmotionRecognizer 를 둔다. 디버그 모드에서 1초마다 판정 근거(속도·종횡비·각도)를 내보내고 낙상 순간 프레임을 저장한다.

## 9. 2026-10-24 추가 (낙상 재보정)

- `phone_config()` 값 재보정 (위 5절). `FallConfig.lost_after_drop_sec` (폰 3.0초): 급락 직후 사람이 사라져 3초 이상 안 보이면
  `fall_detected` 에 `confidence 0.6, reason "drop_then_lost"` 가 실린다 (일반 확정은 0.9, reason 없음). 사라진 동안 `pose_pred` 는
  `uncertain 0.6` 으로 나가 융합이 다른 센서로 결정한다.
- `vision_debug` 에 `lower_body`(하반신 보임 여부)·`note`("하반신 미확인 → 판정 보류 …") 추가. 오디오팀 로그의 "자세 정보 없음"은
  상반신만 찍혀 보류된 것이지 오탐이 아니다. 대시보드 센서 로그에 그대로 표시.
- 워커 `--profile auto|default|phone`: 폰으로 녹화한 mp4 를 파일 소스로 재생할 때 `--profile phone` 으로 맞춘다.


## 9. 2026-10-20 추가 — 표정 2층 구조(mood) 와 7종 모델 (LABELS.md 4절)

- 표정 모델을 AIHub 7종(happy/neutral/sad/angry/anxious/surprised/hurt)으로 재학습 (Training 분할 40~50대 52,497장).
  60세 이상 983장 평가: 7종 0.775 / 5종 접은 정확도 0.796 (이전 5종 모델 0.666). 무표정→슬픔 누수 45/156 → 4/156.
- `src/mood.py MoodEstimator`: 순간 표정을 30초 창에서 집계해 **기분(mood) 8종** 으로 내보낸다
  (happy/calm/sad/anxious/angry/surprised/tired/unknown). 부정 기분은 비율·확신·3초 지속·개인 기준선 초과를 모두 넘겨야 한다.
  설정은 `src/config.py MoodConfig`.
- 새 이벤트: `expression_debug`(분류마다), `mood_pred`(1초), `mood_changed`(변화 시). 기존 `emotion` 이벤트는 그대로 나가되
  mood 에서 파생된다 (`label` 5종 + `mood` 필드 추가; anxious 는 `fear` 로). 서버·대화 모듈은 수정 없이 동작.
- `EmotionRecognizer.process(frame, pose, now=)` 에 시각을 넘기고, 얼굴이 없는 프레임에서는 `tick(now)` 를 호출할 것
  (5초 이상 얼굴이 없으면 mood=unknown). `SilverCareVision.process_frame` 결과에 `mood` 가 추가됨.
- 학습·평가 도구(train.py, scripts/, tests/)와 데이터는 저장소에 없음 (담당자 보관, `D:\DAON\vision_dev`).
