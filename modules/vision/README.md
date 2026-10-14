# silvercare-tracking — 비전 모듈 (이유민 파트)

실버케어 AI 반려로봇의 **카메라 트래킹** 담당 모듈:
**포즈 기반 낙상 감지** + **안면 표정(감정) 인식**.

프로젝트 전체 개요는 [PROJECT.md](PROJECT.md), 할 일은 [TODO.md](TODO.md) 참고.

## 설치

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

모델 가중치는 `weights/`에 동봉 (없으면 첫 실행 시 자동 다운로드).

## 실행 (제출 규격 진입점)

```bash
.venv/bin/python inference.py                     # 웹캠 실시간. 종료: q
.venv/bin/python inference.py --source video.mp4  # 영상 파일 테스트
.venv/bin/python inference.py --no-show --log events.jsonl  # 서버 모드

.venv/bin/python train.py --data ./data/emotion   # 표정 파인튜닝 (데이터 확보 후)
```

## 테스트 / 평가

```bash
.venv/bin/python -m tests.test_fall_detector      # 낙상 로직 단위 테스트 (카메라 불필요)
.venv/bin/python scripts/download_urfall.py       # UR Fall 공개 데이터셋 다운로드
.venv/bin/python scripts/eval_fall.py             # 낙상 정량 평가 (P/R/F1 + 평가 로그)
.venv/bin/python scripts/eval_emotion.py          # 표정 정확도 평가 (라벨 폴더 필요)
```

## 구조

```
inference.py               # [제출] 실시간 추론 진입점
model.py                   # [제출] 모델 정의 + 통합용 파사드 (SilverCareVision)
train.py                   # [제출] 표정 파인튜닝 (노인 얼굴 데이터 확보 후)
weights/                   # [제출] 모델 가중치 동봉 (오프라인 구동 가능)
INTEGRATION.md             # [제출] ★ 통합 담당자용: 설치/실행/JSON 규약/연동 예시
src/
├── main.py                # 실시간 파이프라인 (inference.py의 실제 구현)
├── config.py              # 임계값 설정 (카메라 각도 확정 후 튜닝)
├── pose_tracker.py        # YOLOv8-pose 래퍼 (키포인트/자세 특징 추출)
├── fall_detector.py       # 낙상 상태머신 + pose_pred(fall/no_fall/uncertain) 출력
├── emotion_recognizer.py  # 표정 인식 (파인튜닝 모델 자동 사용, 5종+원본 8종 라벨)
├── events.py              # 팀 통합용 이벤트 버스
└── viz.py                 # 디버그 오버레이
scripts/
├── download_urfall.py     # UR Fall 공개 데이터셋 다운로드
├── eval_fall.py           # 낙상 정량 평가 → P/R/F1 + 유진 규약 평가 로그(jsonl)
└── eval_emotion.py        # 표정 정확도 평가 (혼동행렬)
```

## 팀 통합

이벤트 규약(JSON), 연동 예시 코드, STT 훅, 평가 로그 형식은 **[INTEGRATION.md](INTEGRATION.md)** 참고.
핵심: `pose_pred`(fall/no_fall/uncertain + conf, 0.5초 주기)가 융합 입력,
`emotion`(5종 라벨 + 원본 8종)이 대화 입력, `fall_alert`가 보호자 긴급 알림.

## 낙상 판별 로직 요약

1. **급락**: 엉덩이 중심 하강 속도 > 임계값 (화면높이의 55%/초)
2. **누움**: bbox 종횡비 ≥ 1.1 또는 상체 각도 ≥ 55° (수직 기준)
3. **낙상 확정**: 급락 직후 누운 자세 1.5초 지속 → `fall_detected`
4. **긴급 알림**: 이후 10초간 움직임 없음 → `fall_alert`
5. 천천히 눕는 동작(침대)은 급락이 없으므로 낙상으로 판정하지 않음
