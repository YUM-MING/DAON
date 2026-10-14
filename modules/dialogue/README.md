# silvercare-dialogue

> **통합 담당자는 먼저 [docs/INTEGRATION.md](docs/INTEGRATION.md)를 보세요** (입력·출력 형식, 연결 코드 예시, 통합 체크리스트를 1~2쪽으로 요약).

SilverCare AI 졸업작품 중 **교감형 대화 알고리즘 + 안전 상황 판단 + 비상 판단/보호자 알림 연결** 모듈입니다.

SilverCare AI는 혼자 지내는 어르신의 안전과 정서를 돌보는 반려로봇/서비스입니다. 이 저장소는 그중
포즈·표정·음성(STT)·환경음·mmWave 움직임 정보를 받아 **무슨 말을 할지**와 **보호자에게 알려야 하는지**를 결정하는 부분을 담당합니다.
TTS 모델, 센서/인식 모델, 보호자 웹앱(UI)은 각각 TTS 모듈·인식 모듈·UI 모듈에서 맡으며 이 저장소에는 연결 지점(인터페이스)만 있습니다.

작성: 채혜원 (대화 로직·비상 알림 모듈)

## 빠른 실행

Python 3.10 이상이 필요합니다. 프로젝트 폴더에서 Windows PowerShell 기준:

```powershell
python -m venv .venv                                       # 가상환경 만들기
.\.venv\Scripts\Activate.ps1                               # 가상환경 켜기
pip install -r requirements.txt                            # → pytest 설치 (실행 자체는 표준 라이브러리만 사용)
python -m pytest -q                                        # → 324 passed
python main.py --replay samples/fall_no_response.jsonl     # → 낙상 → 다시 듣기 → 재질문 → 보호자 비상 알림 순서로 출력
```

자세한 설명은 아래 목차를 참고하세요.

## 목차

통합 요약 문서: [docs/INTEGRATION.md](docs/INTEGRATION.md)

