# 통합 가이드 (silvercare-dialogue)

통합 담당자가 이 문서만 보고 대화·안전 판단 모듈을 붙일 수 있도록 정리한 요약입니다. 자세한 설명은 [README](../README.md)를 보세요.
모든 코드 예시와 명령은 프로젝트 루트에서 Python 3.11·Windows로 실행해 확인했습니다 (런타임은 표준 라이브러리만 사용).

## 1. 한눈에 보는 흐름

```
입력 (JSON 한 건, 약 1초마다)                판단                              출력
 포즈·움직임(mmWave)·표정·환경음·STT 텍스트 → DialogueEngine.process()  →  응답 문장·전략·상태 (DialogueOutput)
 + timestamp(초)                             (위험 점수, 안전 확인,          ├→ TTS 모듈: TTS.speak(response_text, on_done)
                                              비상 판단, 대화 전략)          └→ 보호자 알림: 알림 JSON (콘솔·파일·웹훅·텔레그램)
```
입력이 없어도 대기·재질문·비상·재알림 타이머가 진행되도록 **약 1초마다 빈 입력(tick)** 을 넣어야 합니다 (`SilverCarePipeline.tick(now)`, 또는 `RealtimeRunner`가 자동으로 처리).

## 2. 입력 JSON (`DialogueInput.from_dict`)
모든 필드는 선택입니다. 값이 없거나 `null`이면 "이번에 새 값 없음"으로 봅니다. 라벨은 소문자·`_`로 정규화하고(`Fall-Down` → `fall`), 알 수 없는 값·키는 무시하고 출력의 `warnings`에 남깁니다.

| 필드 | 타입 | 값 / 예시 | 없을 때 동작 |
|---|---|---|---|
| `timestamp` | 숫자(초) 또는 ISO 문자열 | `1760000000`, `"2026-10-12T15:20:00"` | 현재 시각. 1e11보다 큰 숫자는 밀리초로 보고 초로 변환(경고 1회) |
| `pose` | 문자열 | `normal` · `lying` · `sleeping` · `fall` (`sitting`·`standing` 등은 `normal`로 받음) | 새 자세 정보 없음 |
| `motion` | 문자열 | `moving` · `still` · `none` (mmWave) | 정보 없음. 마지막 값은 30초 동안만 유효 |
| `emotion` | 문자열 | `neutral` · `happy` · `sad` · `angry` · `fear` | 새 표정 없음. 마지막 표정은 60초 동안 유효 |
| `sound_event` | 문자열 | `none` · `speech` · `fall_sound` · `daily` · `voice_unclear`(목소리는 들렸지만 STT 실패) | 새 소리 없음 |
| `stt` | 문자열 | `"응 괜찮아"` | 발화 없음 (문자열이 아니면 무시+경고) |
| `pose_conf` · `emotion_conf` · `sound_conf` | 숫자 0~1 (`*_confidence` 키도 허용) | `0.92` | 1.0으로 간주. 0.4 미만 신호는 무시. **1 초과 100 이하는 퍼센트로 봄** (`95` → 0.95, `1.5` → 0.015) |

라벨 별칭은 `src/dialogue_types.py`의 `ALIASES`·`VOICE_UNCLEAR_ALIASES` 한 곳에서 추가합니다.

## 3. 출력 (`DialogueOutput.to_dict()`)
| 필드 | 의미 |
|---|---|
| `response_text` | TTS로 보낼 문장. `null`이면 말하지 않음 |
| `state` | `NORMAL` · `EMOTIONAL` · `SAFETY_CHECK` |
| `strategy` | `NONE` · `SMALL_TALK` · `EMPATHIZE` · `FOLLOW_UP` · `GENTLE_PROBE` · `ASK_CHECK` · `REASK` · `RETRY_LISTEN` · `CLARIFY` · `RESUME` · `ESCALATE` · `REASSURE` |
| `emergency` | 이번 턴에 비상 알림을 보낼 때만 `true` |
| `emergency_active` | 비상 발생부터 해제 전까지 매 턴 `true` (UI의 "비상 중" 표시용) |
| `alert_status` | 바뀐 순간만: `raised`(최초) · `updated`(더 심각한 원인) · `realert`(재알림) · `resolved`(해제), 평소 `""` |
| `alert_seq` / `alert_cause` / `alert_origin` | 이번 사건의 알림 순번 / 현재 원인(`no_response`·`danger`·`crisis`) / 최초 원인 |
| `response_deadline_sec` | 확인 질문(`ASK_CHECK`·`REASK`·`RETRY_LISTEN`·`CLARIFY`) 뒤 대답을 기다리는 초 (보통 10, 위험 HIGH면 6). 그 외 `null` |
| `notice` | 비상은 아닌 보호자 정서 알림 문장 (없으면 `null`) |
| `user_responded`, `reason`, `assessment`, `warnings` | 비상 중 발화 여부, 판단 이유(로그용), 위험 점수·근거, 입력 경고 |

