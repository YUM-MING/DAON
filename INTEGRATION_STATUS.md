# DAON 통합 현황 (2026-10-14)

통합자 작업 기록. 규약과 전체 설계는 `TEAM_INTEGRATION_GUIDE.md`, 서버 구조는 `server/__init__.py` 참고.

## 1. 완료 판정 체크리스트 (가이드 6절)

| 항목 | 상태 | 비고 |
|---|---|---|
| 4개 모듈 한 번에 기동 | ✅ | `./run.sh` (실제 센서) / `./run.sh --mock` (가짜 센서) |
| 시나리오 A: 표정 → 선제 발화 → 대화 | ✅ (mock) | 표정 `sad` 반영 + 발화 "외롭다" → 공감 응답 → TTS → listening. 실카메라·마이크로 재확인 필요 |
| 시나리오 B: 낙상 → 10초 카운트다운 → 보호자 팝업 15초 이내 | ✅ (mock) | 포즈+낙상음 → HIGH → 6초×2 = **12초**에 `alert_created`. 카메라만이면 MEDIUM → 10초×2 = 20초 |
| 오탐 해제: "괜찮아" → 카운트다운 중지, 평시 복귀, 보호자 알림 없음 | ✅ (mock) | recover → wake → listening, `alert_created` 없음 |
| 평가 로그 jsonl → 분석 탭 | ✅ | `logs/eval_log.jsonl` 0.5초마다 기록, 대시보드 개발자 모드에서 타임라인·정확도 표시 |
| 기기 연결 상태 대시보드 반영 | ✅ | `device_status` 5초마다 (camera/mic/radar: ok·error·stale·mock·off, 폰 online, 배터리) |

(mock) = 가짜 센서 + **실물 대화 모듈**로 검증. 카메라/마이크 실기 검증은 담당자와 함께 진행.

## 2. 실행

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt           # torch 2.14 / numpy 2.5.3 / torchaudio 2.11 로 통일 (음성 모듈 2.9.1 → 상향, 동작 확인)
./run.sh --mock                           # 센서 없이 UI + 대화 모듈
./run.sh                                  # config.yaml 대로 실제 카메라/마이크 워커
```
- 웹앱: `http://<PC IP>:8000/` → 사용자용(로봇 얼굴) / 보호자용. 같은 Wi-Fi의 폰에서 접속.
- 가짜 이벤트: `curl -X POST localhost:8000/mock/scenario/B` (`A`, `false_alarm`), `/mock/fall`, `/mock/speech?text=괜찮아`, `/mock/emotion/sad`, `/mock/radar/still`
- 상태: `GET /api/status`, 이벤트: `GET /api/events`, 평가 로그: `GET /api/eval_log`
- 테스트: `python -m pytest server/tests -q` (8개, 가짜 시계로 시나리오 A/B/오탐/보호자 해제)

## 3. 구조 (한 줄 요약)

```
비전 워커 ─┐                          ┌→ /ws/robot    (state: sleep|wake|listening|thinking|speaking|alert|recover)
음성 워커 ─┼→ 내부 버스 → 대화 어댑터(혜원 엔진) → 조정자 ─┤
레이더 워커┘        ↑                                   └→ /ws/guardian (alert_created / alert_resolved / device_status / notice)
                 폰 tts_done · 보호자 해제/메시지
```
- 비전·음성은 **별도 프로세스**(워커)로 띄운다. 한 프로세스에 넣으면 cv2 와 faster-whisper(`av`)가 서로 다른 libavdevice 를 실어 충돌 경고가 나고, 두 모듈 다 `model.py`/`config.py`/`inference.py` 라는 같은 이름을 쓴다.
- 모듈 코드는 그대로. 서버와의 변환은 `server/adapters/<모듈>.py` 한 파일씩.
- 낙상 판정·카운트다운의 **단일 기준은 대화 모듈**. 비전의 자체 10초 `fall_alert` 는 참고 이벤트로만 받는다 (이중 알림 방지).
- 에코 방지: 로봇이 말하는 동안 음성 워커가 `speech`/`voice_unclear` 만 버리고 낙상 감지는 계속한다.
- TTS: 폰 브라우저 `speechSynthesis`(ko-KR). 재생이 끝나면 `tts_done` 회신, 안 오면 서버가 타임아웃 처리.

## 4. 모듈 수정 요청 목록 (담당자별 — 서버에서 우회 중, 모듈에서 고치면 더 깔끔한 것)

