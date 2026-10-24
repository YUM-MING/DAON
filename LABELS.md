# DAON 공통 라벨 사전 (v0.1 초안, 2026-10-18)

> 센서 모듈(비전·음성·레이더) → 서버 → 대화·웹 사이에서 쓰는 **모든 라벨과 필드 이름의 기준**.
> 작성: 이유민. 팀 리뷰 후 v1.0으로 확정하고, 그 뒤 바뀔 때는 버전을 올리고 아래 "변경 이력"에 적는다.
> 기계가 읽는 사본은 `labels.json` (같은 내용, 테스트에서 참조).

## 0. 왜 만드나

지금은 모듈마다 라벨·필드·시각 단위가 달라 서버 어댑터가 전부 번역한다 (`server/adapters/*.py`).
그 과정에서 **불안(fear) 표정은 비전이 surprised로 합치고 대화 모듈은 surprised를 neutral로 받아 사라지고**,
비전의 `uncertain` 판정은 대화에 전달되지 않으며, 음성 이벤트 시각은 경과초라 서버가 수신 시각으로 바꿔 끼운다.
이 문서대로 맞추면 어댑터 번역이 없어지고, 같은 영상·녹음으로 모듈 단독 vs 융합 정확도를 한 스크립트로 비교할 수 있다.

## 1. 공통 규칙 (모든 이벤트)

```json
{"type": "<이벤트 이름>", "ts": 1760000000.12, "home": 1, "pred": "fall", "conf": 0.83, "detail": {...}}
```

| 필드 | 규칙 |
|---|---|
| `type` | 소문자 snake_case. 센서 판정은 `<모듈>_pred`, 그 외는 아래 표 |
| `ts` | **unix epoch 초 (float)**. 경과초·밀리초·ISO 문자열 금지. 모듈 내부 경과초가 필요하면 `sensor_time`에 따로 |
| `home` | 집 ID (서버가 붙임. 모듈 단독 실행 시 생략 가능) |
| `pred` | 낙상 판정 3값 고정: `fall` / `no_fall` / `uncertain`. `uncertain` = "이 센서만으로는 모름, 다른 센서가 결정" |
| `conf` | 0~1 float. 이름은 항상 `conf` (`confidence`, `pose_conf` 등 금지). 보정된 확률일 필요는 없고 높을수록 확신 |
| `detail` | 모듈별 부가 정보 (아래 표). 소비자는 모르는 키를 무시한다 |
| 주기 | 판정(`*_pred`)은 **0.5초마다** 이벤트 유무와 무관하게 발행 (타임라인이 끊기지 않게). 변화 이벤트는 변화 시 1회 |
| 라벨 표기 | 소문자 영문 snake_case. 한글·대문자·하이픈 금지 (대화 모듈의 ALIASES는 안전망일 뿐, 기준이 아님) |

## 2. 포즈·낙상 (비전, 이유민)

### `pose_pred` — 0.5초마다

| 필드 | 값 | 설명 |
|---|---|---|
| `pred` | `fall` / `no_fall` / `uncertain` | 카메라 단독 낙상 판정 |
| `conf` | 0~1 | |
| `detail.present` | bool | 사람이 보이는지 |
| `detail.posture` | `standing` / `sitting` / `lying` / `unknown` | `unknown` = 자세를 판단할 근거 부족 (하반신 안 보임 등) |
| `detail.visibility` | `full` / `upper` / `head` / `none` | 보이는 신체 범위. `upper` = 상반신만(탁상 로봇 기본 상태), `head` = 얼굴만(너무 가까움) |
| `detail.distance` | `near` / `mid` / `far` / `unknown` | 어깨 폭·얼굴 크기로 추정한 거리대 (near ≈ 1m 이내) |
| `detail.motion` | `moving` / `still` / `unknown` | 최근 1초 키포인트 이동 유무 |
| `detail.person_conf` | 0~1 | 사람 검출 신뢰도 |
| `detail.fall_state` | `monitoring` / `fallen` / `alerted` | 상태머신 (디버그·UI용) |

`person_status`(2초 주기)와 `motion`(1초 주기) 이벤트는 **이 한 줄로 흡수**한다. 이행 기간에는 둘 다 내보내고, 소비자가 다 옮기면 제거.

### 낙상 생애주기 이벤트 — 변화 시 1회

