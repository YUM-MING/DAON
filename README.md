# DAON
[DAON] AI실버케어: 낙상 감지 및 정서 교감 맞춤형 서비스

멀티모달 AI 기반 어르신 낙상 감지 및 케어용 탁상형 반려 로봇 (4인 팀 졸업작품).
전체 설계와 통합 순서는 [TEAM_INTEGRATION_GUIDE.md](TEAM_INTEGRATION_GUIDE.md) 참고.

## 저장소 구조

```
DAON/
├── modules/
│   ├── vision/      # 비전 (이유민) — 포즈·낙상·표정 인식
│   ├── audio_ai/    # 음성 (한정연) — 환경음 분류 + STT
│   └── dialogue/    # 대화·융합·알림 (채혜원)
├── web/             # 웹앱 (김유진) — 로봇 얼굴 + 보호자 대시보드
├── server/          # 통합 서버 (FastAPI + WebSocket 브로커) — 작업 예정
└── TEAM_INTEGRATION_GUIDE.md
```

각 모듈 폴더에는 담당자가 쓴 `README.md` / `INTEGRATION.md` 가 그대로 들어 있다.
**실행에 필요한 파일만** 올려 두었고, 학습 데이터·실험 로그·백업 가중치는 제외했다
(원본 zip은 각 담당자 보관).

## 담당자별 작업 규칙

- 자기 모듈 폴더(`modules/<파트>/`, `web/`)만 수정한다. 다른 모듈은 건드리지 않는다.
- 모듈과 서버 사이 연결은 `server/adapters/` 의 어댑터 한 파일이 담당한다.
  모듈 출력 형식이 바뀌면 해당 어댑터만 고치면 된다.
- 규약(메시지 형식)은 `TEAM_INTEGRATION_GUIDE.md` 3절이 기준. 바꿀 일이 있으면 팀 합의 후 통합자가 수정한다.

## 모듈 단독 실행

| 파트 | 명령 (각 모듈 폴더에서) |
|---|---|
| 비전 | `python inference.py` |
| 음성 | `python inference.py` (마이크), `python inference.py --no-stt --file <wav>` |
| 대화 | `python main.py --replay samples/fall_no_response.jsonl` |
| 웹앱 | `web/index.html` 을 브라우저로 열기 |

Python 3.12 권장. 의존성은 각 모듈의 `requirements.txt` 참고 (통합 venv 구성은 서버 작업과 함께 정리 예정).
