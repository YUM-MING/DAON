# DAON 통합 현황 (2026-10-18, 4차)

> 실폰 테스트 후 진단·튜닝 방법은 **[TUNING_GUIDE.md](TUNING_GUIDE.md)** (음성 인식률 / 표정 / 낙상 오탐 / 입출력 통일).
> 개발 환경은 **Windows 기준** (`run.bat`). 웹·비전은 통합자(이유민)가 직접 수정, 음성·대화는 담당자에게 요청.

## 0. 담당자별 할 일 한눈에 (2026-10-18)

### 한정연 (음성) — 우선순위 순
1. **무음/거의 무음 창을 모듈 안에서 걸러 달라.** 값이 전부 0인 1초 창을 분류기가 "낙상·충격음 0.886" 으로 판정한다(학습 데이터에 무음이 없어 OOD). 서버가 임시로 rms<−62dBFS 게이트를 넣었지만 모듈이 하는 게 맞다. 근거 녹음: `logs/home_2/mic_20261018_*.wav`.
2. 거의 무음 창이 "대화" 로 분류돼 whisper 에 들어가면 환각 문장("대대 목욕수수")이 나온다 → STT 전에 발화 구간의 평균 음량 하한을 두기.
3. 폰 녹음 2개로 whisper `base` vs `small`, `beam_size` 1 vs 3 비교. 클리핑 입력 피크 정규화 검토.
4. `speech_threshold`, `SILENCE_SEC`, 최소 발화 길이, `stt_model`, `beam_size` 를 `AudioSensor`/`AudioRouter` 인자로 노출 (서버 `config.yaml` 에서 조정).
5. 폰 PCM 을 직접 넣는 공개 API `AudioSensor.push_pcm(int16_bytes)` + 이벤트 `ts`(epoch).
6. 한국어 발화로 speech 클래스 보강 학습 (가장 효과 큼, 시간 걸림).

### 채혜원 (대화·융합) — 우선순위 순
1. **비상(ESCALATED) 이 끝나지 않는다.** 재알림 상한(2회) 뒤에도 20초마다 안심 멘트가 무한 반복. 상한 뒤 N분 지나면 자동 종료하거나, 보호자 해제 공개 API `engine.resolve_external(reason, now)` 를 추가 (서버는 지금 `_end_safety()` 를 직접 호출).
2. 의미 없는 STT 문자열("대대 목욕수수")을 발화로 받아 CLARIFY 로 가는 대신, 한글 음절 비율·길이 기준으로 `voice_unclear` 취급.
3. 표정 `emotion_conf` 가 0.6 미만이면 공감 전략을 바로 꺼내지 말고 한 번 더 관찰 (표정 오인식이 대화로 번지는 것 방지).
4. 카메라만 낙상일 때 20초(MEDIUM 10초×2) → 15초 목표면 `response_timeout`/`max_asks` 조정 또는 포즈 단독 HIGH 검토. 비전이 이제 `motion` 을 주므로 레이더 없이도 still/moving 이 들어온다.
5. 보호자 확인 후 발화 문장을 `responses.py` 에서 생성. `pose_pred/audio_pred` 형식을 직접 받는 입력 어댑터 (TUNING_GUIDE 4절).

### 라벨 표준 (2026-10-19 공유) — 전원
- **[LABELS.md](LABELS.md) / `labels.json` 이 라벨·필드 이름의 기준.** 담당자별 할 일은 LABELS.md 5절(정연)·6절(혜원)·7절(웹). 반대 의견 없으면 v1.0 으로 확정.
- 혜원: `ALIASES["emotion"]` 에 calm→neutral, anxious→fear, hurt→sad, tired→neutral 추가 (4줄). 장기적으로 tired 정식 값.
- 정연: `audio_pred{pred,conf,detail.label}` 0.5초 주기, `ts`(epoch 초), 충격음 클래스 이름 `impact`, 무음 `silence`.
- 웹: `state.mood`(8종) 가 같이 옴 → `EMOTION_LABEL` 8종 확장 (ws-client 에 임시 배지 표 `MOOD_BADGE` 있음).

### 이유민 (비전·웹·통합) — 진행 중 (2026-10-20)
- 완료(로컬, 미푸시): 표정 7종 재학습(60+ 983장 0.796, 무표정→슬픔 45→4/156), 기분(mood) 집계층 `modules/vision/src/mood.py`,
  서버 워커·조정자·대시보드에 mood 연결, 폰 프레임 녹화(`config.yaml vision.record`) → `logs/home_N/cam_*.mp4`.
- 다음: 낙상 촬영(프로토콜 `vision_dev/docs/fall_recording_protocol.md`) → `scripts/eval_fall.py --sweep` 로 phone 프로파일 재보정 → 비전 일괄 푸시.
- 완료: 폰 프로파일(`phone_config`), 하반신 가시성 게이트, 카메라 기반 `motion` 이벤트(`FallDetector.motion_state`), 낙상 스냅샷, 웹 목업 제거·실데이터 연결.
- 다음: 실폰 스냅샷(`logs/home_N/fall_*.jpg`)으로 폰 프로파일 재보정, 폰 전면 카메라 각도에서 표정 얼굴 크기 확보(전송 해상도 480x360 검토), 시나리오별 데이터 수집(각 10회)으로 카메라 단독 vs 융합 그래프.