| `type` | 필드 | 뜻 |
|---|---|---|
| `fall_detected` | `conf`, `reason`(선택) | 카메라가 낙상을 확정 (급락 + 누움 지속, conf 0.9). 로봇 1차 음성 확인 트리거. `reason: drop_then_lost` 면 급락 직후 사람이 화면에서 사라져 3초 이상 안 보인 경우(conf 0.6, 폰 프로파일) |
| `fall_recovered` | — | 기립 또는 응답으로 해제 |
| `fall_alert` | `elapsed_sec` | 낙상 후 무움직임 10초 → 보호자 알림 |

### 대화 모듈 입력으로의 매핑 (서버 또는 혜원 모듈 `from_sensor_events()`)

| `pose_pred` | 대화 `pose` |
|---|---|
| `pred=fall` | `fall` |
| `pred=no_fall`, `posture=lying` | `lying` |
| `pred=no_fall`, `posture=standing/sitting` | `normal` |
| `pred=no_fall`, `posture=unknown` | 보내지 않음 (자세 정보 없음) |
| `pred=uncertain` | 보내지 않음 — 단, `detail.motion`은 `motion` 입력으로 보냄 |

## 3. 움직임 (레이더 또는 비전)

### `motion_pred` — 0.5초마다 (레이더 있으면 레이더, 없으면 비전)

| 필드 | 값 |
|---|---|
| `pred` | 낙상 판정 관점: 레이더는 항상 `uncertain` (단독으로 낙상을 말하지 않음) |
| `detail.motion` | `moving` / `still` / `none` (`none` = 사람 없음) |
| `detail.source` | `radar` / `vision` |
| `detail.distance_cm` | 레이더만 |

대화 입력 `motion` 값은 `detail.motion` 그대로. 레이더와 비전이 동시에 있으면 **레이더 우선** (최근 5초 내 값이 있을 때).

## 4. 표정·기분 (비전, 이유민) — 2층 구조

핵심 변경: 모델이 내는 **순간 표정(expression)** 과 팀에 넘기는 **기분(mood)** 을 분리한다.
대화·웹은 `mood_pred`만 쓴다. 한 장 오인식이 바로 로봇 발화로 이어지는 것을 막기 위해서다.

### 4.1 `expression` — 모델 원본 라벨 (1층, 학습 클래스)

AIHub "한국인 감정인식을 위한 복합 영상"의 7개 라벨을 그대로 쓴다. 지난 5종 파인튜닝에서 상처·불안을 슬픔에 합친 것이
"슬픔"을 넓게 학습시켜 무표정까지 끌어당긴 원인 중 하나로 본다.

| `expression` | AIHub 원 라벨 | 비고 |
|---|---|---|
| `happy` | 기쁨 | |
| `neutral` | 중립 | **무표정. 노인 기본 얼굴이 여기 와야 한다** |
| `sad` | 슬픔 | |
| `angry` | 분노 | |
| `anxious` | 불안 | 대화 모듈의 `fear`에 해당 — 지금은 사라지는 신호 |
| `surprised` | 당황 | |
| `hurt` | 상처 | 2층에서 `sad`로 합침 |

발행 이벤트: `expression_debug` (every_n_frames마다, 디버그·학습 데이터 수집용). `detail`: `raw`(위 7종), `scores`(7개 확률), `face_px`, `aligned`(정렬 여부).

### 4.2 `mood_pred` — 팀 전송 라벨 (2층) — 1초마다

최근 **30초 창**의 expression 분포 + 눈 감김·고개 숙임 규칙으로 계산한다.

| `pred` (mood) | 기준 | 대화 `emotion` | 웹 `emotion` |
|---|---|---|---|
| `happy` | happy 비율 우세 | `happy` | `happy` (얼굴이 같이 웃음) |
| `calm` | neutral 우세 또는 어느 것도 우세하지 않음. **기본값** | `neutral` | `neutral` |
| `sad` | sad+hurt 비율 ≥ 0.5 **이고** 평균 conf ≥ 0.6 **이고** 3초 이상 지속. **hurt는 conf ≥ 0.7일 때만 sad로 세고 아니면 neutral** (7종 평가에서 무표정 23장 중 4장이 hurt로 감) | `sad` | `neutral` + 배지 "슬픔(감지)" |
| `anxious` | anxious 우세, 같은 조건 | `fear` | `neutral` + 배지 |
| `angry` | angry 우세, 같은 조건 | `angry` | `neutral` + 배지 |
| `surprised` | 최근 5초 창(표본 3개 이상)에서 비율 ≥ 0.5 이고 conf ≥ 0.6 | `neutral` | `surprised` |
| `tired` | 눈 감김 비율(30초 창) ≥ 0.3 또는 고개 숙임 지속 — **분류기가 아니라 규칙** | `neutral` (혜원 모듈에 `tired` 추가 요청) | `neutral` + 배지 "피곤(감지)" |
| `unknown` | 얼굴이 없거나 너무 작음 | 보내지 않음 | 이전 값 유지 |