**비상 알림 JSON** (`AlertMessage.to_dict()`, 웹훅 본문 = `logs/alerts.jsonl` 한 줄)
| 필드 | 의미 |
|---|---|
| `kind` | `emergency`(비상) · `resolved`(비상 해제) · `notice`(정서 알림) |
| `status` | `raised` · `updated` · `realert` · `resolved` · `notice` |
| `seq` | 이번 비상 사건의 알림 순번 (해제·정서 알림은 0) |
| `cause` | `no_response` · `danger` · `crisis` · `emotional` |
| `title`, `body` | 보호자에게 보여 줄 제목·본문 (한국어) |
| `timestamp`, `time` | 발생 시각(초) / `"2026-10-12 12:16:52"` 형식 (timestamp가 0이면 `"-"`) |

## 4. 연결 코드 예시 (프로젝트 루트에서 실행)
**① 엔진 직접 호출**
```python
import sys; sys.path.insert(0, "src")          # 프로젝트 루트에서 실행
from dialogue_algorithm import DialogueEngine, DialogueInput

engine = DialogueEngine()
out = engine.process(DialogueInput.from_dict({"timestamp": 0, "pose": "fall", "motion": "still"}))
print(out.state.value, out.strategy.value, out.response_text, out.response_deadline_sec)
out = engine.process(DialogueInput.from_dict({"timestamp": 3, "stt": "응 괜찮아"}))
print(out.state.value, out.strategy.value, out.response_text)
```
출력: `SAFETY_CHECK ASK_CHECK 괜찮으세요? 제 말 들리시면 대답해주세요. 6.0` / `NORMAL RESUME 다행이에요. 많이 놀라셨죠? 천천히 쉬세요, 저는 옆에 있을게요.`

**② TTS 콜백 연결** (`CallbackTTS(fn)`: fn은 재생이 끝나면 반환한다고 가정. 끝나면 `on_done(True)`, 예외면 `on_done(False)`)
```python
import sys; sys.path.insert(0, "src")
from dialogue_algorithm import DialogueEngine
from notifier import AlertDispatcher, ConsoleNotifier
from pipeline import SilverCarePipeline
from tts_adapter import CallbackTTS
def play(text):                                  # TTS 모듈 함수 자리: 재생이 끝나면 반환
    print("재생:", text)
p = SilverCarePipeline(DialogueEngine(), CallbackTTS(play), AlertDispatcher([ConsoleNotifier()]),
                       on_tts_done=lambda ok: print("재생 완료:", ok))
p.handle({"timestamp": 0, "stt": "안녕"})
```
출력: `재생: 안녕하세요! 오늘 컨디션은 어떠세요?` / `재생 완료: True`

**③ 알림 채널 연결** (웹훅 채널을 직접 추가. 수신 서버 `python samples/webhook_receiver.py`를 먼저 실행)
```python
import sys; sys.path.insert(0, "src")
from dialogue_algorithm import DialogueEngine
from notifier import AlertDispatcher, ConsoleNotifier, WebhookChannel
from pipeline import SilverCarePipeline
from tts_adapter import ConsoleTTS
alerts = AlertDispatcher([ConsoleNotifier(), WebhookChannel("http://127.0.0.1:8765/alerts")])
p = SilverCarePipeline(DialogueEngine(), ConsoleTTS(), alerts,
                       on_delivery=lambda status, out: print("외부 채널:", status))
p.handle({"stt": "살려줘"})                      # timestamp를 생략하면 현재 시각
```
출력: 콘솔 알림 → `외부 채널: sent` → 🔊 비상 안내 문장. 수신 서버에는 `[수신] … kind=emergency  status=raised  seq=1  cause=danger`.
코드 없이 연결하려면 환경변수 `ALERT_WEBHOOK_URL`(또는 `.env`)을 설정하고 `python main.py --live`를 실행합니다. 새 채널은 `Notifier`를 상속해 `send(msg)`만 구현하면 됩니다.