통합자 작업 기록. 규약과 전체 설계는 `TEAM_INTEGRATION_GUIDE.md`, 서버 구조는 `server/main.py` 머리 주석 참고.

## 1. 지금 구조 (기획서 하드웨어 설계 그대로)

```
[로봇 폰 브라우저]  카메라 JPEG(8fps) + 마이크 PCM(16kHz) ──WebSocket──▶ [발표 PC 서버 (FastAPI)]
   얼굴 UI · 스피커(TTS)      ◀── state(표정·문장·카운트다운) ──          ├ 비전 워커 (유민) : 포즈·낙상·표정
                                                                        ├ 음성 워커 (정연) : 낙상음 분류 + whisper STT
[보호자 브라우저 (어디서든)] ◀──WebSocket── alert/device/notice ──        ├ 대화·융합 (혜원) : 집마다 엔진 하나
   로그인 · 집 코드 · 대시보드 · 리포트                                    ├ 레이더 워커 (PC USB, 나중에)
                                                                        └ 계정·집·알림 DB (SQLite)
                     ▲ https 공개 주소 = Cloudflare 터널 (run.bat --tunnel)
```

- **계정**: 보호자 이메일+비밀번호 회원가입 → "집"이 생기고 6자리 코드 발급. 로봇 폰은 코드만 입력(`pair.html`). 어르신은 로그인 없음.
- **데모 계정**: `demo@daon.local / daon1234`, 집 코드 `123456` (config.yaml `demo`). 로그인 화면의 "데모 계정으로 로그인" 버튼 한 번이면 끝. 대시보드에 개발자 도구 카드(가짜 사건 버튼)가 뜬다.
- **집(home) 단위 런타임**: 집마다 이벤트 버스·조정자·대화 엔진이 따로. 비전·음성 워커는 서버에 하나씩이고 이벤트에 `home` 이 붙어 라우팅된다.
- **USB 센서 자리**: 레이더 등은 서버 PC 워커(`server/workers/radar_worker.py`)로 붙이고 `config.yaml radar.home_code` 로 어느 집 센서인지 지정. 새 USB 센서 = 워커 스크립트 하나.

## 2. 실행

```bat
run.bat --tunnel         REM 서버 + https 공개 주소 출력 → 폰·보호자가 어디서든 접속 (폰 카메라는 https 필수)
run.bat                  REM 로컬만 (http://localhost:8000, 같은 PC 브라우저에서 카메라 테스트 가능)
run.bat --mock           REM 센서 워커 없이 가짜 이벤트
python -m pytest server\tests -q      REM 10개 (시나리오 + 계정/DB)
```
1. 터미널에 뜬 `https://….trycloudflare.com` 을 폰과 보호자 PC 에서 연다.
2. 폰: "사용자용" → 집 코드 입력 → 얼굴 화면. 화면을 한 번 터치하면 카메라·마이크 권한 → 스트리밍 시작 (좌하단 `📷 123f · 🎤 456c` 로 전송량 확인).
3. 보호자: "보호자용" → 로그인(또는 데모 계정) → 대시보드. 기기 상태 표의 카메라·마이크가 "정상"이면 폰 스트림이 서버 워커에 들어가는 것.
4. 대화: 폰 마이크에 말하면 서버 whisper 가 글자로 바꾸고 혜원 엔진이 답을 만들어 폰이 말한다. 낙상: 폰 카메라 앞에서 넘어지면 폰이 "괜찮으세요?" → 무응답 시 보호자 팝업.

## 3. 완료 체크리스트

| 항목 | 상태 | 비고 |
|---|---|---|
| 한 번에 기동 | ✅ | `run.bat --tunnel` |
| 보호자·사용자 원격 연결 | ✅ | 로그인/회원가입, 집 코드 페어링, 세션 쿠키, 로봇 토큰 |
| 폰 카메라·마이크 → 서버 판정 | ✅ (시뮬레이션) | 가짜 폰 클라이언트로 JPEG/PCM 전송 → 워커 수신 → 기기 상태 ok. **실제 폰 실기 검증은 미완** |
| 시나리오 A / B / 오탐 해제 | ✅ (테스트+mock) | 포즈+낙상음 12초, 카메라만 20초 |
| 평가 로그·분석 탭 | ✅ | `logs/<집코드>/eval_log.jsonl` |
| 리포트·이력 | ✅ | SQLite: 감정 변화, 대화 턴, 알림/해제 → 리포트 탭 요일별 감정·최근 대화, 이력 탭 알림 기록 |
| 기기 상태 | ✅ | 폰 스트림 3초 내 수신이면 ok, 끊기면 stale, 폰 없으면 off |
| 레이더 | ⏳ | 워커·파서만 작성, 실기기 미검증, 기본 off |

## 4. 모듈 수정 요청 목록 (담당자별)

