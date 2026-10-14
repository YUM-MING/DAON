# DAON 진단·튜닝 가이드 (2026-10-14)

실폰 테스트에서 나온 세 문제 — **음성 인식률 낮음 / 표정 오인식(웃는데 우울) / 가만히 있어도 낙상** — 를
어디서 보고, 무엇을 바꾸고, 누구에게 넘길지 정리한 문서.

## 0. 먼저: 로그 보는 법 (이게 핵심)

서버를 켜고 보호자 대시보드(데모 계정)에 들어가면 홈 탭 아래에 **📡 실시간 센서 로그** 카드가 있다.
워커(비전·음성)와 대화 엔진이 판단할 때 쓴 **내부 값을 그대로** 보여 준다.

| 줄 | 뜻 | 어디서 나오나 |
|---|---|---|
| `포즈 no_fall(0.9) 자세=standing 상태=monitoring 속도=0.02 종횡비=0.61 각도=12.3 사람conf=0.88 급락=False 누움=0s` | 1초마다 낙상 판정 근거 | 비전 워커 `vision_debug` |
| `표정 happy raw=happy conf=0.81 얼굴=52px ✔안정→전송` | 표정 분류 한 번(10프레임마다) | 비전 워커 `emotion_debug` |
| `소리 speech(0.87) P(fall)=0.02` | 0.5초마다 소리 분류 (대화/낙상/소음) | 음성 워커 `audio_debug` |
| `🗣 STT "오늘 날씨 좋네" (2.1s)` | whisper 결과 | 음성 워커 `speech` |
| `🗣 목소리는 났으나 인식 실패` | 분류기는 말소리라 했는데 whisper 가 빈 문자열 | `voice_unclear` |
| `🤖 EMPATHIZE "…" (이유)` | 대화 엔진이 왜 그 말을 했는지 | 대화 모듈 `reason` |

파일로도 남는다 (`logs/<집코드>/`, 데모 집은 `logs/123456/`, 워커 파일은 `logs/home_<id>/`):
- `logs/home_1/mic_YYYYMMDD_HHMMSS.wav` — 폰 마이크 소리 원본(16kHz). **정연 모듈로 그대로 재현 가능**:
  `cd modules/audio_ai && python inference.py --file ../../logs/home_1/mic_….wav --verbose`
- `logs/home_1/fall_<epoch>.jpg` — 비전이 낙상으로 확정한 순간의 프레임 (오탐이면 이 사진으로 이유를 본다)
- `logs/123456/eval_log.jsonl` — 0.5초마다 pose/audio/radar/fused 판정 (분석 탭)
- `logs/daon.db` — 대화 턴·표정 변화·알림 (리포트)
- 터미널 — 워커 stderr (`[STT] …`, `[vision] …`)

끄려면 `config.yaml` 의 `vision.debug`, `audio.debug`, `audio.record` 를 false.

---

## 1. 음성 인식률 — 정연 파트에 넘길 것

소리가 글자가 되기까지 다섯 관문을 지난다. 센서 로그에서 **어느 관문에서 끊기는지** 먼저 본다.

```
폰 마이크 → ① 브라우저 전처리(에코제거·노이즈억제·AGC) → ② 16kHz PCM 전송
  → ③ 분류기 "speech ≥ 0.5" 인 0.5초 창만 모음 → ④ 1초 침묵 후 whisper(base, ko, int8)
  → ⑤ 텍스트 비어 있으면 voice_unclear
```

| 증상 (센서 로그) | 막힌 관문 | 조치 |
|---|---|---|
| 말하는데 `소리 ambient` 로만 찍힘 | ③ 분류기가 한국어 말소리를 '소음'으로 봄 | **정연**: speech 클래스가 LibriSpeech(영어 낭독)로만 학습됨. 폰 녹음 wav 로 한국어 발화를 보강 학습 (DATASET.md 절차) 또는 `speech_threshold` 0.5→0.35 (`config.yaml` 로 빼 두면 좋음) |
| `소리 speech` 는 찍히는데 STT 줄이 안 옴 | ④ 발화가 0.4초 미만이거나 침묵 1초를 못 채움(계속 소음) | **정연**: `SILENCE_SEC` 1.0→0.7, 짧은 발화 기준 0.4→0.3 검토. 우리: 조용한 곳에서 테스트 |
| `목소리는 났으나 인식 실패` 가 자주 | ④ whisper 가 못 알아들음 | **정연**: `stt_model` base→small (정확도↑, 지연 +1.6초), `beam_size` 1→3, `vad_filter` 끄고 비교. 폰 wav 로 base/small 직접 비교해 주면 됨 |
| STT 결과가 엉뚱한 문장 | ①②④ 소리 품질 | 우리: `web/js/robot-sensors.js` 의 `echoCancellation/noiseSuppression/autoGainControl` 을 끄고 비교 (AGC 가 말을 눌러 버리는 경우 있음). wav 를 들어 보면 바로 안다 |
| 로봇이 말한 직후 내 말이 사라짐 | 에코 방지 음소거 | 설계상 TTS 중엔 발화를 버림. TTS 끝난 뒤 0.5초 기다렸다 말하기. 필요하면 `coordinator._finish_speaking` 뒤 유예 시간 추가 |