### 채혜원 (대화·융합)
1. **보호자 측 해제 공개 API** — 대시보드 "확인 완료/오탐/119" 시 안전 확인 세션을 끝내야 하는데 공개 메서드가 없어 `engine._end_safety(now)` 를 직접 호출 중. `engine.resolve_external(reason, now)` 같은 API 요청.
2. **보호자 확인 후 발화 문장** — 현재 서버가 고정 문구("보호자분이 확인하셨어요…")를 말함. 응답 생성기(`responses.py`)에서 만들어 주면 톤이 일관됨.
3. **카메라만 낙상일 때 20초** — 소리·레이더 없이 포즈만 fall 이면 MEDIUM(10초×2). 15초 목표를 맞추려면 `response_timeout`/`max_asks` 조정 또는 "낙상 포즈 단독도 HIGH" 검토. (포즈+소리는 12초로 OK)
4. 입력은 서버가 0.25초 창에서 합쳐 한 건으로 넣는다 (따로 넣으면 "새 위험 근거"로 보고 재질문함). 의도된 동작이면 그대로 두고, 같은 시각 신호는 한 입력으로 온다는 걸 README 가정 표에 추가 요청.

### 한정연 (음성)
1. 이벤트 `time` 이 센서 시작 후 경과초라 서버가 수신 시각(epoch)으로 대체 중 (`sensor_time` 으로 보존). 가능하면 `ts`(epoch) 필드 추가.
2. `stop()` 이 STT flush 때문에 수 초 걸림 → 서버는 3초 후 강제 종료. 빠른 종료 옵션이 있으면 좋음.
3. 마이크 장치 번호는 `config.yaml` 의 `audio.input_device` 로 지정. 이 Mac 기준 0 = 내장 마이크.

### 이유민 (비전)
1. 움직임 유무(키포인트 이동량)를 `motion` 이벤트로 내보내면 레이더 없이도 대화 모듈의 `motion` 입력(still/moving)으로 쓸 수 있음 → 카메라 단독 20초 문제 완화.
2. `fall_alert` 10초 타이머는 서버에서 보호자 알림으로 쓰지 않음 (대화 모듈이 기준). 혼동 없게 INTEGRATION.md 에 명시.

### 김유진 (웹앱)
1. `user-face.js` 로드 시 8초간 자동 데모(sleep→wake→…)가 서버 메시지와 겹침 → 서버 연결 시(`window.DaonWS` 존재) 데모 생략.
2. `guardian.js` 9초 뒤 "레이더 끊김" 데모 타이머 제거 (실제 `device_status` 가 덮어쓰지만 토스트가 뜸).
3. alert 상태의 자체 10초 카운트다운 대신 서버 `countdown` 값을 쓰도록 `setState` 옵션 추가 (지금은 `ws-client.js` 가 타이머를 끄고 덮어씀).
4. 감정 `surprised` 표시(현재 neutral 로 표시), 기기 상태 표의 카메라/마이크 행에 id 부여.
5. 추가된 파일: `web/js/ws-client.js` (+ html 2곳에 script 한 줄). 규약 수신 로직은 여기 있으니 UI 쪽으로 흡수해도 됨.

## 5. 담당자 확인 목록 (결정 필요)

| 질문 | 현재 가정 |
|---|---|
| 레이더(LD2410B) 드라이버 담당? 4개 zip 어디에도 없음 | 통합자가 `server/workers/radar_worker.py` 임시 작성, 실기기 미검증, 기본 `off` |
| 스마트폰 카메라/마이크를 쓸 것인가 | 두 모듈 다 PC 직결 전제 → 1차는 **PC 웹캠/마이크**, 폰은 얼굴·스피커만. 폰 카메라/마이크를 쓰려면 WebRTC 스트리밍 추가 작업 |
| TTS 엔진 | 폰 브라우저 SpeechSynthesis. 품질 부족하면 PC TTS(예: edge-tts) + 오디오 전송으로 교체 |
| HTTPS | 폰이 카메라/마이크를 안 쓰면 불필요 (SpeechSynthesis 는 http 에서도 동작, 첫 터치 필요 → index.html "시작하기" 버튼이 그 역할) |
| 보호자 외부 알림(문자/카톡) | 혜원 모듈 웹훅 채널에 Zapier 연결 가능. `.env` 의 `ALERT_WEBHOOK_URL` 만 설정하면 됨 |

## 6. 다음 단계 (정확도·튜닝)

- **비전**: UR Fall 기준 F1 0.687 (정면 낙상 미탐, 빠르게 눕기 오탐). 로봇 카메라 높이/각도 확정 후 `src/config.py` 임계값 재보정. `motion` 이벤트 추가 검토.
- **음성**: 실환경 오탐 2/15 (의자 끌기) → 단독 발동 금지는 융합에서 이미 보장(소리 35점 < 발동 35점+). 한국어 발화 데이터 보강은 후속.
- **대화**: 키워드 규칙 기반 ("시험에 떨어졌어" 오탐 등). 위험 점수 가중치는 실측 후 조정.
- **융합 지연**: 포즈+소리 12초 / 포즈만 20초 → 레이더 또는 비전 motion 으로 15초 이내 보장.
- **실기 검증 순서**: ① `./run.sh` 로 PC 웹캠·마이크 기동 → ② 폰에서 `/user-face.html` 접속, TTS 들리는지 → ③ 실제 낙상 모션 → 보호자 팝업 시간 측정 → ④ "괜찮아" 오탐 해제 → ⑤ 레이더 연결 후 `radar.mode: worker`.