1. [특징](#특징)
2. [처리 흐름 (입력 → 판단 → 출력)](#처리-흐름-입력--판단--출력)
3. [주요 기능](#주요-기능)
4. [설치](#설치)
5. [실행 방법](#실행-방법)
6. [실시간 모드 (--live)](#실시간-모드---live)
7. [재생 모드 (--replay)와 샘플 입력](#재생-모드---replay와-샘플-입력)
8. [입력 형식](#입력-형식)
9. [출력 형식](#출력-형식)
10. [팀원 코드에서 호출하기](#팀원-코드에서-호출하기)
11. [TTS 모듈 연결 방법](#tts-모듈-연결-방법)
12. [보호자 알림](#보호자-알림)
13. [알림 연동 (웹훅)](#알림-연동-웹훅)
14. [환경변수](#환경변수)
15. [실시간성](#실시간성)
16. [장기 기억 (기본 OFF)](#장기-기억-기본-off)
17. [테스트](#테스트)
18. [프로젝트 구조](#프로젝트-구조)
19. [가정 및 통합 시 확인 사항](#가정-및-통합-시-확인-사항)
20. [알려진 한계](#알려진-한계)
21. [팀 통합 시 남은 작업](#팀-통합-시-남은-작업)

## 특징

- **LLM 호출에 의존하지 않는 자체 알고리즘**: 입력 분석 → 상황 판단 → 대화 상태 결정 → 응답 전략 결정 → 문장 생성을 Python 규칙 로직으로 직접 구현했습니다. 현재 LLM은 연결되어 있지 않으며, 일상/공감 문장을 다듬는 선택적 확장 지점(`DialogueEngine(llm=...)`)만 있습니다.
- **안전 판단과 문장은 규칙·고정 템플릿**: 안전 확인·비상 관련 문장은 항상 고정 템플릿을 사용하고, 비상 여부를 LLM에 맡기지 않습니다.
- **판단 근거 기록**: 위험 점수와 그 근거(`evidence`)가 출력과 보호자 메시지에 함께 남습니다.
- **외부 라이브러리 없이 실행**: 실행은 Python 표준 라이브러리만 사용합니다 (`pytest`는 테스트용).

## 처리 흐름 (입력 → 판단 → 출력)

```
 [입력] pose / emotion / stt / sound_event / motion (+ 확신도, timestamp)
        ← 포즈·표정 모델, STT, 소리 분류기, mmWave (통합 단계에서 연결)
                         │
              DialogueInput.from_dict()      라벨·확신도 정규화, 별칭 처리, 알 수 없는 값은 warnings
                         │
                ConversationContext          최근 센서 이벤트, 낙상 사건, 감정, 대화 이력
                         │
     text_analyzer.analyze()  +  situation.assess()
       (발화 의미 분석)            (위험 점수·등급 / 정서 판단)
                         │
                  DialogueEngine             [판단] 상태 결정 → 응답 전략 → 안전 확인 / 비상 관리
                         │
                  DialogueOutput
                ┌────────┴─────────────────────────┐
          response_text                emergency / notice / alert_status
                │                                  │
     [출력] TTS 연결점 (TTS.speak)        AlertDispatcher → Console / File(logs/alerts.jsonl)
           ← TTS 모듈 연결                               / Telegram / Webhook(보호자 웹앱)

 SilverCarePipeline: 위 흐름 + 보호자 알림(먼저) + TTS 전달을 한 번에 처리
                     (background=True면 알림·TTS를 각각 별도 스레드에서 실행해 메인 루프를 막지 않음)
 RealtimeRunner:     입력 큐 + 1초 tick (입력이 없어도 시간 기반 로직 진행)
```

코드에서의 연결 위치:

| 구분 | 공개 진입점 (팀원 코드에서 호출) | 내부 구현 (직접 호출하지 않음) |
|---|---|---|
| 입력 | `RealtimeRunner.feed(dict)`, `SilverCarePipeline.handle(dict)`, `DialogueInput.from_dict(dict)` | `main.py:run_live`의 입력 스레드 |
| 판단 | `DialogueEngine.process(DialogueInput)` | `DialogueEngine._general` / `_safety` / `_after_escalation`, `situation.assess` |
| 시간 진행 | `SilverCarePipeline.tick(now)`, `RealtimeRunner.poll_once()` / `run(stop_event)` | — |
| 출력 | 반환값 `DialogueOutput`(`.to_dict()`), 콜백 `on_tts_done` / `on_delivery` / `on_error` | `SilverCarePipeline._send_alerts`(알림), `SilverCarePipeline._speak`(TTS) |
| 마무리 | `SilverCarePipeline.wait_idle(timeout)` | `BackgroundWorker` |

## 주요 기능

### 대화 상태와 응답 전략

| 상태 | 의미 |
|---|---|
| `NORMAL` | 일상 대화 |
| `EMOTIONAL` | 부정 감정에 대한 공감 대화 |
| `SAFETY_CHECK` | 안전 확인 중 또는 비상 상황 |

응답 전략(`strategy`): `NONE`(말하지 않음), `SMALL_TALK`, `EMPATHIZE`, `FOLLOW_UP`, `GENTLE_PROBE`, `ASK_CHECK`, `REASK`, `RETRY_LISTEN`, `CLARIFY`, `RESUME`, `ESCALATE`, `REASSURE`

### 일반·정서 대화
- 인사·식사·감사·취침 등 일상 화제별 응답, 최근에 쓴 문장은 피해서 선택 (모두 쓴 뒤에는 가장 오래전에 쓴 문장부터 다시 사용)
- 기쁨 표현("너무 기뻐", "행복해")이나 밝은 표정(60초 이내)이 있으면 화제 응답보다 먼저 함께 기뻐하기 (가족·건강 화제는 전용 문장)
- 방금 한 말을 되묻지 않기: "아침 먹었어" → "맛있게 하셨다니 다행이에요", "밥 안 먹었어"·"뭐 먹을까?" → 가벼운 제안, "무릎이 쑤셔" → 불편함을 받아주는 말 ("아파" 같은 통증은 지금처럼 안전 확인)
- 표정과 발화를 함께 보고 공감 강도(가볍게/깊게)를 결정
- 말이 없어도 표정에서 부정 감정이 새로 감지되면 먼저 말 걸기 (최소 30초 간격)
- 표정은 어두운데 "괜찮아"·"기분 좋아"라고 하면 조심스럽게 되묻기. 단 감사·회복 발화("고마워", "좋아졌어" 등)에는 평상시 응답. 되묻기는 연속 2번까지만 하고, 3번째부터는 "편하실 때 말씀해 주세요" 같은 물러서는 말
- 부정 발화가 3회 이어지거나 같은 주제(외로움·가족·건강·수면)가 반복되면 되묻기, 5회 연속이면 보호자 정서 알림(`notice`, 비상 아님)
- 최근 20턴 대화 이력 유지
- 과거형 긍정("그래도 오늘은 좋았어", "즐거웠어")은 슬픈 표정 직후에도 회복 쪽으로 보고 좋았던 일을 받아주는 말("좋은 하루를 보내셨다니 저도 기뻐요")
- 오래된 밝은 표정에 끌려가지 않고 실제 발화 기준으로 응답 (60초가 지난 밝은 표정은 긍정 응답에 쓰지 않음). "피곤해·지쳤어·기운이 없어"는 쉬시도록 권하는 말, "힘들어"는 지금처럼 공감
- 약 복용("약 먹었어")은 식사로 되묻지 않고 약 챙김을 받아주는 말, 수면 불편("잠을 못 자", "잠을 설쳤어")은 다시 묻지 않고 받아주는 말 (의료 조언은 하지 않음)
- 질문형 입력: "몇 시야"는 입력 시각(`timestamp`, 실행 PC 시간대)으로 "오후 3시 20분"처럼 답하고, "날씨 어때"는 날씨 정보가 연결되지 않았음을 솔직히 말하며(날씨를 지어내지 않음), "뭐 할 줄 알아"에는 대화·안부 확인·위급 시 보호자 알림을 소개. "이름이 뭐야"는 기존 응답
- 직전 발화와 같은 화제가 이어지면 처음 꺼내는 말 대신 "가족분 이야기 더 들려주세요", "그다음엔 어떻게 되셨어요?" 같은 이어가는 말. 고인 표현(돌아가신·영감·할멈 등)과 그리움("보고 싶네", "그리워")이 함께 있으면 그리움을 짚는 공감 (위기 표현이 섞이면 지금처럼 즉시 비상)
- 시간·날씨를 묻는 문장("시간 알려줘", "날씨 알려줘")의 "알려줘"는 "살려줘" 오인식 보정에서 제외. 단독 "알려줘", "나 좀 알려줘"·"누가 좀 알려줘"는 실제 "살려줘"의 오인식일 수 있어 안전을 위해 기존대로 비상

### 안전 판단 (위험 점수)
`situation.assess()`가 아래 근거로 위험 점수를 계산합니다. 점수는 가중치 합산 방식의 규칙 기반 heuristic입니다.

| 근거 | 사용 입력 |
|---|---|
| 낙상 포즈, 낙상음, 둘의 일치 / 낙상음 직후 누운 자세 (15초 이내 교차 확인) | `pose`, `sound_event`, 각 확신도 |
| 낙상 근거가 있을 때 움직임 보정 (없음 +, 있음 −, 정보 없음 소폭 −, 자는 중 보정) | `motion`, `pose=sleeping` |
| 누운 채 60초 이상 움직임 없음 | `pose=lying` + `motion` |
| 확인 후에도 바닥 자세가 120초 이상 지속 | `pose` (fall/lying 지속) |
| 위급 발화 / 위험 의심 발화 / 낙상 언급 / 극단적 선택 암시 | `stt` |

- 확신도 0.4 미만 신호는 무시하고, 확신도에 비례해 점수를 줍니다.
- 위험 등급: 35점 이상 `MEDIUM`(안전 확인 시작), 70점 이상 `HIGH`(대기 시간 단축), 위급 발화·극단적 선택 암시는 `CRITICAL`(즉시 비상).
- `emotion`과 대화 이력은 위험 점수에 **직접 들어가지 않습니다** (정서 판단과 응답 전략에만 사용).
- mmWave 값은 30초가 지나면 "정보 없음"으로 보며, 정보 없음을 "움직임 있음"으로 간주하지 않습니다.
- 같은 낙상 상태가 반복 입력돼도 한 사건으로 처리하고, `normal` 자세가 3초 이상 이어져야 일어난 것으로 봅니다.

### 능동 안전 확인
위험 감지 → "괜찮으세요?"(원인별 첫 질문) → 대답/센서 재확인 → 정상 복귀 / 재질문 / 비상

- 응답 대기 10초 (HIGH 위험이면 6초), 최초 질문 포함 2회 질문. 대기 시간은 출력의 `response_deadline_sec`로 함께 나가므로 UI 카운트다운을 엔진과 맞출 수 있습니다.
- 목소리는 감지됐는데 STT 텍스트가 없으면(`sound_event`가 `speech` 또는 `voice_unclear`) 무응답으로 보지 않고 다시 듣기 (`RETRY_LISTEN`)
- 모호한 대답은 한 번 더 구체적으로 확인 (`CLARIFY`)
- 확인 중 새 센서 위험 근거(예: 다른 이유로 확인하던 중 실제 낙상)가 들어오면 그 근거로 다시 확인
- 마지막 질문에도 대답이 없으면, 유효한 `moving`이 있을 때만 정상 복귀(모니터링 계속), 그 외에는 비상
  (단, 바닥 자세가 오래 이어져 다시 확인한 경우에는 움직임만으로 복귀하지 않고 비상)

### 비상 상태 관리
| `alert_status` | 의미 |
|---|---|
| `raised` | 최초 비상 발생 |
| `updated` | 비상 중 더 심각한 정보(위험 발화 → 극단적 선택 암시 등)로 원인 갱신 |
| `realert` | 마지막 알림 후 60초가 지나도 안전이 확인되지 않으면 재알림 (최대 2회) |
| `resolved` | "괜찮아"처럼 명확한 안전 응답으로 해제 → 보호자에게 해제 알림 |

- 비상 원인(`alert_cause`): `no_response`(무응답), `danger`(위험 발화), `crisis`(극단적 선택 암시)
- **응답 있음 ≠ 안전 확인**: 사용자가 말을 했다는 사실은 기록(`user_responded`)하되, "응/네"만으로는 해제하지 않습니다.

### 장기 기억 (선택, 기본 OFF)
- `--user ID`로 켜면 사용자별로 이야기한 화제(가족·건강·잠 등)와 횟수만 JSON 파일에 기억하고, 다음 세션 첫 일상 대화에서 한 번 "지난번에 말씀하신 가족 이야기, 그 뒤로 어떠세요?"처럼 이어 줍니다. 원문은 저장하지 않고 안전 판단에는 쓰지 않습니다. 자세한 내용은 [장기 기억 (기본 OFF)](#장기-기억-기본-off)

## 설치

**Python 3.10 이상 필요** (3.11 권장, 3.11~3.13에서 설치·테스트 확인). 테스트용 `pytest==9.1.1`이 Python 3.10 이상을 요구하므로 3.9 이하에서는 `pip install -r requirements.txt`가 실패합니다. Windows PowerShell 기준:

```powershell
git clone https://github.com/chaehyewon/silvercare-dialogue.git
cd silvercare-dialogue
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

- `requirements.txt`에는 테스트용 `pytest`만 있습니다. 실행 자체에는 추가 설치가 필요 없습니다.
- PowerShell 실행 정책 때문에 `Activate.ps1`이 막히면, 활성화 없이 `.\.venv\Scripts\python.exe main.py`처럼 가상환경의 Python을 직접 실행하면 됩니다.

## 실행 방법

프로젝트 루트에서 실행합니다. (`main.py`가 자기 위치를 기준으로 `src/`를 찾으므로 다른 폴더에서 실행해도 동작합니다.)

| 명령 | 동작 |
|---|---|
| `python main.py` | 시연 모드. 대표 상황 7개(일상 대화, 공감, 낙상→응답, 낙상→무응답→비상, 낙상음 융합, 과거 이야기, 극단적 선택 암시)를 `[입력] [판단] [대화] [알림]` 로그로 출력 |
| `python main.py --interactive` | 직접 입력 모드. 문장만 입력하면 발화(stt)로 처리, `pose=fall motion=still`처럼 `key=value`로 센서 값 입력. 입력마다 3초 경과로 계산하며 `t=20`(시각 이동), `wait 7`(입력 없이 7초 경과), `reset`, `help`, `quit` 지원 |
| `python main.py --spec` | 다른 모듈과 주고받는 입력/출력 JSON 예시 출력 |
| `python main.py --live` | 실시간 모드 (실제 시계, 알림·TTS 연결). 아래 참고 |
| `python main.py --live --linger 30` | 실시간 모드 + 입력이 끝난 뒤에도 30초 동안 시간 흐름(대기·재질문·비상·재알림)을 계속 진행 |
| `python main.py --replay samples/fall_no_response.jsonl` | 재생 모드 (JSONL을 가상 시간으로 재생). 아래 참고 |
| `python main.py --interactive --user room203` | 장기 기억을 켜고 실행 (`--live`·`--replay`에도 붙일 수 있음). [장기 기억](#장기-기억-기본-off) 참고 |
| `python benchmarks/measure_latency.py` | 처리 시간 측정 ([실시간성](#실시간성)) |
| `python samples/webhook_receiver.py` | 로컬 웹훅 수신 테스트 서버 ([알림 연동](#알림-연동-웹훅)) |

시연·직접 입력·재생 모드는 대화 엔진만 실행하며 TTS·보호자 알림은 실제로 보내지 않습니다 (로그에 "알림 발생"만 표시).
`src/realtime_demo.py`는 가짜 시계로 파이프라인 전체를 시연하는 별도 스크립트입니다 (`src/demo.py`는 이전 버전 시연 스크립트).

## 실시간 모드 (--live)

```powershell
python main.py --live
```

- 표준 입력(stdin)으로 **JSON 객체를 한 줄씩** 입력합니다.
- 실제 시계로 동작하며, 입력이 없어도 1초마다 tick이 진행되어 응답 대기 → 재질문 → 비상 → 재알림이 시간에 따라 일어납니다.
- **입력 JSON의 `timestamp`는 무시**하고 입력이 처리되는 현재 시각을 사용합니다 (이 규칙은 `--live`에만 적용).
- 보호자 알림 발송과 TTS 재생은 각각 별도 스레드에서 실행되어 메인 루프(판단·tick)를 막지 않습니다.
- 응답 문장은 TTS 연결점(기본: 콘솔 출력 `🔊 ...`)이 한 번만 출력하고, 판단 로그에는 상태·전략·사유·`[대기]`·`[비상]`·`[알림 전달]`이 표시됩니다.
- 잘못된 JSON 줄은 경고를 표시하고 건너뛴 뒤 계속 입력을 받습니다.
- 입력 끝(EOF, 남은 작업을 마친 뒤) 또는 Ctrl+C로 정상 종료합니다. `--linger SEC`를 주면 입력이 끝난 뒤에도 SEC초 동안 시간 흐름을 계속 진행합니다.
- 어떤 방식으로 종료하든(입력 끝, `--linger` 종료, Ctrl+C) 대기·진행 중인 알림 발송과 TTS를 **최대 3초** 기다렸다가 끝나므로, 비상 직후 종료해도 알림(웹훅 등)이 나갑니다. 기다리는 중 Ctrl+C를 한 번 더 누르면 바로 끝납니다.
- 판단 로그, TTS 출력(🔊), 알림 출력은 같은 출력 잠금을 사용해 줄 단위로 섞이지 않습니다.
- 실행 위치와 관계없이 **프로젝트 루트**의 `.env`를 읽고 `logs/alerts.jsonl`에 알림을 기록합니다.

입력 예시:

```json
{"pose": "fall", "pose_conf": 0.95, "motion": "still"}
{"stt": "응 괜찮아", "pose": "normal", "motion": "moving"}
{"stt": "오늘 기분이 좀 안 좋아", "emotion": "sad", "emotion_conf": 0.8}
```

첫 줄만 입력하고 기다리면 약 6초 뒤 재질문, 약 12초 뒤 보호자 비상 알림이 발생합니다 (HIGH 위험 기준).

`--live`에 파이프·리다이렉트로 입력하면 **입력이 끝나는 즉시(남은 작업을 마친 뒤) 종료**되므로, 재질문·비상 같은 시간 흐름은 나타나지 않습니다. 시간 흐름을 재현하려면 둘 중 하나를 사용하세요.

- **`--linger SEC`**: 입력이 끝난 뒤에도 SEC초 동안 실제 시간으로 tick(응답 대기·재질문·비상·재알림·알림 전송)을 계속 진행한 뒤 종료합니다. 기본값 0은 입력이 끝나면 바로 종료.
  ```powershell
  Get-Content samples\fall_no_response.jsonl -Encoding utf8 | python main.py --live --linger 30
  ```
  → 확인 질문, 다시 듣기 → 약 6초 뒤 재질문 → 약 12초 뒤 비상 알림 → 30초 뒤 종료 (실제로 30초 걸림)
- **`--replay`**: 기다리지 않고 timestamp 기준 가상 시간으로 재생 ([재생 모드](#재생-모드---replay와-샘플-입력)).

PowerShell 파이프가 첫 줄에 붙이는 UTF-8 BOM은 자동으로 제거합니다. 한글이 들어 있는 파일은 PowerShell 설정에 따라 인코딩이 바뀔 수 있으므로 `cmd /c "python main.py --live --linger 30 < 파일"` 리다이렉트를 권장합니다.

## 재생 모드 (--replay)와 샘플 입력

```powershell
python main.py --replay samples/daily_chat.jsonl
python main.py --replay samples/emotional_sad.jsonl
python main.py --replay samples/fall_no_response.jsonl
```

- JSONL(한 줄에 JSON 입력 하나)을 순서대로 재생합니다. 각 줄의 `timestamp`(초)를 기준으로 **실제로 기다리지 않고 가상 시간**으로 진행하며, 입력 사이에는 1초 간격 tick을 넣어 응답 대기/재질문/비상/재알림이 시간에 따라 일어납니다.
- `timestamp`가 없는 줄은 직전 입력 1초 뒤로 봅니다. 잘못된 줄은 경고 후 건너뜁니다.
- 대화 엔진만 실행합니다 (TTS 재생·보호자 알림 발송은 하지 않고 로그에 표시만). 같은 파일은 항상 같은 결과를 냅니다.

| 샘플 | 내용 | 기대 결과 |
|---|---|---|
| `samples/daily_chat.jsonl` | 인사, 식사, 날씨, 감사, 취침 | 모두 `SMALL_TALK`, 안전 확인·비상 없음 |
| `samples/emotional_sad.jsonl` | 슬픈 표정 → 외로움·그리움 발화 | `EMPATHIZE` 후 3번째 부정 발화에서 `FOLLOW_UP` |
| `samples/fall_no_response.jsonl` | 낙상(포즈+낙상음+무동작) → `voice_unclear` → 무응답 | `ASK_CHECK`(0초) → `RETRY_LISTEN`(3초) → `REASK`(9초) → `ESCALATE` 비상(15초) |

샘플은 실제 입력 형식과 같으므로 `--live`에도 넣을 수 있습니다. 단 `--live`는 timestamp를 무시하고 줄들을 한꺼번에 처리합니다.

```powershell
cmd /c "python main.py --live < samples\daily_chat.jsonl"
```

(Windows PowerShell 5.1의 `Get-Content ... |` 파이프는 한글을 다른 인코딩으로 바꿔 보낼 수 있어 `cmd /c` 리다이렉트를 권장합니다. `main.py`는 표준 입력을 UTF-8로 읽습니다.)

## 입력 형식

`DialogueInput.from_dict(dict)`가 받는 필드입니다. **모든 필드는 선택**이며, 없는 값은 생략하거나 `null`로 보냅니다 ("이번에 새로 들어온 값 없음").

| 필드 | 타입 | 기본값 | 허용 값 / 설명 | 예시 |
|---|---|---|---|---|
| `timestamp` | float(초) 또는 ISO 8601 문자열 | 없으면 현재 시각 | 입력 시각. 숫자는 **초 단위**. 값이 `1e11`보다 크면 밀리초(예: JavaScript `Date.now()`)로 보고 초로 자동 변환 (경고는 처음 한 번만) | `12.0`, `1791698412000` |
| `pose` | str | null | `normal` `lying` `sleeping` `fall` | `"fall"` |
| `pose_conf` | float | null (1.0으로 간주) | 0~1. 1~100 값은 퍼센트로 보고 변환. 0.4 미만은 무시 | `0.95` |
| `emotion` | str | null | `neutral` `happy` `sad` `angry` `fear` | `"sad"` |
| `emotion_conf` | float | null (1.0) | `pose_conf`와 동일 | `0.8` |
| `stt` | str | null | 어르신 발화 텍스트. 빈 문자열은 발화 없음 | `"괜찮아"` |
| `sound_event` | str | null | `none` `speech` `fall_sound` `daily` `voice_unclear` | `"fall_sound"` |
| `sound_conf` | float | null (1.0) | `pose_conf`와 동일 | `0.85` |
| `motion` | str | null | mmWave 움직임: `moving` `still` `none`(사람 없음). 30초 지나면 정보 없음으로 처리 | `"still"` |

- 라벨은 대소문자·공백·하이픈을 구분하지 않고, 일부 별칭도 받습니다. 예: `sitting`·`standing`·`walking` → `normal`, `falling`·`낙상` → `fall`, `슬픔` → `sad`, `impact` → `fall_sound`, `stationary`·`미동` → `still` (전체 목록은 `src/dialogue_types.py`의 `ALIASES`).
- **`voice_unclear`**: "사람 목소리는 감지됐지만 STT 텍스트로 바꾸지 못함". 예비 별칭 `unclear_voice`, `unclear_speech`, `speech_unclear`도 같은 값으로 받습니다 (목록: `src/dialogue_types.py`의 `VOICE_UNCLEAR_ALIASES`). 안전 확인 중에는 `speech` + 빈 텍스트와 똑같이 다시 듣기로 처리합니다.
- `pose_confidence`, `emotion_confidence`, `sound_confidence` 키도 확신도로 받습니다.
- 알 수 없는 **값**(예: `"pose": "jumping"`)은 무시하고 출력의 `warnings`에 기록합니다.
- 정의되지 않은 **키**(예: `"text"`)는 무시하고, 같은 키는 처음 한 번만 `warnings`에 경고를 남깁니다 (매 프레임 반복 경고 방지). 발화는 반드시 `stt` 키로 보내야 합니다. 받는 키 목록: `src/dialogue_types.py`의 `KNOWN_INPUT_KEYS`.
- 응답 대기·재알림 같은 타이머는 입력의 시각으로 진행되므로, 새 입력이 없어도 약 1초마다 빈 입력(`{"timestamp": 현재시각}`)을 넣거나 `SilverCarePipeline.tick()` / `RealtimeRunner`를 사용해야 합니다.

## 출력 형식

`DialogueEngine.process()`가 반환하는 `DialogueOutput` (`.to_dict()`로 dict 변환):

| 필드 | 타입 | 설명 |
|---|---|---|
| `state` | str | `NORMAL` / `EMOTIONAL` / `SAFETY_CHECK` |
| `strategy` | str | 이번 턴의 응답 전략 (위 목록) |
| `response_text` | str 또는 null | **TTS로 보낼 문장**. null이면 말하지 않음 |
| `emergency` | bool | true인 턴에 보호자 비상 알림을 보냄 |
| `alert_seq` | int | 이번 비상 사건에서 보낸 알림 순번 (0 = 알림 없음, 최초·갱신·재알림 포함) |
| `alert_cause` | str | 현재 비상 원인: `no_response` / `danger` / `crisis` (없으면 `""`) |
| `alert_status` | str | 비상 상태가 바뀐 순간의 이벤트: `raised` / `updated` / `realert` / `resolved` (변화 없으면 `""`) |
| `alert_origin` | str | 이번 비상의 최초 원인 (원인이 갱신돼도 유지) |
| `user_responded` | bool | 안전 확인·비상 중 사용자 발화가 있었는지 (안전 확인 완료를 뜻하지 않음) |
| `notice` | str 또는 null | 비상은 아닌 보호자 정서 알림 문장 |
| `reason` | str | 이번 판단의 이유 (로그용 문장) |
| `assessment` | dict 또는 null | 상황 판단 결과: `risk_score`, `risk_level`(`NONE`~`CRITICAL`), `evidence`(근거 목록), `valence`(`negative`/`neutral`/`positive`), `emotion_intensity`(0~1), `trigger`, `mismatch` |
| `warnings` | list | 입력값 문제 (알 수 없는 라벨 등) |
| `response_deadline_sec` | float 또는 null | 안전 확인 질문(`ASK_CHECK`·`REASK`) 및 대기를 다시 시작하는 `RETRY_LISTEN`·`CLARIFY` 출력에서, 이번 질의 후 응답을 기다리는 시간(초). 위험도가 높으면 짧아짐(현재 10초 / HIGH 6초). 그 외에는 null |
| `emergency_active` | bool | 비상 알림이 발생한 턴(`raised`)부터 해제(`resolved`)되기 전까지 **매 턴 true** (갱신·재알림·안심 멘트·대기 tick 포함). 평상시, 안전 확인 대기 중(아직 비상 전), 해제된 턴과 그 이후에는 false |

`alert_status`는 상태가 바뀌는 턴에만 값이 있는 "이벤트"이고, `emergency_active`는 지금 비상 중인지를 나타내는 "상태"입니다. UI는 `emergency_active`로 비상 화면을 켜고 끄며, `alert_status`로 발생·갱신·재알림·해제 순간을 표시하면 됩니다.

향후 확장: 보호자(웹앱)가 비상을 직접 해제하는 입력은 아직 없습니다. 현재 비상은 어르신의 명확한 안전 응답("괜찮아" 등)으로만 해제됩니다.

출력 예 (`python main.py --spec` 일부):

```json
{"state": "SAFETY_CHECK", "strategy": "ASK_CHECK", "response_text": "괜찮으세요? 제 말 들리시면 대답해주세요.",
 "emergency": false, "alert_seq": 0, "alert_cause": "", "alert_status": "", "alert_origin": "",
 "user_responded": false, "notice": null, "reason": "안전 확인 시작: ...", "assessment": {"risk_score": 99, "risk_level": "HIGH", "...": "..."},
 "warnings": [], "response_deadline_sec": 6.0, "emergency_active": false}
```

## 팀원 코드에서 호출하기

모든 예시는 같은 Python 환경에서 `src/`를 import 경로에 추가해 사용합니다.

### 1. 대화 엔진만 직접 호출

```python
import sys
sys.path.insert(0, "src")          # 프로젝트 루트에서 실행할 때

from dialogue_algorithm import DialogueEngine, DialogueInput

engine = DialogueEngine()          # 사용자 1명당 엔진 1개

out = engine.process(DialogueInput.from_dict(
    {"timestamp": 0, "pose": "fall", "pose_conf": 0.95, "motion": "still"}))
print(out.state.value, out.strategy.value, out.response_text, out.response_deadline_sec)
# SAFETY_CHECK ASK_CHECK 괜찮으세요? 제 말 들리시면 대답해주세요. 6.0

out = engine.process(DialogueInput.from_dict({"timestamp": 6}))   # 새 입력 없이 시간만 진행
print(out.strategy.value)          # REASK

out = engine.process(DialogueInput.from_dict({"timestamp": 12}))
print(out.emergency, out.alert_status, out.alert_cause)   # True raised no_response

data = out.to_dict()               # dict(JSON 변환 가능 형태)
```

### 2. 파이프라인: 판단 + 보호자 알림 + TTS 전달

```python
import sys, time
sys.path.insert(0, "src")

from dialogue_algorithm import DialogueEngine
from notifier import build_default_dispatcher
from pipeline import SilverCarePipeline
from tts_adapter import CallbackTTS

pipeline = SilverCarePipeline(
    DialogueEngine(),
    CallbackTTS(lambda text: print("[TTS로 전달]", text)),   # TTS 모듈 함수로 교체
    build_default_dispatcher(env_path=".env", log_path="logs/alerts.jsonl"),
    on_error=print,                                          # TTS/알림 오류 보고
    background=True,                                         # 알림·TTS를 별도 스레드에서 (메인 루프 비블로킹)
    on_tts_done=lambda ok: print("재생 끝:", ok),            # TTS 재생 완료(실패 포함)
    on_delivery=lambda status, out: print("외부 알림:", status),   # sent / failed / unconfigured
)

out = pipeline.handle({"stt": "도와줘"})    # timestamp가 없으면 현재 시각
print(out.emergency, out.alert_status)      # True raised (알림 발송이 먼저 접수, 그다음 TTS)

pipeline.tick(time.time())                  # 입력이 없을 때도 주기적으로 호출
pipeline.wait_idle(5)                       # (종료 전) 남은 알림·TTS 작업 대기
```

`background`를 생략하면(기본 False) 알림과 TTS를 `handle()` 안에서 차례로 실행합니다.
**실제 연동에서는 `background=True`를 권장합니다.** 동기 모드에서는 웹훅 서버에 연결되지 않을 때 `handle()`이 최대 약 10초(타임아웃 3초 × 3회 시도 + 재시도 간격) 멈출 수 있고, 그동안 판단·tick도 멈춥니다.

### 3. 실시간 러너: 다른 스레드에서 입력을 넣기

```python
import threading
from pipeline import RealtimeRunner

runner = RealtimeRunner(pipeline)           # 예시 2의 pipeline 사용. 기본 시계 time.time, 1초 tick
stop = threading.Event()
threading.Thread(target=runner.run, args=(stop,), daemon=True).start()

runner.feed({"stt": "응 괜찮아"})            # 센서/STT 콜백에서 호출 (스레드 안전한 큐)
# ... 종료할 때
stop.set()
```

`runner.run()`은 결과를 반환하지 않고, TTS 전달과 보호자 알림은 파이프라인 안에서 처리됩니다.
결과를 직접 받아야 하면 `runner.poll_once()`를 반복 호출하면 됩니다 (`DialogueOutput` 반환).

## TTS 모듈 연결 방법

이 모듈의 역할은 **응답 문장(`response_text`)을 만들어 TTS로 넘기는 것**까지이며, TTS 연결 인터페이스를 제공합니다. 실제 TTS 엔진은 TTS 모듈이 구현합니다.

| 구성 | 역할 |
|---|---|
| `TTS.speak(text, on_done=None)` | 인터페이스. `on_done(ok: bool)`은 재생이 끝나면(실패 포함) 한 번 호출 |
| `CallbackTTS(fn)` | 외부 함수 하나를 TTS로 연결. `fn(text)`가 정상 반환하면 성공, 예외면 실패 |
| `SafeTTS(primary, fallback)` | 주 TTS가 실패하면 대체 출력(기본: 화면)으로 넘김. 이때 `on_done(False)` |
| `ConsoleTTS` | 화면 출력 (`🔊 문장`). 기본값 (`make_tts()`) |
| `speak_with_done(tts, text, on_done)` | `on_done`을 받지 않는 이전 방식 TTS(`speak(text)`만 있음)도 같은 방식으로 호출 |

TTS 모듈을 꽂는 예 (프로젝트 루트에 파일로 저장해 그대로 실행할 수 있습니다. `tts_module` 부분만 실제 TTS 모듈로 바꾸면 됩니다):

```python
import sys
import types
sys.path.insert(0, "src")          # 프로젝트 루트에서 실행할 때

from dialogue_algorithm import DialogueEngine
from notifier import build_default_dispatcher
from pipeline import SilverCarePipeline
from tts_adapter import CallbackTTS, SafeTTS

# ── TTS 모듈 자리 (예시용 가짜 모듈) ──
# 실제 연결 시: import tts_module  (재생이 끝날 때 반환하는 함수를 제공한다고 가정)
tts_module = types.SimpleNamespace(synthesize_and_play=lambda text: print("[재생]", text))


def play(text):                    # TTS 모듈 함수를 감싼다: 재생이 끝나면 반환
    tts_module.synthesize_and_play(text)


engine = DialogueEngine()
dispatcher = build_default_dispatcher(env_path=".env", log_path="logs/alerts.jsonl")
tts = SafeTTS(CallbackTTS(play))   # 재생 실패 시 화면 출력으로 대체되고 시스템은 계속 동작
pipeline = SilverCarePipeline(engine, tts, dispatcher, background=True,
                              on_tts_done=lambda ok: print("재생 끝:", ok))   # 실제로는 여기서 STT를 다시 켠다

pipeline.handle({"stt": "안녕"})   # → [재생] 안녕하세요! ... / 재생 끝: True
pipeline.wait_idle(5)              # 예제 종료 전에 백그라운드 재생이 끝나길 기다림

# on_done만 따로 쓰는 예 (파이프라인 없이 TTS 직접 호출)
tts.speak("괜찮으세요?", on_done=lambda ok: print("재생 성공" if ok else "재생 실패"))
```

- `background=True`이면 TTS 재생은 별도 작업 스레드(작업 큐, 순서 유지)에서 실행되어 판단·tick을 막지 않습니다.
- `on_done`은 `fn`이 반환한 시점에 호출되므로, TTS 모듈 함수가 "재생 완료 후 반환"하도록 연결해야 재생 완료 신호로 쓸 수 있습니다 ([가정](#가정-및-통합-시-확인-사항)).
- `Pyttsx3TTS`는 선택 설치(`pip install pyttsx3`) 데모용 클래스이며 `requirements.txt`에는 포함되어 있지 않습니다.

## 보호자 알림

| 구성 | 상태 |
|---|---|
| 메시지 생성 (`build_emergency` / `build_resolved` / `build_notice`) | 구현. 비상 원인·발생 시각·판단 근거, 재알림·원인 갱신·해제 문구, 극단적 선택 암시 시 109 상담전화 안내 포함 |
| `AlertDispatcher` | 구현. 채널별 최대 3회 시도, 한 채널 실패해도 다른 채널 계속 발송, 30초 내 같은 알림 중복 방지, 정서 알림 1시간 간격 제한, 실패 건 보관(`failed`) |
| `ConsoleNotifier` | 구현·테스트됨. 화면 출력 |
| `FileNotifier` | 구현·테스트됨. `logs/alerts.jsonl`에 알림 JSON 한 줄씩 기록 |
| `WebhookChannel` | 구현·테스트됨 (로컬 HTTP 수신 포함). `ALERT_WEBHOOK_URL`로 알림 JSON을 HTTP POST, 타임아웃 3초 |
| `TelegramNotifier` | **전송 코드 구현, 실제 계정 발송 검증 전.** Telegram Bot API `sendMessage`로 HTTP POST (테스트는 가짜 전송으로만 확인) |

- `build_default_dispatcher()`는 콘솔 + 파일을 항상 사용하고, `TELEGRAM_BOT_TOKEN`과 `TELEGRAM_CHAT_ID`가 **둘 다** 있을 때 텔레그램을, `ALERT_WEBHOOK_URL`이 있을 때 웹훅을 추가합니다.
- `SilverCarePipeline`은 비상(`emergency`)·정서 알림(`notice`)·해제(`resolved`)가 있을 때 알림을 TTS보다 먼저 보냅니다. `background=True`면 별도 스레드에서 발송해 메인 루프를 막지 않습니다. 동기 모드(기본값)에서는 외부 채널이 응답하지 않을 때 최대 약 10초 멈출 수 있으므로 실제 연동에서는 `background=True`를 권장합니다.
- **외부 채널 전달 상태**: 콘솔·파일은 로컬 기록일 뿐이므로, 보호자에게 실제로 전달됐는지는 외부 채널(웹훅·텔레그램) 기준으로 따로 판단합니다.

  | 상태 | 의미 | 처리 |
  |---|---|---|
  | `sent` | 외부 채널 중 하나 이상 성공 | — |
  | `failed` | 외부 채널이 설정돼 있지만 전부 실패 | `on_error`로 보고, `failed`에 보관 |
  | `unconfigured` | 외부 채널 미설정 (콘솔·파일 기록만) | 오류 아님, 상태로만 구분 |

  상태는 `on_delivery(status, out)` 콜백과 `pipeline.last_delivery`로 확인합니다 (`--live`는 `[알림 전달]` 줄로 표시).
- **재전송**: 실패해서 보관된 알림은 파이프라인이 30초(`retry_interval`, 입력 시각 기준)마다 `retry_failed()`로 다시 보냅니다. 한 건당 최대 **10회**(`src/notifier.py`의 `MAX_RETRY_ROUNDS`)까지 재전송하고, 그래도 실패하면 더 이상 보내지 않고 `gave_up`으로 기록합니다 (`AlertDispatcher.gave_up` 목록, 발송 기록 `history`의 `gave_up=True`, `on_error`로 "gave_up" 보고 — `--live`에서는 `⚠` 줄로 표시).
- Zapier 전용 채널은 없습니다. JSON POST를 받는 URL이라면 `ALERT_WEBHOOK_URL`로 연결할 수 있는 구조이지만, Zapier 등 외부 서비스로는 검증하지 않았습니다.

## 알림 연동 (웹훅)

보호자 웹앱(또는 수신 서버)은 알림이 발생할 때마다 아래 JSON을 **HTTP POST**(`Content-Type: application/json; charset=utf-8`)로 받습니다. `logs/alerts.jsonl`의 한 줄과 **같은 내용**입니다. 2xx로 응답하면 성공, 그 외에는 실패로 보고 재시도합니다.

| 필드 | 타입 | 설명 |
|---|---|---|
| `kind` | str | `emergency`(비상) / `resolved`(비상 해제) / `notice`(정서 알림, 비상 아님) |
| `status` | str | 알림 상태: `raised`(최초) / `updated`(원인 갱신) / `realert`(재알림) / `resolved`(해제) / `notice` |
| `title` | str | 화면 제목 (예: `[실버케어 긴급] 낙상 후 무응답`) |
| `body` | str | 본문 (여러 줄, 판단 근거·발생 시각·연락 안내 포함) |
| `seq` | int | 이번 비상 사건의 알림 순번 (1부터, 해제·정서 알림은 0) |
| `cause` | str | `no_response` / `danger` / `crisis` / `emotional`(정서 알림) |
| `timestamp` | float | 발생 시각 (초, 엔진 입력 시각 기준) |
| `time` | str | 사람이 읽는 시각 (`YYYY-MM-DD HH:MM:SS`, 실행 PC 시간대) |

요청 JSON 예:

```json
{
  "kind": "emergency",
  "title": "[실버케어 긴급] 낙상 후 무응답",
  "body": "낙상이 의심되는 상황 이후 말을 걸었지만 응답이 없고 움직임도 확인되지 않았어요.\n발생 시각: 2026-10-11 15:00:12\n판단 근거: 낙상 포즈(+50, 확신도 0.95) / 움직임 없음(+20) / 무응답 + 움직임 없음 (motion=still, pose=fall)\n연락이 닿지 않으면 119 신고를 고려해 주세요.",
  "seq": 1,
  "cause": "no_response",
  "timestamp": 1791698412.0,
  "status": "raised",
  "time": "2026-10-11 15:00:12"
}
```

로컬에서 받는 쪽을 테스트하는 방법 (PowerShell 창 2개):

```powershell
# 창 1: 수신 서버 (받은 알림을 화면에 출력하고 200으로 응답)
python samples/webhook_receiver.py              # http://127.0.0.1:8765/alerts

# 창 2: 낙상 샘플을 넣고 30초 동안 시간 흐름 진행 → 약 12초 뒤 비상 알림이 창 1로 들어옴
$env:ALERT_WEBHOOK_URL = "http://127.0.0.1:8765/alerts"
Get-Content samples\fall_no_response.jsonl -Encoding utf8 | python main.py --live --linger 30
```

직접 입력하려면 `python main.py --live`를 실행한 뒤 `{"pose": "fall", "pose_conf": 0.95, "motion": "still"}`를 입력하고 기다리면 됩니다.

## 환경변수

| 이름 | 필수 | 설명 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | 선택 | 텔레그램 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 선택 | 알림을 받을 채팅 ID |
| `ALERT_WEBHOOK_URL` | 선택 | 알림 JSON을 POST할 주소 (보호자 웹앱 등) |

- 프로젝트 루트의 `.env` 파일(`KEY=VALUE` 형식) 또는 시스템 환경변수로 설정합니다. 이미 설정된 환경변수는 `.env`가 덮어쓰지 않습니다.
- 텔레그램이나 웹훅을 쓸 때만 예시 파일을 복사해 값을 채웁니다. 쓰지 않으면 `.env`를 만들지 않아도 됩니다.
  ```powershell
  Copy-Item .env.example .env
  ```
- **`.env`가 없어도** 모든 실행 모드와 테스트가 실행됩니다 (이때 알림은 콘솔과 파일만 사용, 외부 채널 상태는 `unconfigured`).
- `.env`와 `logs/`는 Git에 올리지 않습니다 (`.gitignore`). 실제 토큰 값을 코드나 문서에 적지 마세요.

## 실시간성

`python benchmarks/measure_latency.py`로 이 모듈 안의 처리 시간을 측정합니다 (구간별 1000회 반복).

측정 환경: Python 3.11.3 / Windows / 개발용 PC, 2026-10-11 측정

| 구간 | 중앙값(ms) | p95(ms) | 최대(ms) |
|---|---:|---:|---:|
| `DialogueEngine.process` 1회 | 0.185 | 0.324 | 1.791 |
| `SilverCarePipeline.handle` 1회 (판단 + 알림 메시지·파일 기록 + TTS 전달) | 0.320 | 1.847 | 19.500 |
| `RealtimeRunner` 입력 투입(feed) ~ 결과 반환(poll_once) | 0.225 | 0.488 | 5.495 |

- **이 측정에는 STT 변환, 실제 TTS 합성·재생, 네트워크 전송(텔레그램·웹훅), 화면 출력 시간이 포함되지 않습니다.** 측정값은 PC와 실행 시점에 따라 달라집니다.
- 실시간 모드에서는 입력이 없어도 1초마다 tick이 진행되므로, 시간 기반 판단(응답 대기·재질문·비상·재알림)은 최대 약 1초 단위로 반영됩니다.
- 알림 발송(네트워크)과 TTS 재생은 `background=True`에서 별도 스레드로 실행되어 판단 지연에 더해지지 않습니다.

## 장기 기억 (기본 OFF)

사용자별로 **어떤 화제를 이야기했는지**만 기억해서, 다음 대화 세션의 첫 일상 대화에서 한 번 "지난번에 말씀하신 가족 이야기, 그 뒤로 어떠세요?"처럼 이어 줍니다. 기본은 꺼져 있고, 끄면 기존 동작과 출력이 완전히 같습니다 (`tests/test_memory.py`에서 확인).

```
python main.py --interactive --user room203               # data/memory/room203.json 에 기억 (ID는 가명으로)
python main.py --live --user room203 --memory-dir data/test_memory   # 저장 폴더 지정 (상대 경로는 프로젝트 루트 기준)
python main.py --replay samples/daily_chat.jsonl --user demo
```

```python
from memory import UserMemory
engine = DialogueEngine(memory=UserMemory("room203", "data/memory"))   # memory=None(기본)이면 OFF
```

| 구분 | 내용 |
|---|---|
| 저장하는 것 | 본 파일 `<id>.json`: 화제별 언급 횟수(`topic_counts`: family·health·sleep·weather·lonely), 마지막 화제(`last_topic`)와 그 시각(`last_ts`, 입력 timestamp), 대화 세션 수(`sessions`), **표정 감정 횟수**(`emotion_counts`: happy·sad·angry·fear). 보조 파일 `<id>.recent.json`: 최근 3세션 시작 시점의 화제 횟수(`snapshots`), 지난 화제를 언급한 횟수(`mentions`) |
| 저장하지 않는 것 | **발화 원문**, 이름·주소·병명 등 개인 정보 (화제 라벨과 숫자만 저장) |
| 기록하지 않는 때 | 안전 확인·비상 중(`SAFETY_CHECK`), 안전 전략 응답(`ASK_CHECK`·`REASK`·`ESCALATE`·`RESUME` 등) |
| 언급하는 때 | 새 세션(엔진 생성 = 프로그램 실행 1회, `--interactive`의 `reset` 포함)마다 최대 한 번, 세션 첫 2턴 안의 일상 응답(인사·일반·긍정·화제 응답)에서. 시간·날씨·기능·식사·약 답변 턴은 2턴에 세지 않음. 안전 확인·비상·공감·되묻기가 한 번이라도 나온 세션에서는 언급하지 않음. 인사가 들어간 문장("다시 뵈니 반가워요…")은 세션 첫 발화에서만 |
| 언급하는 화제 | 화제 이야기가 있었던 최근 3세션에서 가장 많이 나온 가족·건강·잠(동률이면 더 최근). 날씨는 그 셋이 하나도 없을 때만. 외로움(lonely)은 기록만 하고 먼저 꺼내지 않음. 화제를 모르면 언급하지 않음. 문장은 화제별 3개를 언급 횟수로 돌려 씀 |
| 언급 뒤 | 같은 화제 대답은 이어가기 응답("가족분 이야기 더 들려주세요"), 건강·잠 언급 뒤 "이제 괜찮아/잘 자"는 "다행이에요" |
| 파일 저장 | 임시 파일(`<id>.json.<임의>.tmp`)에 쓰고 fsync 후 교체. 교체 실패 시 3번 재시도(0.25초 이내), 끝내 실패하면 임시 파일 삭제 + 세션당 경고 한 줄 (대화는 계속) |
| 파일 읽기 | 없으면 빈 기억. 내용이 깨졌으면 원본을 `<id>.json.bak`으로 보존하고 빈 기억. 읽기 자체가 실패(잠김·권한)하면 그 세션은 저장하지 않음(기존 파일 보호). BOM 허용, 잘못된 필드만 기본값으로 |
| 사용자 ID | 영문·숫자·한글·`_`·`-`. 다른 문자가 있거나 80자를 넘으면 해시 8자리를 붙인 파일 이름(서로 겹치지 않음). 빈 ID는 거부(`--user ""`는 경고 후 기억 OFF) |

**개인정보·운영 안내**
- **사용자 ID는 가명으로** 정합니다 (예: `room203`, `user01`). 실명을 쓰지 마세요. ID가 그대로 파일 이름이 되고 실행 화면에도 출력됩니다.
- 저장 내용은 **암호화하지 않은 평문 JSON**입니다. 같은 PC의 다른 사용자도 열어 볼 수 있으니 폴더 접근 권한을 관리하세요. 내용 확인은 파일을 텍스트로 열면 됩니다.
- **표정 감정 횟수**(슬픔·화남·두려움 등)는 정서 상태에 관한 정보입니다. 보호자·기관 환경에서 쓸 때는 기억 기능 사용과 저장 항목에 대해 **본인·보호자의 수집 동의**가 필요할 수 있습니다.
- **보관 기간과 자동 삭제가 없습니다.** 지우기 전까지 계속 쌓입니다 (파일 크기는 화제·감정 종류 수만큼으로 일정).
- **삭제**: 해당 사용자의 `data/memory/<id>.json`, `<id>.recent.json`, `<id>.json.bak`, 남아 있는 `<id>.json.*.tmp`를 함께 지웁니다. 전체 삭제는 `data/memory/` 폴더 삭제.
- **한 사용자 = 한 프로세스**를 가정합니다. 같은 사용자 ID로 프로그램 두 개를 동시에 실행하면 서로 덮어써 기록이 사라질 수 있습니다 (파일 잠금 없음).

한계: 화제 단위로만 기억하고 사실("아들이 다음 주에 온다")이나 누구 이야기였는지는 기억하지 않습니다. 화제 판별은 기존 키워드 규칙(`text_analyzer.TOPICS`)을 그대로 써서 음식·추억 같은 이야기는 화제로 남지 않습니다. 안전 판단(`situation.assess`, 안전 확인, 비상)에는 기억을 쓰지 않습니다.
`DialogueEngine(llm=None)`은 선택적 LLM 보조 자리이며 기본 OFF입니다 (지금은 연결하지 않음, 안전 전략에는 쓰지 않음).

## 테스트

```powershell
python -m pytest -v
```

현재 구현에 대해 **324개 자동 테스트가 통과**합니다.

| 파일 | 개수 | 내용 |
|---|---|---|
| `tests/test_dialogue.py` | 124 | 대화 엔진 세부 동작: 상태·전략, 발화 분석, 위험 판단, 안전 확인, 비상 이후 처리, 낙상 사건 처리, 감사·회복 발화 |
| `tests/test_scenarios.py` | 47 | 생활 상황 시나리오 (정상 생활 오탐 방지, 낙상 흐름, 위기, 정서) |
| `tests/test_integration.py` | 32 | 알림 메시지·채널·발송 관리, TTS 어댑터, 파이프라인, 실시간 러너 |
| `tests/test_alerts.py` | 10 | 알림 JSON 필드, 웹훅(가짜 전송·로컬 HTTP 수신), 외부 채널 성공/실패/미설정, 비블로킹 발송, 30초 재전송 |
| `tests/test_tts.py` | 6 | `on_done` 성공/실패, 이전 방식 TTS 호환, 백그라운드 TTS 비블로킹 |
| `tests/test_voice_deadline.py` | 5 | `voice_unclear` 별칭, 다시 듣기, `response_deadline_sec` |
| `tests/test_live.py` | 15 | `--live` 실행 경로 (가짜 시계 + 짧은 실행 확인, 응답 1회 출력, UTF-8·BOM 파이프 입력, `--linger`, 출력 잠금, Ctrl+C 시 알림 마무리) |
| `tests/test_stability.py` | 4 | 재전송 상한과 `gave_up` 기록, 정의되지 않은 입력 키 1회 경고 |
| `tests/test_input_time.py` | 3 | 밀리초 timestamp 자동 변환과 1회 경고, 초 단위 값 유지 |
| `tests/test_emergency_active.py` | 3 | `emergency_active`가 비상 발생부터 해제 전까지 유지되는지 |
| `tests/test_dialogue_quality.py` | 28 | 응답 반복 선택, 기쁨 먼저 받아주기, 식사·불편감 되묻기 방지, 조심스러운 되묻기 연속 방지, 과거형 긍정·피곤·약·수면, 시간·날씨·기능 질문("알려줘" 오인식 보정 포함), 같은 화제 이어가기, 고인 그리움 (항목마다 안전 입력 회귀 확인) |
| `tests/test_memory.py` | 12 | 장기 기억 저장·불러오기, 깨진 파일 복구, 원문 미저장, `memory=None` 동작 동일, 세션당 1회 언급, 안전·위기에서 미사용, 안전 확인·비상 중 기록 안 함, 사용자 분리, `--user` 옵션 |
| `tests/test_memory_robust.py` | 12 | 장기 기억 읽기 실패·깨진 파일 구분(.bak 보존, 읽기 전용), 읽기 재시도, BOM·부분 복구, 교체 재시도·임시 파일 삭제·세션당 경고, fsync, ID 충돌 방지·길이 제한·빈 ID 거부 |
| `tests/test_memory_recall.py` | 11 | 지난 화제 언급 문장 회전, 첫 2턴·안전/공감 뒤 미언급, 인사 문장은 첫 발화만, 언급 후 이어가기·"다행이에요", 화제별 문장, 최근 3세션 화제 선택, 예전 파일 호환, 안전 흐름 동일, 약 복용 이야기는 건강 화제로 기억하지 않음 |
| `tests/test_replay.py` | 7 | 샘플 형식, `--replay` 재생, 처리 시간 측정 스크립트 실행 |
| `tests/test_main.py` | 5 | `main.py` 실행 모드와 로그 형식, `--spec` 값 목록이 실제 허용 값과 일치 |

시나리오별 판단 이유를 보려면 `python tests/test_scenarios.py`로 직접 실행할 수 있습니다.

## 프로젝트 구조

```
silvercare-dialogue/
├── main.py                  실행 진입점 (시연 / --interactive / --spec / --live / --replay)
├── requirements.txt
├── pytest.ini
├── .env.example             환경변수 예시 (텔레그램·웹훅, 모두 선택)
├── docs/
│   └── INTEGRATION.md         통합 담당자용 요약 (입출력 형식, 연결 예시, 체크리스트)
├── src/
│   ├── dialogue_algorithm.py  DialogueEngine: 상태·전략 결정, 안전 확인, 비상 관리, 응답 생성
│   ├── dialogue_types.py      입력/출력 구조, 상태·전략·위험 등급, Config(기준값·가중치), 입력 정규화·별칭
│   ├── text_analyzer.py       STT 텍스트 → 의미 신호 (위험·부정·감정·화제)
│   ├── situation.py           위험 점수·등급, 정서 판단, 판단 근거
│   ├── context.py             대화 이력, 최근 센서 이벤트, 낙상 사건, 움직임 유효시간
│   ├── responses.py           응답 문장 템플릿
│   ├── memory.py              장기 기억 (사용자별 화제·감정 횟수, 기본 OFF)
│   ├── explain.py             판단 결과를 읽기 쉬운 로그로 변환
│   ├── pipeline.py            SilverCarePipeline, RealtimeRunner, BackgroundWorker
│   ├── notifier.py            보호자 알림 메시지·채널(콘솔·파일·텔레그램·웹훅)·발송 관리, .env 로더
│   ├── tts_adapter.py         TTS 연결 인터페이스
│   ├── realtime_demo.py       가짜 시계 기반 파이프라인 시연 (레거시)
│   └── demo.py                이전 버전 시연 스크립트 (레거시)
├── samples/
│   ├── daily_chat.jsonl       샘플 입력: 일상 대화
│   ├── emotional_sad.jsonl    샘플 입력: 정서 공감
│   ├── fall_no_response.jsonl 샘플 입력: 낙상 → 무응답 → 비상
│   └── webhook_receiver.py    로컬 웹훅 수신 테스트 서버
├── benchmarks/
│   └── measure_latency.py     처리 시간 측정
└── tests/                     자동 테스트 (아래 '테스트 파일별 검증 내용' 표)
```

판단 기준값(대기 시간, 위험 점수 가중치, 유효시간 등)은 모두 `src/dialogue_types.py`의 `Config`에 모여 있으며, `DialogueEngine(config=Config(...))`로 바꿀 수 있습니다.

### 파일 분류와 읽는 순서

| 파일 | 구분 | 연동할 때 수정하나요? |
|---|---|---|
| `src/dialogue_algorithm.py` | 핵심 판단 | 아니오 |
| `src/situation.py` | 핵심 판단 | 아니오 |
| `src/text_analyzer.py` | 핵심 판단 | 아니오 |
| `src/context.py` | 핵심 판단 | 아니오 |
| `src/responses.py` | 핵심 판단 (응답 문장 템플릿) | 아니오 |
| `src/memory.py` | 보조 (장기 기억, 기본 OFF. 일상 응답에만 쓰고 안전 판단에는 쓰지 않음) | 아니오 |
| `src/dialogue_types.py` | 입출력 연결 (입력 정규화·별칭, 출력 구조, `Config`) | 예: 센서·STT·환경음 모듈 연결 시 (라벨 이름이 다르면 `ALIASES`·`VOICE_UNCLEAR_ALIASES`에 별칭 추가) |
| `src/pipeline.py` | 입출력 연결 (`SilverCarePipeline`, `RealtimeRunner`) | 아니오 (호출만 함) |
| `src/tts_adapter.py` | 입출력 연결 (TTS 연결점) | 예: TTS 모듈 연결 시 (보통은 파일 수정 없이 `CallbackTTS(fn)`에 함수를 넘김) |
| `src/notifier.py` | 입출력 연결 (보호자 알림 채널·발송 관리) | 예: UI 모듈(보호자 웹앱) 연결 시 (새 알림 채널이 필요할 때 `Notifier` 하위 클래스 추가. 웹훅만 쓰면 수정 없이 `ALERT_WEBHOOK_URL`) |
| `src/explain.py` | 보조 (판단 결과 로그 출력) | 아니오 |
| `main.py` | 입출력 연결 (실행 진입점: 시연 / `--interactive` / `--spec` / `--live` / `--replay`) | 아니오 |
| `src/realtime_demo.py` | 시연용 — **레거시(삭제 가능)** | 아니오 |
| `src/demo.py` | 시연용 — **레거시(삭제 가능)** | 아니오 |
| `samples/*.jsonl` | 시연용 (샘플 입력) | 아니오 (형식 참고용) |
| `samples/webhook_receiver.py` | 시연용 (로컬 웹훅 수신 테스트 서버) | 아니오 (UI 모듈 연동 시 수신 형식 확인용으로 실행) |
| `benchmarks/measure_latency.py` | 테스트·측정용 | 아니오 |
| `tests/*.py` | 테스트·측정용 | 아니오 |
| `docs/INTEGRATION.md` | 문서 (통합 담당자용 요약) | 아니오 (통합할 때 먼저 읽는 문서) |
| `requirements.txt` | 설정 | 아니오 (새 의존성을 추가할 때만) |
| `pytest.ini` | 설정 | 아니오 |
| `.env.example` | 설정 (환경변수 예시) | 예: 알림 채널 설정 시 (`.env`로 복사해 텔레그램·웹훅 값 입력. `.env`는 Git에 올리지 않음) |
| `.gitignore` | 설정 | 아니오 |
| `.vscode/settings.json` | 설정 (VS Code가 `src/`를 import 경로로 인식) | 아니오 |

레거시로 표시한 근거: 두 파일 모두 다른 코드·테스트에서 import하거나 실행하지 않고 이 README에서만 언급됩니다. 지금도 단독 실행은 되지만 역할이 겹칩니다. `src/demo.py`(대표 상황 출력)는 `python main.py`(시연 모드)와 `--replay`로, `src/realtime_demo.py`(가짜 시계로 파이프라인 전체 시연)는 `--live --linger`와 `--replay`로 대체되었습니다. 또한 `realtime_demo.py`는 실행할 때 `logs/demo_alerts.jsonl`을 지우고 다시 씁니다.

**읽는 순서** (처음 보는 사람용)

1. `main.py` — 실행 모드 5가지가 각각 어떤 부품을 조립하는지 한눈에 보입니다.
2. `src/dialogue_types.py` — 입력(`DialogueInput`)과 출력(`DialogueOutput`)의 실제 필드, 기준값(`Config`)이 모여 있어 연동의 기준이 됩니다.
3. `src/dialogue_algorithm.py` — `DialogueEngine.process()`가 상태·전략·안전 확인·비상을 결정하는 중심입니다.
4. `src/situation.py`와 `src/text_analyzer.py` — 위험 점수와 발화 해석의 근거가 어떻게 계산되는지 봅니다.
5. `src/pipeline.py`, `src/notifier.py`, `src/tts_adapter.py` — 판단 결과가 보호자 알림과 TTS로 나가는 경로와 연결 지점입니다.

**모듈 의존 관계** (화살표: import 하는 쪽 → 되는 쪽)

```
main.py ─┬─► pipeline.py ──────┬─► dialogue_algorithm.py ─┬─► text_analyzer.py
         │                     │                          ├─► situation.py ──► dialogue_types.py
         │                     └─► tts_adapter.py         ├─► context.py
         │                                                ├─► memory.py
         ├─► notifier.py                                  ├─► responses.py
         ├─► tts_adapter.py                               └─► dialogue_types.py
         ├─► explain.py
         ├─► memory.py
         └─► dialogue_algorithm.py

benchmarks/measure_latency.py ─► dialogue_algorithm.py, notifier.py, pipeline.py, tts_adapter.py
samples/webhook_receiver.py   ─► (표준 라이브러리만)
```

`pipeline.py`는 `notifier.py`를 import하지 않고, 알림 담당 객체(`AlertDispatcher`)를 생성자 인자로 받습니다. `notifier.py`, `tts_adapter.py`, `context.py`, `text_analyzer.py`, `responses.py`, `explain.py`, `dialogue_types.py`, `memory.py`는 다른 프로젝트 모듈을 import하지 않습니다.

**테스트 파일별 검증 내용**

| 파일 | 검증 내용 |
|---|---|
| `tests/test_dialogue.py` | 대화 엔진 세부 동작: 상태·전략, 발화 분석, 위험 판단, 안전 확인, 비상 이후 처리, 낙상 사건, 감사·회복 발화 |
| `tests/test_scenarios.py` | 생활 상황 시나리오 47개의 기대 판단 (정상 생활 오탐 방지, 낙상 흐름, 위기, 정서) |
| `tests/test_integration.py` | 알림 메시지·채널·발송 관리, TTS 어댑터, 파이프라인, 실시간 러너 (가짜 채널로) |
| `tests/test_alerts.py` | 알림 JSON 필드, 웹훅(가짜 전송·로컬 HTTP 수신), 외부 채널 성공/실패/미설정, 비블로킹 발송, 30초 재전송 |
| `tests/test_tts.py` | `speak(text, on_done)` 성공/실패 보고, 이전 방식 TTS 호환, 백그라운드 TTS 비블로킹 |
| `tests/test_voice_deadline.py` | `voice_unclear` 별칭과 다시 듣기, `response_deadline_sec`가 엔진 대기 시간과 일치 |
| `tests/test_live.py` | `--live` 실행 경로: timestamp 무시, 잘못된 JSON, EOF·Ctrl+C 종료, `--linger`, UTF-8·BOM 입력, 출력 잠금, 종료 시 알림 마무리 |
| `tests/test_replay.py` | 샘플 입력 형식, `--replay` 가상 시간 재생, 처리 시간 측정 스크립트 실행 |
| `tests/test_stability.py` | 재전송 상한(`MAX_RETRY_ROUNDS`)과 `gave_up` 기록, 정의되지 않은 입력 키 1회 경고 |
| `tests/test_input_time.py` | 밀리초 timestamp 자동 변환과 1회 경고, 초 단위 값 유지 |
| `tests/test_emergency_active.py` | `emergency_active`가 비상 발생부터 해제 전까지 유지되는지 |
| `tests/test_dialogue_quality.py` | 응답 반복 선택, 기쁨 먼저 받아주기, 식사·불편감 되묻기 방지, 조심스러운 되묻기 연속 방지, 과거형 긍정·피곤·약·수면, 시간·날씨·기능 질문("알려줘" 오인식 보정 포함), 같은 화제 이어가기, 고인 그리움 (항목마다 안전 입력 회귀 확인) |
| `tests/test_memory.py` | 장기 기억: 저장·복구·원문 미저장·기본 OFF 동작 동일·세션당 1회·안전 상황 미사용·사용자 분리·`--user` 옵션 |
| `tests/test_memory_robust.py` | 장기 기억 파일 견고성: 읽기 실패·깨진 파일·BOM·부분 복구, 저장 재시도·fsync·경고, 사용자 ID 처리 |
| `tests/test_memory_recall.py` | 장기 기억 언급 품질: 회전·언급 시점·이어가기·화제별 문장·최근 3세션 선택·안전 흐름 동일·약 복용 이야기 제외 |
| `tests/test_main.py` | `main.py` 시연·`--interactive`·`--spec` 실행과 로그 형식, `--spec` 값 목록 = `VALID` |

## 가정 및 통합 시 확인 사항

다른 모듈과의 연결 방식이 확정되기 전이라 아래를 가정해 구현했습니다. 달라지면 표시된 한 곳만 고치면 됩니다.

| 가정 | 현재 구현 | 다르면 고칠 곳 |
|---|---|---|
| 소리 분류기는 "목소리는 들리는데 텍스트 없음"을 `voice_unclear`로 보낸다 | 주 이름 `voice_unclear` + 예비 별칭 3개, `speech` + 빈 텍스트와 같은 처리 | `src/dialogue_types.py`의 `VOICE_UNCLEAR`, `VOICE_UNCLEAR_ALIASES` |
| 그 밖의 입력 라벨·키 이름은 [입력 형식](#입력-형식)과 같다 | 별칭 표로 흡수, 모르는 키는 무시 | `src/dialogue_types.py`의 `ALIASES`, `DialogueInput.from_dict` |
| TTS 모듈은 문장 하나를 받아 **재생이 끝날 때 반환**하는 함수로 연결할 수 있다 | `CallbackTTS(fn)`, 반환 시점에 `on_done(True)` | TTS 모듈 연결 함수 (`CallbackTTS`에 넣는 함수) |
| 재생 중 STT 억제는 TTS/STT 쪽에서 `on_done`(또는 `on_tts_done`)으로 처리한다 | 신호만 제공, 억제 동작은 구현하지 않음 | 통합 코드에서 콜백 연결 |
| 보호자 웹앱은 알림을 HTTP POST JSON으로 받는다 | `WebhookChannel`, 본문 = `logs/alerts.jsonl` 한 줄 | `src/notifier.py`의 `WebhookChannel` / `AlertMessage.to_dict` |
| 알림 JSON의 `status`는 "알림 상태"(raised/updated/realert/resolved/notice)다 | 파일 기록과 웹훅 본문이 같아야 해서, 전송 결과(성공/실패)는 본문이 아니라 `on_delivery`·`AlertDispatcher.history`로 분리 | `src/notifier.py`의 `build_*` |
| 슬픈 표정 직후라도 감사·회복 발화는 평상시 응답을 원한다 (표정은 슬픈데 "기분 좋아"·"괜찮아"는 조심스럽게 되묻기 유지) | 감사(`THANKS`)·회복(`RECOVERY`) 표현이면 표정 감정 이월을 적용하지 않음. 부정 표현이 함께 있으면 공감 유지 | `src/text_analyzer.py`의 `THANKS`·`RECOVERY`, `src/situation.py`의 정서 판단 |
| 기쁨은 화제보다 먼저 받아주는 것이 자연스럽다. 단 "날씨가 좋네" 같은 약한 긍정은 기존 화제 응답이 더 맞다 | 분명한 기쁨 표현(`JOY`) 또는 60초 이내 밝은 표정이면 기쁨 응답(가족·건강은 전용 문장). 인사·감사·취침 등은 그대로 우선 | `src/text_analyzer.py`의 `JOY`, `src/dialogue_algorithm.py`의 `_small_talk_kind` |
| 식사·불편감을 이미 말했으면 같은 내용을 다시 묻지 않는다. "쑤셔·저려·결려" 같은 불편감은 안전 확인 대상이 아니다 | 식사 상태(`ask`/`not`/`done`, "약 먹었어"는 제외)별 응답, 불편감은 받아주는 응답. 통증("아파")의 안전 확인 조건은 바꾸지 않음 | `src/text_analyzer.py`의 `MEAL_ASK`·`MEAL_NOT`·`MEAL_DONE`·`DISCOMFORT` |
| 표정과 말이 다를 때 세 번 이상 되물으면 어르신이 부담을 느낀다 | 조심스러운 되묻기는 연속 2번(`MAX_GENTLE_PROBES`)까지, 그다음은 물러서는 말(`back_off`). 다른 응답이 오면 횟수 초기화. 안전 판단과 무관 | `src/dialogue_algorithm.py`의 `MAX_GENTLE_PROBES` |
| 과거형 긍정("오늘은 좋았어", "재밌었어")은 회복 신호이며, 슬픈 표정 직후라도 슬픔을 반복하지 않는다 | `PAST_POSITIVE`를 긍정·회복 신호로 처리하고 좋았던 일을 받아주는 응답(`good_day`). 부정형("안 좋았어")은 기존 공감 | `src/text_analyzer.py`의 `PAST_POSITIVE` |
| 몇 분 전의 밝은 표정은 지금 일상 응답을 좌우하면 안 된다. 피곤함은 공감이 아니라 쉬라는 말이 자연스럽다 | 일상 '긍정' 응답은 발화가 긍정일 때만(60초 안 밝은 표정은 기쁨 응답). "피곤·지쳤·기운이 없어"는 `tired` 응답, "힘들어"는 기존 공감 유지. `situation.assess`는 수정하지 않음 | `src/dialogue_algorithm.py`의 `_small_talk_kind`, `src/text_analyzer.py`의 `TIRED` |
| "약 먹었어/먹을 시간"은 식사가 아니라 약 복용 이야기다 | `MEDICINE` 표현이면 식사 응답보다 먼저 약 챙김을 칭찬하는 응답(`medicine`). 복약 조언은 하지 않음 | `src/text_analyzer.py`의 `MEDICINE` |
| 잠을 못 잤다는 말은 다시 묻지 않고 받아주며, 수면에 대한 의료 조언은 하지 않는다 | `SLEEP_BAD` 표현이면 `sleep_bad` 응답("낮에 피곤하시겠어요" 정도). 감정 표현("힘들어")이 함께 있으면 기존 공감 | `src/text_analyzer.py`의 `SLEEP_BAD` |
| 시간 질문은 입력 시각(실행 PC 시간대)으로 답하고, 날씨 정보는 연결되어 있지 않으므로 지어내지 않는다 | "몇 시야"는 `timestamp` 기준 "오후 3시 20분", "날씨 어때"는 확인할 수 없다고 솔직히 말함, "뭐 할 줄 알아"는 대화·안부·보호자 알림 소개. "시간/날씨 알려줘"처럼 시간·날씨를 묻는 문장의 "알려~"는 "살려줘" 오인식 보정에서 뺌. 단독 "알려줘"와 "나 좀 알려줘"·"누가 좀 알려줘"는 실제 "살려줘"의 오인식일 수 있어 기존대로 비상 | `src/text_analyzer.py`의 `_question`, `src/dialogue_algorithm.py`의 `korean_time` |
| "같은 화제"는 바로 직전 발화의 첫 번째 화제가 같을 때만으로 본다 | 직전 턴과 화제가 같으면 처음 꺼내는 문장 대신 이어가기 응답(`continue_family` / `continue`). 사이에 다른 이야기가 끼면 다시 처음 문장. 누구(아들·딸)인지는 구분하지 않고 "가족분"으로 말함 | `src/context.py`의 `last_topic`, `src/dialogue_algorithm.py`의 `_small_talk_kind` |
| "보고 싶네/그리워"에 고인 표현(돌아가신·영감·할멈·먼저 간…)이 함께 있으면 고인에 대한 그리움으로 본다 | 일상 응답 대신 그리움을 짚는 공감(`EMPATHIZE`, `TOPIC_EMPATHY["grief"]`). 위기 표현이 섞이면 기존대로 즉시 비상(그리움 처리보다 먼저). 살아 계신 가족("아들이 보고 싶네")은 기존 공감 | `src/text_analyzer.py`의 `MISSING`·`DECEASED` |
| "알려줘"가 "살려줘"의 오인식일 가능성은 정보를 묻는 문장에서만 낮다고 본다 | "시간/날씨 알려줘"처럼 시간·날씨 질문 표현이 함께 있으면 '알려~'를 "살려줘" 오인식 보정에서 뺌. 단독 "알려줘"·"나 좀 알려줘"·"누가 좀 알려줘"는 기존대로 비상 (요청보다 좁게 적용: 다른 말이 붙은 모든 문장에서 빼면 "나 좀 살려줘"의 오인식을 놓칠 수 있음) | `src/text_analyzer.py`의 `_fuzzy_severe` |
| 장기 기억의 '세션'은 엔진 생성 1회(프로그램 실행 1회)로 본다 | 시간 간격으로 세션을 나누지 않음. `--interactive`의 `reset`도 새 세션. 지난 화제는 세션 시작 시점의 기억으로 고정 | `src/dialogue_algorithm.py`의 `DialogueEngine.__init__` |
| 지난 화제 언급은 "첫 일상 대화" 중 답할 내용이 없는 말(인사·일반·긍정·화제)에서만 한다 | 첫 발화가 시간 질문·식사·약 이야기면 그 답을 먼저 하고, 그다음 인사·일반 대화에서 한 번 언급 | `src/dialogue_algorithm.py`의 `RECALL_KINDS` |
| 외로움 화제는 다음 세션에서 먼저 꺼내지 않는다 | 정서적으로 무거운 화제를 로봇이 먼저 꺼내지 않도록 기록만 함. 먼저 말 걸기(PROACTIVE)에는 기억을 쓰지 않음 (선택 사항이라 공감 응답을 건드리지 않음) | `src/memory.py`의 `TOPIC_LABELS` |
| 장기 기억 파일을 "읽지 못함"과 "내용이 깨짐"은 다르게 다룬다 | 없으면 빈 기억. 깨졌거나 JSON 객체가 아니면 원본을 `<id>.json.bak`으로 옮겨 보존하고 빈 기억. 읽기 실패(잠김·권한)는 0.05초 간격으로 2번 더 시도한 뒤 그 세션을 읽기 전용(저장 안 함, 경고 한 줄). BOM 허용. 잘못된 필드(음수·문자열·NaN)는 그 필드만 기본값으로, 나머지는 유지 | `src/memory.py`의 `_load`, `_repair` |
| 장기 기억 저장 실패는 대화를 멈추지 않고 알리기만 한다 | 임시 파일에 쓰고 fsync 후 교체. 교체가 실패하면 0.03·0.07·0.15초 뒤 3번 재시도(합계 0.25초), 끝내 실패하면 임시 파일을 지우고 세션당 첫 1회만 stderr에 경고. 측정: 턴 처리 p95 약 1.8ms(기억 OFF 약 0.3ms) | `src/memory.py`의 `_write` |
| 장기 기억 사용자 ID는 서로 겹치지 않는 파일 이름으로 바꾼다 | 영문·숫자·한글·`_`·`-`만 그대로 쓰고, 다른 문자를 `_`로 바꾼 경우·80자 초과·Windows 예약 이름(CON 등)이면 원래 ID의 해시 8자리를 붙임(`a/b`와 `a_b`는 다른 파일). 빈 ID·공백 ID는 거부하고 `--user`에서는 경고 후 기억 OFF. 이 규칙 이전(511a183)에 특수문자 ID로 저장한 파일은 새 이름으로 찾지 못함 | `src/memory.py`의 `_safe_id`, `main.py`의 `make_memory` |
| 지난 화제 언급은 세션 첫 2턴 안에서만, 문장은 언급 횟수로 회전한다 | 안전 확인·비상(해제 후 포함)·공감·되묻기 응답이 한 번이라도 나오면 그 세션은 언급하지 않음. 인사가 있는 문장은 세션 첫 발화에서만, 그 뒤는 인사 없는 문장. 시간·날씨·기능·식사·약 답변 턴은 2턴에 세지 않음(요청은 "질문 답변 직후 언급 안 함"이었으나 기존 테스트 `test_recall_does_not_replace_answers_with_content`가 "몇 시야 → 약 먹었어 → 안녕"의 3번째 턴 언급을 요구해 이렇게 조정) | `src/dialogue_algorithm.py`의 `_with_memory`, `RECALL_WINDOW`, `RECALL_BLOCKERS` |
| 언급한 화제에 대한 대답은 그 화제를 이어서 받는다 | 언급 화제를 직전 화제로 넣어 같은 화제 대답은 이어가기 응답. 건강·잠 언급 직후 "이제 괜찮아/좋아졌어/잘 자/푹 자"는 "다행이에요" 계열(`relieved`). "잘 자"는 이때만 잘 잔다는 뜻으로 해석 | `src/dialogue_algorithm.py`의 `_with_memory`, `src/text_analyzer.py`의 `SLEEP_WELL` |
| 꺼낼 화제는 "화제 이야기가 있었던 최근 3세션"에서 가장 많이 나온 것, 동률이면 더 최근 것 | 외로움은 건너뜀. 날씨는 가족·건강·잠 후보가 하나도 없을 때만, "그 뒤로" 없는 날씨 전용 문장 사용(요청은 날씨 제외였으나 기존 테스트 `test_user_option_runs_replay_with_memory`가 날씨만 나온 샘플의 두 번째 실행에서 언급을 요구). 세션 경계 기록은 본 JSON이 아닌 보조 파일 `<id>.recent.json`에 둠(기존 테스트가 본 파일의 키 목록을 고정). 보조 파일이 없는 예전 기억은 전체 횟수로 선택 | `src/memory.py`의 `recall_topic`, `src/responses.py`의 `RECALL` |
| 장기 기억은 한 사용자 ID를 한 프로세스만 쓴다고 가정한다 | 파일 잠금은 만들지 않음. 동시에 쓰면 나중에 쓴 쪽이 덮어써 기록이 유실될 수 있음(평가 때 600세션 중 58만 남음). 기관 환경에서는 어르신 한 분당 한 프로세스로 운영 | `src/memory.py` |
| 약 복용 이야기만으로는 장기 기억에 건강 화제로 남기지 않는다 | "약 먹었어", "혈압약 먹었어"처럼 약 복용 표현만 있으면 기억할 화제에서 health를 뺌. 증상·몸 부위·병원 표현(아파·허리·무릎·쑤셔·병원 등)이 함께 있으면 건강으로 기억. 약 복용 응답과 안전 조건은 그대로 | `src/dialogue_algorithm.py`의 `_memory_topics`, `HEALTH_NOT_MEDICINE` |
| UI 카운트다운은 `response_deadline_sec`로 맞춘다 | `ASK_CHECK`·`REASK`·`RETRY_LISTEN`·`CLARIFY` 출력에 대기 시간(초) | `src/dialogue_algorithm.py`의 `_waiting` |
| 실패 알림은 최대 10회 재전송 후 포기하고, 포기 사실은 알림 파일이 아니라 발송 기록·오류 보고로 남긴다 | `MAX_RETRY_ROUNDS = 10`, `gave_up` 목록·`history`·`on_error`. `logs/alerts.jsonl`은 "한 줄 = 알림 JSON" 형식 유지 | `src/notifier.py`의 `MAX_RETRY_ROUNDS` |
| 정의되지 않은 입력 키는 오타·형식 불일치일 수 있으니 알려야 하지만, 매 프레임 반복하면 안 된다 | 같은 키는 프로세스당 처음 한 번만 경고 | `src/dialogue_types.py`의 `KNOWN_INPUT_KEYS` |
| 파이프 입력은 UTF-8이며 PowerShell처럼 첫 줄에 BOM이 붙을 수 있다 | 표준 입력을 `utf-8-sig`로 읽고 줄 앞 BOM 제거 | `main.py`의 `main()`, `parse_live_line` |
| 1e11보다 큰 숫자 timestamp는 밀리초다 (초 단위 현재 시각은 약 1.8e9, 밀리초는 약 1.8e12) | 1000으로 나눠 초로 변환, 프로세스당 한 번만 경고. 1e11 이하는 지금처럼 초로 처리 | `src/dialogue_types.py`의 `MS_TIMESTAMP_THRESHOLD` |
| 시간 기준은 이 모듈이 받은 시각이다 (`--live`) | `--live`는 입력 timestamp를 무시, 일반 API는 입력 timestamp 사용 | 통합 단계에서 timestamp 기준 확정 후 결정 |

통합 단계에서 확인할 것: 소리 분류기의 실제 라벨 이름, STT 결과 전달 방식(키 이름·최종 결과만 보내는지), TTS 함수의 호출 방식과 재생 완료 시점, 보호자 웹앱의 수신 URL과 응답 형식, 센서 전송 주기와 회복 시 `normal` 전송 여부, 팀 전체 시간 기준.

## 알려진 한계

**현재 독립 모듈 범위의 한계**
- 위험 점수와 기준값(대기 시간, 가중치 등)은 프로토타입용 규칙 기반 heuristic이며, 의학적으로 검증된 모델이나 값이 아닙니다.
- 발화 분석은 키워드·규칙 기반이라 일부 자연어 표현에서 오탐/미탐이 있을 수 있습니다 (예: 다른 사람이 주어인 낙상 표현, 일부 간접적 표현).
- 슬픈 표정이 감지된 직후(표정 유효시간 60초 이내)에도 감사·회복 발화("고마워", "좋아졌어", "괜찮아졌어", "풀렸어" 등)에는 평상시 응답을 합니다. 다만 그 밖의 키워드 없는 발화에는 표정 감정이 이어서 반영되어 공감 응답이 나올 수 있고, 감사·회복 표현은 목록(`THANKS`, `RECOVERY`) 기반입니다.
- 실패한 알림은 30초마다 최대 10회 재전송한 뒤 포기(`gave_up`)합니다. 포기 기록은 `logs/alerts.jsonl`이 아니라 발송 기록·오류 보고로만 남습니다.
- `--replay`는 대화 엔진만 실행합니다 (알림·TTS는 실제로 보내지 않음). `--user`를 붙이면 장기 기억 파일이 바뀌므로 같은 파일을 다시 재생해도 첫 인사가 달라질 수 있습니다.
- 장기 기억은 화제 단위만 기억합니다 (사실·원문 기억 없음). 같은 사용자를 두 프로세스가 동시에 쓰면 기록이 유실될 수 있습니다 (한 사용자 = 한 프로세스 가정).

**통합 전 상태**
- 실제 pose·emotion·STT·sound·mmWave 모델/센서는 아직 연결 전이며, 현재는 시연·테스트·`--live`·`--replay` 입력으로 검증합니다.
- 실제 TTS 모델 연결 전입니다. 재생 완료 신호(`on_done`)는 제공하지만, TTS 재생 중 로봇 자기 음성 재인식 방지와 응답 대기 시작 시점 조정은 통합 때 처리합니다.
- 텔레그램은 실제 계정 발송 검증 전입니다. 웹훅은 로컬 수신 서버로만 확인했습니다.
- 여러 PC/장비 간 timestamp 기준은 통합 단계에서 정합니다.

## 팀 통합 시 남은 작업

아래는 이 저장소의 미완성 기능이라기보다, 통합 단계에서 정할 항목입니다.

- 실제 pose / emotion 모델 연결
- 실제 STT 연결
- 실제 sound classifier 연결 (`voice_unclear` 라벨 확인)
- 실제 mmWave 센서 연결 (전송 주기, 회복 시 `normal` 전송 등 규격 확정)
- 모듈 간 데이터 전송 통로 (WebSocket 등)
- 실제 TTS 모델 연결 (`CallbackTTS`, `on_done`)
- 보호자 웹앱 연결 (웹훅 수신 URL, 비상 진행 상태 표시 방식 포함)
- timestamp 기준 확정
- 최종 보호자 알림 채널 결정 및 실제 발송 검증
- 보호자 측 비상 해제(reset) 정책 (향후 확장: 웹앱에서 해제 명령을 보내는 입력. 현재는 미구현)
