"""서버 테스트 격리: 실제 modules/dialogue/.env · 외부 알림(텔레그램·웹훅) · 실제 LLM · 하드웨어(워커 프로세스)를 쓰지 못하게 막는다.

- 대화 어댑터(server/adapters/dialogue.py)는 만들 때 notifier.load_env 로 modules/dialogue/.env 를 읽고, 텔레그램·웹훅 키가 있으면
  그 채널을 붙인다. 대화 엔진은 DIALOGUE_LLM 이 켜져 있으면 실제 LLM 클라이언트를 만든다.
- 여기서는 (1) load_env 를 아무것도 하지 않는 함수로 바꾸고 (2) 외부 채널·LLM 관련 환경변수를 비우고 DIALOGUE_LLM=off 로 두며
  (3) 대화 어댑터가 만들어질 때 알림 채널이 bus/file(저장소 밖) 뿐이고 LLM 이 꺼져 있는지 단언한다.
- 서버 테스트는 하위 프로세스를 만들지 않는다. 그래도 워커(비전·오디오·레이더) 프로세스가 뜨지 않게 생성 함수를 막는다.
  환경변수는 하위 프로세스에도 상속되므로, 혹시 생기는 하위 프로세스도 같은 빈 값을 받는다.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DIALOGUE_SRC = ROOT / "modules" / "dialogue" / "src"

# (2) 환경변수: 빈 값으로 둔다 (load_env 가 다시 불려도 setdefault 라서 덮어쓰지 못한다)
BLOCKED_ENV = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ALERT_WEBHOOK_URL", "DIALOGUE_LLM_MODEL",
               "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
for _k in BLOCKED_ENV:
    os.environ[_k] = ""
os.environ["DIALOGUE_LLM"] = "off"

# (1) .env 읽기 차단: 서버 어댑터·요약기가 쓰는 것과 같은 notifier 모듈 객체를 고친다
if str(DIALOGUE_SRC) not in sys.path:
    sys.path.insert(0, str(DIALOGUE_SRC))
import notifier as _notifier  # noqa: E402

_notifier.load_env = lambda path=".env": {}

# (3) 대화 어댑터 방어 단언
from server.adapters import dialogue as _dialogue_adapter  # noqa: E402

_orig_init = _dialogue_adapter.DialogueAdapter.__init__
ALLOWED_CHANNELS = {"bus", "file"}


def _guarded_init(self, *args, **kwargs):
    _orig_init(self, *args, **kwargs)
    notifiers = self.pipeline.dispatcher.notifiers
    names = [n.name for n in notifiers]
    assert set(names) <= ALLOWED_CHANNELS, f"테스트에서 외부 알림 채널이 붙음: {names}"
    for n in notifiers:
        path = getattr(n, "path", None)
        if path:
            assert ROOT not in pathlib.Path(path).resolve().parents, f"알림 기록 파일이 저장소 안: {path}"
    assert getattr(self.engine, "_llm", None) is None, "테스트에서 LLM 클라이언트가 만들어짐"


_dialogue_adapter.DialogueAdapter.__init__ = _guarded_init

# 하드웨어·워커 프로세스 차단 (서버 테스트는 워커를 띄우지 않는다. 띄우려 하면 바로 실패)
from server.adapters import worker as _worker  # noqa: E402


def _blocked_spawn(self):
    raise RuntimeError(f"테스트에서 워커 프로세스 실행이 막혀 있습니다: {getattr(self, 'name', '?')}")


_worker.WorkerAdapter._spawn = _blocked_spawn


@pytest.fixture(autouse=True)
def _keep_blocked_env():
    """테스트가 환경변수를 바꿔도 다음 테스트 전에 다시 막는다."""
    for k in BLOCKED_ENV:
        os.environ[k] = ""
    os.environ["DIALOGUE_LLM"] = "off"
    yield