**정연 파트 전달 목록 (요약)**
1. 폰 녹음 wav (`logs/home_1/mic_*.wav`) 몇 개를 보내고, `inference.py --file` 로 돌렸을 때 분류 라벨·STT 결과를 확인해 달라. 어느 관문이 문제인지 같이 본다.
2. 한국어 발화로 speech 클래스 보강 학습 (가장 효과 큼).
3. `speech_threshold`, `SILENCE_SEC`, 최소 발화 길이, `stt_model`, `beam_size` 를 `AudioSensor`/`AudioRouter` 인자로 노출 → 우리가 `config.yaml` 에서 조정.
4. 폰 PCM 을 직접 넣는 공개 API `AudioSensor.push_pcm(int16_bytes)` (지금은 서버가 내부 `AudioRouter` 를 직접 씀).
5. 이벤트에 `ts`(epoch) 추가.

---

## 2. 표정 — 지금 매핑과 확인 방법

**흐름**: 폰 프레임 → YOLO 포즈 → 코·눈 키포인트로 얼굴 박스 → 파인튜닝 ONNX(5종: angry/happy/neutral/sad/surprised) →
같은 라벨 2회 연속이면 `emotion` 이벤트 → 서버 → (a) 대화 엔진 입력(공감 전략 결정) (b) 로봇 얼굴 `state.emotion`.

**로봇 얼굴 정책 (이번에 바꿈, `web/js/ws-client.js MIRROR`)**: 눈 모양은 **상태** 가 기준이다.
듣는 중=호기심 눈, 말하는 중=호기심↔반짝임, 긴급=긴급 눈, 대기=졸린 눈. 감지된 어르신 감정은 **happy 일 때만 같이 웃고**,
sad/angry 는 얼굴에 비추지 않고 배지에만 `슬픔(감지)` 로 표시한다 (우울한 어르신에게 우울한 얼굴을 보여 주지 않기 위해).
다시 거울처럼 따라 하게 하려면 `MIRROR` 에 `sad:"sad", angry:"angry"` 를 넣으면 된다.

**"웃고 있는데 우울하다고 함" 이 카메라 문제인지 대화 문제인지 가르는 법**
1. 센서 로그에서 `표정 … raw= conf= 얼굴=NNpx` 줄을 본다.
   - `얼굴=` 이 40~60px 이면 320x240 전송 프레임에서 얼굴이 너무 작다 → 모델이 못 본다. 폰을 더 가까이, 또는 `robot-sensors.js` 의 canvas 를 480x360 으로.
   - raw 가 `sad` 로 찍히고 conf 가 0.5 근처면 모델 오인식 → **비전(우리)**: `modules/vision/src/config.py EmotionConfig.stable_count` 2→3 (더 오래 유지돼야 전송), 또는 전송 시 conf 0.6 미만은 neutral 로 (아래 코드).
   - raw 가 `happy` 인데 로봇이 우울하다고 말하면 → **대화(혜원)**: 발화 텍스트 키워드로 판단한 것. `🤖 … (이유)` 줄에 근거가 적힌다.
2. 노인 60+ 평가에서 happy 0.87 / sad 0.95 / neutral 0.65 였다. **무표정(neutral)이 sad 로 새는 경우가 가장 흔하다.**

**우리가 조정할 코드 (비전)**
```python
# modules/vision/src/config.py
class EmotionConfig:
    every_n_frames = 10     # 10 → 5 로 줄이면 더 자주 판정 (CPU 부하↑)
    min_face_size = 40      # 얼굴 최소 px. 폰 근거리면 60 으로 올려 작은 얼굴은 판정 안 함
    stable_count = 2        # 2 → 3: 같은 표정이 3번 연속이어야 전송 (깜빡임·오인식 완화)
# modules/vision/src/emotion_recognizer.py process() 마지막 줄 근처: conf 낮으면 neutral 로
#   if conf < 0.6: label = "neutral"
```

---

## 3. 낙상 오탐 — 왜 가만히 있는데 넘어졌다고 하나

낙상 확정 조건은 **급락 + 누움 + 지속** 세 가지가 모두 맞아야 한다 (`modules/vision/src/fall_detector.py`).
센서 로그의 `포즈` 줄에서 어떤 조건이 잘못 켜지는지 본다.

| 로그 값 | 정상 | 오탐일 때 흔한 모습 | 원인 |
|---|---|---|---|
| `종횡비` (bbox 가로/세로) | 서면 0.4~0.7 | **1.1 이상** | 폰이 가로이고 사람이 가까워서 상반신만 보임 → 박스가 납작 → '누움' |
| `각도` (상체 기울기) | 서면 <25 | 55 이상 | 어깨·엉덩이 키포인트가 화면 밖/오검출 |
| `속도` (하강, 키/초) | 0 근처 | **0.45 이상** 순간 튐 | 사람이 화면 밖으로 나갔다 들어오거나 키포인트 지터, `scale_px` 가 작을 때 |
| `사람conf` | 0.7+ | 0.5 근처 | 부분 가림·역광 |
| `급락=True 누움=0.6s` | — | 이 조합 뒤 `상태=fallen` | 위 둘이 겹침 |