| 필드 | 값 | 설명 |
|---|---|---|
| `pred` | 위 8종 | |
| `conf` | 0~1 | 창 안에서 해당 라벨 비율 × 평균 모델 확신 |
| `detail.intensity` | `low` / `mid` / `high` | 평균 conf 구간 (0.6 미만 / 0.6~0.8 / 0.8 이상) |
| `detail.stability` | 0~1 | 창 안에서 같은 라벨이 차지한 비율 |
| `detail.baseline_delta` | 0~1 | 개인 기준선(첫 2~3분의 평소 얼굴 분포) 대비 변화량. 기준선 없으면 null |
| `detail.face_px` | int | 얼굴 크기(px). 64 미만이면 신뢰 낮음 |
| `detail.top_expression` | 7종 | 창에서 가장 많이 나온 1층 라벨 |

**원칙**: 부정 기분(sad/anxious/angry)은 세 조건(비율·확신·지속)을 다 넘겨야 내보낸다. 넘기지 못하면 `calm`. 무표정이 슬픔으로 새는 문제는 모델과 이 게이트 두 겹으로 막는다.

변화 이벤트 `mood_changed`(라벨이 바뀔 때 1회, 같은 필드)도 함께 발행한다. 지금의 `emotion` 이벤트를 대체한다.

## 5. 소리 (음성, 한정연)

### `audio_pred` — 0.5초마다

| 필드 | 값 | 설명 |
|---|---|---|
| `pred` | `fall` / `no_fall` / `uncertain` | `fall` = 충격음 임계값(0.6) 초과. 모듈 안의 5초 디바운스는 유지 |
| `conf` | 0~1 | P(impact) |
| `detail.label` | `speech` / `impact` / `ambient` / `silence` | 분류 라벨. 기존 `fall` 클래스는 실제로 충격음이므로 **`impact`로 이름 변경**. `silence` = 무음 게이트에 걸림(분류 안 함) |
| `detail.p_speech`, `detail.p_impact`, `detail.p_ambient` | 0~1 | 세 클래스 확률 |
| `detail.rms_dbfs` | float | 창의 음량 (디버그) |

### 발화 이벤트 — 발화 종료 후 1회

| `type` | 필드 | 설명 |
|---|---|---|
| `speech` | `text`, `duration`, `ts`(발화 **끝** 시각), `detail.stt_model`, `detail.lang_ratio`(한글 음절 비율) | 빈 문자열이면 내보내지 않음 |
| `voice_unclear` | `duration`, `ts` | 분류기는 말소리라 했는데 STT가 비었거나 `lang_ratio` < 0.5 |

대화 입력 매핑: `audio_pred.pred=fall` → `sound_event=fall_sound`, `sound_conf=conf`; `speech` → `stt=text, sound_event=speech`; `voice_unclear` → `sound_event=voice_unclear`; `label=ambient/silence` → 보내지 않음.

## 6. 대화·융합 (채혜원) — 입력 필드는 그대로, 값만 이 사전으로

| 대화 입력 | 허용 값 (변경 없음) | 이 사전에서 오는 값 → 추가할 별칭 |
|---|---|---|
| `pose` | `normal` `lying` `sleeping` `fall` | `standing`·`sitting` → `normal` (이미 있음), `unknown` → 무시 |
| `emotion` | `neutral` `happy` `sad` `angry` `fear` | **`calm` → `neutral`, `anxious` → `fear`, `hurt` → `sad`, `tired` → `neutral`(임시)**, `surprised` → `neutral`(이미 있음) |
| `sound_event` | `none` `speech` `fall_sound` `daily` `voice_unclear` | `impact` → `fall_sound`(이미 있음), `ambient` → `daily`, `silence` → `none` |
| `motion` | `moving` `still` `none` | 그대로 |
| `*_conf` | 0~1 | `conf` 하나로 통일되므로 어댑터가 `pose_conf` 등으로 이름 붙임 |