## 5. 실행 명령과 기대 결과
| 명령 | 기대 결과 |
|---|---|
| `python main.py --replay samples/fall_no_response.jsonl` | 0초 `ASK_CHECK` → 3초 `RETRY_LISTEN` → 9초 `REASK` → 15초 `ESCALATE`(무응답 비상, `raised`) → 35초 `REASSURE` |
| `python main.py --interactive` | `안녕` 입력 → `SMALL_TALK` "안녕하세요! 오늘 컨디션은 어떠세요?", `pose=fall motion=still` 뒤 `wait 7` → `REASK` |
| `'{"pose": "fall", "motion": "still"}' \| python main.py --live --linger 20` (PowerShell) | 확인 질문 → 재질문 → 무응답 비상 알림 → 20초 뒤 종료. `ALERT_WEBHOOK_URL`을 설정하면 `[알림 전달] 외부 채널: 전송 성공` |
| `python samples/webhook_receiver.py` | `웹훅 수신 대기: http://127.0.0.1:8765/alerts`, 알림이 오면 `[수신] … kind=emergency status=raised …` |
| `python -m pytest -q` | `324 passed` |

## 6. 통합할 때 확인할 것
- [ ] **timestamp 기준**: 모든 모듈이 같은 시계(초 단위)를 쓰는지. `--live`는 입력 timestamp를 무시하고 받은 시각을 씀 → [README 가정 표](../README.md#가정-및-통합-시-확인-사항)
- [ ] **응답 대기 시간**: 질문을 낸 순간부터 `response_deadline_sec`(10초, HIGH 6초)를 셈. TTS 재생 시간이 길면 대기가 줄어듦 → UI 카운트다운도 이 값 사용
- [ ] **TTS 재생 중 입력**: 재생 중 들어온 STT를 막는 기능이 없음. 로봇 자기 음성("괜찮으세요?")을 STT가 다시 받아 적으면 안전 확인이 잘못 해제될 수 있으므로, 재생 중(`on_tts_done` 전) STT는 입력 쪽에서 보내지 않는 것을 권장 → [README 알려진 한계](../README.md#알려진-한계)
- [ ] **입력 주기와 회복 신호**: 약 1초 간격 tick, 일어난 뒤 `pose: normal`을 3초 이상 보내야 낙상 사건이 끝남
- [ ] **라벨 이름**: 소리 분류기의 "목소리는 들렸지만 인식 실패" 라벨, 포즈·표정 라벨이 위 표와 다르면 `ALIASES`에 추가
- [ ] **알림 채널 결정**: 웹훅(UI 모듈 수신 URL·2xx 응답) 또는 텔레그램(`TELEGRAM_BOT_TOKEN`·`TELEGRAM_CHAT_ID`) → [README 알림 연동](../README.md#알림-연동-웹훅), [환경변수](../README.md#환경변수)
- [ ] **실시간 연동은 `background=True`**: 동기 모드는 웹훅이 불통이면 `handle()`이 최대 약 10초 멈춤
- [ ] **비상 해제**: 지금은 어르신의 "괜찮아" 등 명시적 발화로만 해제됨. 보호자 측 해제 입력은 없음 → [README 팀 통합 시 남은 작업](../README.md#팀-통합-시-남은-작업)

## 7. 알려진 한계 (숨기지 않음)
- 발화 분석은 **키워드·규칙 기반**입니다. 일부 표현에서 오탐·미탐이 있습니다 (예: "시험에 떨어졌어"를 낙상으로 판단).
- 위험 점수와 기준값은 프로토타입용 heuristic이며 의학적으로 검증된 값이 아닙니다.
- **TTS 자기 음성 재인식(STT 에코) 방지는 미구현**입니다 (6절 체크리스트 참고).
- **텔레그램 실제 계정 발송은 미검증**입니다. 콘솔·파일·웹훅(로컬 수신 서버)만 실제로 확인했습니다.
- 장기 기억(선택, 기본 OFF)은 **화제 단위**만 기억하며 사실·원문은 기억하지 않습니다. 한 사용자 ID는 한 프로세스만 쓴다고 가정합니다 → [README 장기 기억](../README.md#장기-기억-기본-off)
- 실제 포즈·표정·STT·소리 분류기·mmWave·TTS 모델은 아직 연결 전입니다.
