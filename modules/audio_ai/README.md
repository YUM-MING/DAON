# 음성 / 소리 AI 모듈 — 실버케어 AI

담당: 한정연 · 역할: **소리 감지 및 분리(대화 / 낙상·충격음 / 일상 소음) → 대화로 판정되면 STT**

모든 경로는 이 폴더 기준 상대경로(`./`)입니다.

---

## 통합 담당자는 여기부터 (읽는 순서)

| 순서 | 파일 | 소요 | 내용 |
|---|---|---|---|
| 1 | **[INTEGRATION.md](./INTEGRATION.md)** | 15분 | **이것만 읽으면 붙일 수 있습니다.** 설치, 3줄 연동 코드, 이벤트 3종 규격, 임계값, 스레드 주의, 트러블슈팅 |
| 2 | `outputs/benchmark.txt` | 2분 | 실시간성 측정값 (추론 지연, 예산 사용률, STT 지연) |
| 3 | `outputs/realworld_eval.txt` | 2분 | 실사용 환경 성능 (어떤 조건에서 놓치는지) |

나머지 문서는 필요할 때만 보면 됩니다.

- `SUBMISSION.md` — 제출물 대조표, 핵심 수치 요약, 팀원별 전달사항
- `DATASET.md` — 데이터 출처·라이선스·재현 방법
- `EXPERIMENTS.md` — 설계 판단 근거 (왜 이 임계값인지, 무엇을 시도했다 기각했는지)
- `ROADMAP.md` — 후속 보완 계획

**연동에 필요한 코드는 `audio_module.py` 하나입니다.**

```python
from audio_module import AudioSensor

def on_event(ev):
    if ev["type"] == "fall":           handle_fall(ev["confidence"])
    elif ev["type"] == "speech":       handle_utterance(ev["text"])
    elif ev["type"] == "voice_unclear": handle_unclear(ev["duration"])

with AudioSensor(on_event=on_event):
    run_app()
```

### 통합 전 확인할 것 2가지

1. **`fall` 이벤트는 최종 판정이 아니라 의심 신호입니다.** 소리 단독으로 오탐을 0으로 만들 수 없습니다(근거: `outputs/threshold_val.txt`). 포즈 인식과의 교차검증이 설계에 필요합니다.
2. **TTS 재생 중에는 `sensor.stop()` 으로 꺼주세요.** 안 그러면 로봇이 자기 말을 듣고 STT를 돌립니다. 재생이 끝나면 `sensor.start()`.

---

## 1. 무엇을 하는 모듈인가

```
마이크 입력 (16kHz)
   │
   ├─ 1초 윈도우(0.5초 간격)로 잘라 소리 분류 CNN 에 입력
   │
   ├─ fall    → {"type":"fall","confidence":0.93}      팀 파이프라인에 낙상 이벤트 전달
   ├─ speech  → 발화가 끝날 때까지 모았다가 STT 실행
   │              {"type":"speech","text":"오늘 날씨가 춥네"}
   └─ ambient → 무시
```

- **소리 분류기**: 직접 설계·학습 (`model.py`, `train.py`, 가중치 `checkpoints/sound_cls.pt`)
- **STT**: 사전학습 `faster-whisper` 사용 (추후 노인 음성 파인튜닝으로 교체 예정)

---

## 2. 폴더 구조

```
audio_ai/
├── config.py             공유 설정값 (샘플레이트, 클래스, 경로)
├── model.py              소리 분류 모델 정의 (log-Mel + 4블록 CNN, 294K 파라미터)
├── train.py              학습 · 평가 · 가중치 저장
├── prepare_data.py       ESC-50 다운로드 + 음원 → 1초 조각 + manifest.csv
├── record_samples.py     학습용 소리 직접 녹음 도구
├── inference.py          실시간 마이크 추론 CLI (분류 → STT)
├── audio_module.py       ★ 팀 통합용 API (AudioSensor) — 다른 모듈은 이것만 import
├── benchmark.py          실시간성 측정 (지연 ms, RTF)
├── requirements.txt
├── INTEGRATION.md        ★ 통합 규격서 (이벤트 스키마, 임계값, 트러블슈팅)
├── DATASET.md            ★ 데이터셋 출처 · 라이선스 · 분할 규칙
├── data/
│   ├── raw/fall/                    낙상·충격음 원본 (Pixabay 65 + 직접녹음 3)
│   ├── raw/speech/LibriSpeech/      대화 음성 (dev-clean)
│   ├── raw/esc50/                   ESC-50 (자동 다운로드)
│   └── processed/                   1초 조각 + manifest.csv
├── checkpoints/sound_cls.pt         학습 가중치 (제출물, 약 1.2MB)
└── outputs/                         train_log.csv, report.txt, benchmark.txt
```