### 채혜원 (대화·융합)
1. **보호자 측 해제 공개 API** — 대시보드 "확인 완료/오탐/119" 시 `engine._end_safety(now)` 를 직접 호출 중. `engine.resolve_external(reason, now)` 요청.
2. **보호자 확인 후 발화 문장** — 서버 고정 문구 사용 중 (`server/core/coordinator.py GUARDIAN_RESOLVE_TEXT`). `responses.py` 에서 생성해 주면 톤이 맞음.
3. **카메라만 낙상일 때 20초** — MEDIUM(10초×2). 15초 목표면 `response_timeout`/`max_asks` 조정 또는 포즈 단독 HIGH 검토.
4. 서버는 0.25초 창의 센서 이벤트를 한 입력으로 합쳐 넣는다 (따로 넣으면 재질문). README 가정 표에 추가 요청.

### 한정연 (음성)
1. 폰 스트림 모드에서는 `AudioSensor` 대신 `AudioRouter` 를 직접 써서 PCM 을 넣는다 (`server/workers/audio_worker.py HomeAudio`). 공개 API 로 `push_pcm(samples)` 같은 메서드를 `AudioSensor` 에 추가해 주면 서버가 내부 클래스를 안 써도 됨.
2. 이벤트 `time` 이 경과초 → 서버가 수신 시각으로 대체 (`sensor_time` 보존). `ts`(epoch) 추가 요청.
3. whisper 가 폰 압축·에코 처리된 소리를 받는다. 실폰으로 인식률 확인 필요.

### 이유민 (비전)
1. 폰 모드는 `PoseTracker` 공유 + 집마다 `FallDetector`/`EmotionRecognizer` 를 직접 조립한다 (`server/workers/vision_worker.py HomeVision`). `SilverCareVision` 에 "추적기 공유" 옵션이 있으면 서버가 내부 조립을 안 해도 됨.
2. 움직임 유무(키포인트 이동량)를 `motion` 이벤트로 내보내면 레이더 없이도 대화 모듈 `motion` 입력에 쓸 수 있음.
3. 폰 전면 카메라(근거리, 320x240 전송)에서 낙상 임계값 재보정 필요.

### 웹앱 (이제 우리가 수정 — 유진 파트에서 넘겨받음)
- 적용 완료: 서버 모드에서 8초 자동 데모·9초 레이더 끊김 데모 생략, `setState` countdown 옵션, 로봇 연결 해제 버튼(🛠 패널), 헤더 반응형, 실시간 센서 로그 카드.
1. 추가된 파일: `auth.html`, `pair.html`, `js/ws-client.js`, `js/robot-sensors.js`, `js/guardian-ext.js`. `index.html` 의 `selectMode` 두 줄만 수정(페어링/로그인 분기). 나머지 UI 코드는 그대로.
2. `user-face.js` 로드 시 8초 자동 데모가 서버 메시지와 겹침 → `window.DaonWS` 있으면 생략 요청.
3. `guardian.js` 9초 뒤 "레이더 끊김" 데모 타이머 제거 요청.
4. alert 상태의 자체 10초 카운트다운 대신 서버 `countdown` 값을 쓰는 `setState` 옵션 요청 (지금은 ws-client 가 타이머를 끄고 덮어씀).
5. 헤더(집 코드·로그아웃), 개발자 도구 카드, 리포트 채우기를 `guardian-ext.js` 가 DOM 주입으로 하고 있음 → 정식 UI 로 흡수 요청. 기기 상태 표의 행에 id 부여.

## 5. 담당자 확인 목록

| 질문 | 현재 가정 |
|---|---|
| 레이더 드라이버 담당 | 통합자 임시 파서, 실기기 미검증, 기본 off |
| 서버 위치 | 발표 PC + Cloudflare 터널 (주소가 실행마다 바뀜). 고정 주소가 필요하면 Cloudflare 계정 터널 또는 EC2 |
| TTS | 폰 브라우저 SpeechSynthesis(ko-KR). 품질 부족하면 서버 TTS + 오디오 전송 |
| 보호자 외부 알림(문자·카톡) | 혜원 모듈 웹훅 채널 + Zapier. `modules/dialogue/.env` 의 `ALERT_WEBHOOK_URL` |
| 폰 기종·브라우저 | 안드로이드 크롬 기준으로 작성. 아이폰 사파리는 AudioContext 16kHz 미지원 → 48kHz 다운샘플 경로로 동작 |

## 6. 다음 단계

1. **실폰 검증**: 터널 주소로 폰 접속 → 카메라·마이크 권한 → 대시보드 기기 상태 ok → 말하기 → 넘어지기. 안 되는 지점의 터미널 로그와 폰 화면 좌하단 전송량 표시를 보고 조정.
2. **정확도**: 비전 UR Fall F1 0.687 → 폰 근거리 재보정 / 음성 오탐 2/15(의자 끌기) / 대화 키워드 규칙 / 융합 지연(레이더 또는 비전 motion 으로 15초 보장).
3. **레이더**: USB-TTL 로 PC 연결 후 `radar.mode: worker`, 포트 지정.