요청 사항: (1) 위 별칭 4개를 `ALIASES["emotion"]`에 추가, (2) 장기적으로 `tired`를 정식 값으로 (이미 "피곤해" 발화 응답이 있으므로 표정 기반 `tired`도 같은 응답으로), (3) `from_sensor_events(list[dict])` 입력 어댑터 — 이 사전의 이벤트 목록을 직접 받아 한 건의 `DialogueInput`으로 합침.

### 대화 출력 (변경 없음, 참고)

`DialogueOutput.to_dict()` 그대로. 로봇 UI 상태 매핑은 서버 조정자가 담당: `strategy` → `state`(`sleep/wake/listening/thinking/speaking/alert/recover`).

## 7. 웹 (로봇 얼굴·보호자)

`state` 메시지의 `emotion` 필드는 **mood 8종**을 그대로 받는다. 얼굴 반응 정책은 웹이 가진다 (`ws-client.js MIRROR`):

| mood | 얼굴 | 배지 |
|---|---|---|
| `happy` | 같이 웃음 | 기쁨 |
| `calm` | 상태 기본 눈 | 평온 |
| `surprised` | 반짝임 1회 | 놀람 |
| `sad` / `anxious` / `angry` | **바꾸지 않음** (우울한 어르신에게 우울한 얼굴을 비추지 않음) | 슬픔(감지) / 불안(감지) / 화남(감지) |
| `tired` | 바꾸지 않음 | 피곤(감지) |
| `unknown` | 이전 유지 | — |

`EMOTION_LABEL`을 8종으로 확장해야 한다 (지금 4종).

## 8. 평가 로그 (분석 탭, 한 줄 JSON)

```json
{"ts": 1760000003.5, "scenario_id": "fall-front-01", "ground_truth": "fall",
 "pose":   {"pred": "fall",      "conf": 0.90, "posture": "lying", "visibility": "full"},
 "audio":  {"pred": "fall",      "conf": 0.81, "label": "impact"},
 "motion": {"pred": "uncertain", "conf": 1.0,  "motion": "still", "source": "vision"},
 "mood":   {"pred": "calm",      "conf": 0.7},
 "fused":  {"pred": "fall",      "conf": 0.93, "risk_score": 85, "risk_level": "HIGH"}}
```

- `ground_truth`: `fall` / `no_fall`. 낙상 영상은 `gt_fall_start`, `gt_fall_end`(초)를 추가.
- `scenario_id` 규칙: `<종류>-<세부>-<회차>` 예) `fall-front-03`, `nofall-lie-slow-02`, `nofall-pickup-01`.
- 같은 형식을 모듈 단독 평가 스크립트와 서버 실시간 로그가 모두 쓴다 → "카메라 단독 / 소리 단독 / 융합" Recall·Precision을 한 스크립트로.

## 9. 이행 순서

1. 이 문서 v1.0 확정 (팀 리뷰 1일, 반대 없으면 확정).
2. **서버 어댑터가 먼저** 이 사전으로 변환해 흘려보낸다 (모듈은 그대로 → 당장 동작 유지).
3. 각 모듈이 자기 일정에 맞춰 네이티브 출력으로 전환 (비전: `pose_pred.detail`·`mood_pred`, 음성: `audio_pred`·`ts`·`impact`, 대화: 별칭 4개).
4. 모듈 전환이 끝나면 어댑터 변환 코드를 지운다. `labels.json`을 읽는 테스트 하나로 각 모듈 출력이 사전 안의 값인지 검사.

## 10. 변경 이력

| 버전 | 날짜 | 내용 |
|---|---|---|
| v0.1 | 2026-10-18 | 초안 (이유민). 표정 2층 구조, `uncertain` 전달, `ts` 통일, `impact` 명명, `visibility`/`distance` 추가 |
| v0.2 | 2026-10-19 | mood 층 구현 반영: hurt 보수 처리(conf ≥ 0.7), surprised 표본·확신 조건, 기준선 초과 규칙(`baseline_excess_min` 0.25). 구현: `src/mood.py` |
| v0.3 | 2026-10-24 | `fall_detected.reason` 추가(`drop_then_lost`, conf 0.6). 폰 프로파일 낙상 임계값 재보정 (공개 데이터 3종 F1 0.78). 구현: `src/fall_detector.py`, `src/config.py phone_config()` |