**추론만 할 경우 필요한 것은 `*.py` + `checkpoints/sound_cls.pt` 뿐입니다.** `data/` 는 학습용입니다.

---

## 3. 실행 순서

```bash
# 0) 가상환경 + 설치
python -m venv .venv
.venv\Scripts\activate                  # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt

# GPU 없는 PC 는 CPU 전용 torch 가 훨씬 가볍습니다 (약 200MB vs 2.5GB)
pip install torch==2.9.1 torchaudio==2.9.1 --index-url https://download.pytorch.org/whl/cpu

# 1) 공개 데이터셋 내려받기 (ESC-50, 약 600MB)
python prepare_data.py --download-esc50

#    LibriSpeech dev-clean 은 수동 — DATASET.md 4절 참고
#    data/raw/fall/ 은 제출 패키지에 포함되어 있음

# 2) 데이터셋 구성 (1초 조각 + manifest.csv)
python prepare_data.py --max-per-class 2000

# 3) 학습 (CPU 25~45분)
python train.py --epochs 25 --bs 64

# 4) 실시간 테스트
python inference.py                      # 마이크 + STT
python inference.py --no-stt --verbose   # 분류만, 매 윈도우 판정 출력
python inference.py --file test.wav      # 파일로 테스트

# 5) 실시간성 측정
python benchmark.py

# 6) 통합용 API 데모
python audio_module.py
```

학습을 다시 하지 않고 **추론만** 할 경우 0) → 4) 로 바로 갑니다.

---

## 4. 데이터셋

출처 · 라이선스 · 분할 규칙 전체는 **[DATASET.md](./DATASET.md)** 에 있습니다. 요약만 적습니다.

| 클래스 | 설명 | 출처 | 1초 조각 |
|---|---|---|---|
| `speech` | 사람의 대화 소리 | LibriSpeech dev-clean (CC BY 4.0) | 2,000 |
| `fall` | 낙상 · 충격음 | Pixabay 효과음 65 + 직접녹음 3 + ESC-50 3종 | 829 |
| `ambient` | 일상 생활 소음 | ESC-50 18종 (청소기, 세탁기, 물소리, 발소리 등) | 2,000 |

- 전처리: 16kHz 모노 → 피크 정규화 → 1초 조각(0.5초 간격), 무음 조각 제외
- 클래스당 상한 2,000조각 (한 클래스가 압도하면 모델이 그쪽으로만 찍습니다)

### 분할 규칙
같은 원본 파일에서 나온 조각이 train/test 에 섞이지 않도록 **원본 파일 단위로** 70 / 15 / 15 층화 분할합니다.
(섞이면 같은 소리가 양쪽에 들어가 정확도가 실제보다 높게 나옵니다 — 데이터 누수.)
난수 시드가 고정되어 있어 같은 입력이면 같은 분할이 재현됩니다.

---

## 5. 모델

| 항목 | 값 |
|---|---|
| 입력 | 16kHz 모노 1초 파형 |
| 특징 | log-Mel 스펙트로그램 (64 mel, n_fft 512, hop 160) + 샘플 단위 정규화 |
| 구조 | Conv-BN-ReLU ×2 + MaxPool 블록 4개 (16→32→64→128) → GAP → FC |
| 파라미터 | 294,099 (CPU 실시간 추론 가능) |
| 손실 | CrossEntropy + 클래스 가중치 + label smoothing 0.05 |
| 증강 | 시간 이동, 음량 변화(0.5~1.4배), 가우시안 잡음 |