**바로 조정할 값 (`modules/vision/src/config.py FallConfig`)** — 근거리 폰 기준 추천 시작값
```python
drop_velocity_threshold = 0.45   # → 0.7  (가까우면 작은 움직임도 큰 속도로 보임)
lying_aspect_ratio      = 1.1    # → 1.4  (상반신만 보일 때 납작한 박스 오판 방지)
lying_torso_angle_deg   = 55.0   # → 65
fallen_confirm_sec      = 0.5    # → 1.5  (누운 자세가 1.5초 이어져야 확정)
min_person_conf         = 0.5    # → 0.6
drop_memory_sec         = 3.0    # → 2.0
```
바꾼 뒤 서버 재시작 없이 워커만 다시 뜨게 하려면 서버를 재시작하는 게 가장 간단하다.

**구조적 보완 (우리 비전 쪽에서 가능)**
- **전신 가시성 게이트**: 발목·무릎 키포인트가 안 보이면(kp_conf<0.3) 낙상 판정을 보류하고 `uncertain` 으로. 상반신만 보이는 폰 각도에서 오탐 대부분이 사라진다.
- **움직임 이벤트 내보내기**: `_update_motion` 의 값을 `motion: moving|still` 로 발행하면 대화 모듈 `motion` 입력에 쓰여 레이더 없이도 15초 안에 판정.
- 낙상 스냅샷(`logs/home_1/fall_*.jpg`)을 모아 두면 "어떤 자세에서 오탐 나는지" 바로 보인다.

---

## 4. 정확도를 위해 통일하면 좋은 입출력 (팀 제안)

지금은 모듈마다 필드 이름·시각·라벨이 달라 서버가 변환한다. 아래처럼 맞추면 변환이 사라지고 평가 로그도 그대로 쓴다.

**모든 센서 이벤트 공통**
```json
{"type": "<모듈>_pred", "ts": 1760000000.12, "pred": "fall|no_fall|uncertain", "conf": 0.0~1.0, "detail": {...}}
```
- `ts` 는 unix epoch 초(float). 경과초·ms 금지.
- `pred` 3값 고정. `uncertain` 은 "이 센서만으론 모름" — 융합에서 다른 센서가 결정.
- `conf` 는 보정된 확률이 아니어도 되지만 0~1, 높을수록 확신.
- 주기: 포즈 0.5초, 소리 0.5초(이벤트 없을 때도 `no_fall` 을 내면 타임라인이 끊기지 않음), 레이더 0.5초.

| 모듈 | 지금 | 통일 후 |
|---|---|---|
| 비전 포즈 | `pose_pred{pred,conf}` + `person_status{posture}` 따로 | `pose_pred{pred,conf,detail:{posture, motion:moving/still, person_conf}}` 한 줄 |
| 비전 표정 | `emotion{label,raw,confidence}` 변화 시만 | `emotion{label,raw,conf,face_px}` + 1초 주기 `emotion_pred` (안정화 전 값) |
| 음성 | `fall{confidence,time}`, `speech{text}`, `voice_unclear` | `audio_pred{pred,conf,ts,detail:{label,p_fall,p_speech}}` 0.5초 + `speech{text,ts,duration}` + `voice_unclear{ts}` |
| 레이더 | 없음 | `radar_pred{pred:uncertain,conf,detail:{motion,distance_cm}}` |
| 대화 입력 | `pose: normal/lying/fall, motion, emotion, sound_event, stt` | 그대로 두되 **`pose_pred/audio_pred` 를 직접 받는 어댑터를 혜원 모듈 안에** (`from_sensor_events()`) — 그러면 서버 변환 없이 평가 재현이 됨 |
| 대화 출력 | `DialogueOutput` | `+ ui_state(sleep/…/alert)` 를 혜원 모듈이 직접 내면 서버 매핑 제거 |

**정확도 비교 그래프(심사 요구)에 필요한 것**: 같은 wav/영상에 대해 각 모듈이 위 형식으로 `eval_log.jsonl` 을 내면
`scripts/eval_fall.py` 식으로 "카메라 단독 / 소리 단독 / 융합" Recall·Precision 을 한 스크립트로 뽑을 수 있다.
녹음·스냅샷이 이제 자동으로 쌓이니, **시나리오별로 폰 앞에서 10번씩 연기한 데이터**를 모으는 게 다음 단계다.

---

## 5. 혜원 파트에 넘길 것 (대화·융합)
1. 보호자 해제 공개 API (`resolve_external`), 보호자 확인 후 발화 문장 생성.
2. `emotion` 이 들어올 때 conf 가 낮으면(0.6 미만) 공감 전략을 바로 꺼내지 말고 한 번 더 관찰 — 표정 오인식이 대화로 번지는 걸 막는다.
3. 카메라만 낙상일 때 20초 → `response_timeout`/`max_asks` 또는 포즈 단독 HIGH 검토.
4. `pose_pred/audio_pred` 형식을 직접 받는 입력 어댑터 (4절).