**클래스 가중치**를 쓰는 이유: `fall` 샘플이 가장 적어서, 보정하지 않으면 모델이 전부 `ambient` 로 찍고도 정확도가 높게 나옵니다.

---

## 6. 평가 지표

`outputs/report.txt` 에 자동 저장됩니다.

- 클래스별 정밀도 / 재현율 / F1
- 혼동행렬
- **낙상(fall) 재현율** — 이 프로젝트에서 가장 중요한 지표. 놓친 낙상은 제품의 존재 이유를 부정합니다.

### 실시간성

`python benchmark.py` → `outputs/benchmark.txt` 에 저장됩니다.

| 항목 | 의미 |
|---|---|
| 1회 추론 지연 (평균 / p95 / 최악) | 0.5초마다 한 번 돌리므로 **예산 500ms** |
| 예산 사용률 | 낮을수록 다른 모듈(비전·TTS)에 CPU를 양보할 여유가 큼 |
| STT RTF | 1 미만이면 발화 길이보다 짧은 시간에 텍스트가 나옴 |

사용자가 느끼는 응답 지연 = **1.0초(발화 종료 판정 대기) + STT 시간**.
낙상 이벤트는 대기 없이 추론 지연만큼(수 ms)에 바로 나갑니다.

---

## 7. 다른 모듈과 연결하는 방법

상세 규격은 **[INTEGRATION.md](./INTEGRATION.md)** 에 있습니다. 여기서는 최소 예시만.

```python
from audio_module import AudioSensor

def on_event(ev):
    if ev["type"] == "fall":
        print("낙상 의심", ev["confidence"])      # 포즈 결과와 교차검증
    elif ev["type"] == "speech":
        print("사용자 발화:", ev["text"])         # 대화 로직으로 전달

with AudioSensor(on_event=on_event):
    run_app()
```

이벤트 스키마

```json
{"type": "fall",   "confidence": 0.93, "time": 12.5, "probs": {"speech":0.02,"fall":0.93,"ambient":0.05}}
{"type": "speech", "text": "오늘 날씨가 춥네", "duration": 2.1, "time": 15.2}
```

CLI 로 쓸 경우 `inference.py` 가 같은 내용을 **한 줄 JSON** 으로 표준출력에 내보냅니다.
다른 언어(C/C++, JS)에서 붙일 때는 이 방식이 편합니다.

---

## 8. 알려진 한계 (보고서에 명시)

1. **학습한 낙상음은 실제 사람의 낙상이 아닙니다.** 효과음 데이터셋의 신체 낙하음·충격음으로 학습했습니다. 실제 노인의 낙상 음향 공개 데이터셋은 존재하지 않습니다. 따라서 `outputs/report.txt` 의 정확도는 **실제 환경 성능의 상한**으로 봐야 합니다.
2. **STT 는 사전학습 모델(faster-whisper)을 그대로 씁니다.** 노인 음성 파인튜닝은 후속 과제입니다. 제출 기한 내에 노인 음성 데이터 확보 + 학습을 끝낼 수 없다고 판단해 범위에서 제외했습니다.
3. **학습 데이터에 한국어 대화 음성이 없습니다.** speech 클래스는 영어 낭독 음성으로 학습했습니다. "말소리인지 아닌지"는 언어 의존도가 낮지만, 한국어·노인 음성으로 보강하면 더 정확해집니다.
4. `fall` 클래스는 정확히는 **"충격성 소음(impact)"** 입니다. 문 닫힘·물건 낙하가 같은 클래스에 들어 있습니다. 소리 단계에서 이를 구분하려 들면 둘 다 놓치므로, **최종 낙상 판정은 포즈 인식과의 교차검증 + 음성 확인에 맡기는 설계**입니다.
5. 소음이 큰 환경(TV 크게 켜둔 상태, 여러 사람 대화)의 성능은 별도 검증이 필요합니다.

판단 근거는 [DATASET.md](./DATASET.md) 3절에 적었습니다.
