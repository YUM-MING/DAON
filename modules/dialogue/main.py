"""실행 진입점: 프로젝트 루트에서 `python main.py` 한 줄로 대화 알고리즘을 확인한다.

  python main.py                 시연 모드: 대표 상황 몇 개를 [입력]→[판단]→[대화] 로그로 보여준다
  python main.py --interactive   직접 입력 모드: 자세/감정/발화를 입력하면서 반응을 본다
  python main.py --spec          다른 모듈과 주고받는 입력/출력 JSON 형식을 보여준다
  python main.py --live          실시간 모드: stdin JSON 한 줄씩 → 실제 시계로 판단·응답·보호자 알림
                 [--linger SEC]  입력이 끝난 뒤에도 SEC초 동안 시간 흐름을 계속 진행 (파이프 입력으로 비상까지 재현)
  python main.py --replay F      재생 모드: JSONL 파일을 timestamp 기준 가상 시간으로 재생 (예: samples/*.jsonl)
  [--user ID] [--memory-dir P]   장기 기억 ON (--interactive/--live/--replay, 기본 OFF, 저장: data/memory/ID.json)
"""
import argparse
import json
import pathlib
import shlex
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from dialogue_algorithm import DialogueEngine, DialogueInput, Strategy   # noqa: E402
from dialogue_types import VALID, VOICE_UNCLEAR_ALIASES                # noqa: E402
from explain import format_turn                                # noqa: E402
from memory import UserMemory                                  # noqa: E402
from notifier import build_default_dispatcher                  # noqa: E402
from pipeline import RealtimeRunner, SilverCarePipeline        # noqa: E402
from tts_adapter import make_tts                               # noqa: E402

DEMO = [
    ("1. 일상 대화", [{"timestamp": 0, "stt": "오늘 날씨가 좋네"}]),
    ("2. 표정과 말이 모두 슬픔 → 깊은 공감", [{"timestamp": 0, "emotion": "sad", "stt": "너무 속상해"}]),
    ("3. 낙상 → 괜찮다고 대답 → 일상 복귀", [
        {"timestamp": 0, "pose": "fall"}, {"timestamp": 3, "stt": "응 괜찮아"}]),
    ("4. 낙상 + 움직임 없음 → 무응답 → 보호자 비상 알림", [
        {"timestamp": 0, "pose": "fall", "motion": "still"}, {"timestamp": 5}, {"timestamp": 7}, {"timestamp": 14}]),
    ("5. 낙상음 단독은 대기, 직후 누운 자세+무동작이 겹치면 확인", [
        {"timestamp": 0, "sound_event": "fall_sound"},
        {"timestamp": 3, "pose": "lying", "motion": "still"}]),
    ("6. 과거 이야기는 안전 확인하지 않음", [{"timestamp": 0, "stt": "어제 넘어졌었어"}]),
    ("7. 극단적 선택 암시 → 확인 없이 즉시 비상", [{"timestamp": 0, "stt": "더 살기 싫다"}]),
]

HELP = """입력 예:  고마워                      (그냥 문장만 치면 어르신이 한 말로 처리)
          pose=fall motion=still
          stt="응 괜찮아"
          emotion=sad stt="너무 속상해"
          pose=fall pose_conf=0.6 sound_event=fall_sound
시간:    기본으로 입력마다 3초가 흐릅니다.   t=20 (그 시각으로 이동)   wait 7 (7초 경과, 입력 없음)
명령:    reset (처음부터)   help   quit
값:      pose=normal|sitting|lying|sleeping|fall   emotion=happy|sad|angry|neutral
         motion=moving|still   sound_event=fall_sound|speech|daily   *_conf=0~1"""


def run_demo():
    for title, steps in DEMO:
        print(f"\n=== {title} ===")
        eng = DialogueEngine()
        for raw in steps:
            out = eng.process(DialogueInput.from_dict(raw))
            print(format_turn(raw, out, raw["timestamp"]))


def run_interactive(memory=None):
    print("대화 알고리즘 직접 입력 모드 (help: 도움말, quit: 종료)\n" + HELP + "\n")
    eng, now = DialogueEngine(memory=memory), 0.0
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in ("quit", "exit", "q"):
            return
        if line == "help":
            print(HELP); continue
        if line == "reset":
            eng, now = DialogueEngine(memory=memory), 0.0
            print("처음부터 다시 시작합니다."); continue
        raw = {}
        try:
            tokens = shlex.split(line)
        except ValueError as e:
            print(f"입력 형식 오류: {e} (따옴표를 확인하세요)"); continue
        if tokens[0] == "wait":
            try:
                now += float(tokens[1])
            except (IndexError, ValueError):
                print("사용법: wait 7"); continue
        else:
            if not any("=" in t for t in tokens):
                tokens = [f"stt={line}"]          # key=value가 하나도 없으면 문장 전체를 발화로 본다
            bad = [t for t in tokens if "=" not in t]
            if bad:
                print(f"'key=value' 형식이 아닙니다: {bad}  (help 참고)"); continue
            for t in tokens:
                k, v = t.split("=", 1)
                if k == "t":
                    try:
                        now = float(v)
                    except ValueError:
                        print("t는 숫자여야 합니다"); raw = None; break
                elif k.endswith("conf"):
                    try:
                        raw[k] = float(v)
                    except ValueError:
                        print(f"{k}는 숫자여야 합니다"); raw = None; break
                else:
                    raw[k] = v
            if raw is None:
                continue
            if not any(t.startswith("t=") for t in tokens):
                now += 3
        raw_in = dict(raw, timestamp=now)
        print(format_turn(raw, eng.process(DialogueInput.from_dict(raw_in)), now))


def show_spec():
    eng = DialogueEngine()
    sample_in = {"timestamp": 0, "pose": "fall", "pose_conf": 0.92, "emotion": "sad", "emotion_conf": 0.8,
                 "stt": None, "sound_event": "fall_sound", "sound_conf": 0.85, "motion": "still"}
    out = eng.process(DialogueInput.from_dict(sample_in))
    print("# 입력 (다른 모듈 → 대화 알고리즘). 없는 값은 null 이거나 생략해도 됩니다.")
    print(json.dumps(sample_in, ensure_ascii=False, indent=2))
    print("\n# 출력 (대화 알고리즘 → TTS / 비상 알림 모듈)")
    print(json.dumps(out.to_dict(), ensure_ascii=False, indent=2))
    print("\n# 값 설명")
    for line in spec_value_lines():
        print(line)
    print("*_conf: 0~1 (95처럼 퍼센트로 와도 0.95로 변환)   timestamp: 초 단위 숫자 또는 ISO 문자열\n"
          "→ TTS에는 response_text만 넘기고, emergency가 true일 때만 비상 알림을 보냅니다.")


SPEC_ALIAS_NOTES = {   # 대표 별칭 안내 (전체 별칭은 src/dialogue_types.py의 ALIASES)
    "pose": "별칭 예: sitting·standing·walking → normal, Fall-Down·falling → fall",
    "sound_event": "voice_unclear 별칭: " + "|".join(VOICE_UNCLEAR_ALIASES),
}


def spec_value_lines() -> list:
    """--spec의 허용 값 목록. dialogue_types.VALID에서 직접 만들어 코드와 어긋나지 않게 한다."""
    lines = []
    for kind in ("pose", "emotion", "sound_event", "motion"):
        line = f"{kind}: " + "|".join(sorted(VALID[kind]))
        if kind in SPEC_ALIAS_NOTES:
            line += f"  ({SPEC_ALIAS_NOTES[kind]})"
        lines.append(line)
    return lines


LIVE_HELP = """실시간 모드: JSON을 한 줄씩 입력하세요. (Ctrl+C 또는 입력 끝(EOF)으로 종료)
  예: {"pose": "fall", "pose_conf": 0.95, "motion": "still"}
      {"stt": "괜찮아", "pose": "lying", "motion": "moving"}
  입력이 없어도 실제 시간이 흘러 응답 대기/재질문/비상/재알림이 진행됩니다.
  live 모드에서는 입력의 timestamp를 무시하고 현재 시각을 씁니다."""


def parse_live_line(line: str):
    """stdin 한 줄 → 입력 dict. 빈 줄은 None, 잘못된 줄은 ValueError.
    live 독립 실행에서는 외부 timestamp를 믿지 않고 제거한다 (RealtimeRunner가 현재 시각을 붙인다)."""
    line = line.lstrip("﻿").strip()          # PowerShell 파이프가 첫 줄에 붙이는 BOM 제거
    if not line:
        return None
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 형식이 아닙니다 ({e.msg}, 위치 {e.pos})") from None
    if not isinstance(raw, dict):
        raise ValueError('JSON 객체 형태({"pose": ...})로 입력하세요')
    raw.pop("timestamp", None)
    return raw


def build_live_runner(root: pathlib.Path = ROOT, clock=time.time, printer=print,
                      background: bool = False, memory=None) -> RealtimeRunner:
    """기존 부품 조립: 대화 엔진 + TTS 연결점 + 기본 보호자 알림(콘솔/파일/.env가 있으면 텔레그램).
    .env와 logs는 실행 위치가 아니라 프로젝트 루트 기준. background=True면 TTS를 별도 스레드에서 실행."""
    dispatcher = build_default_dispatcher(env_path=str(root / ".env"),
                                          log_path=str(root / "logs" / "alerts.jsonl"), printer=printer)
    pipeline = SilverCarePipeline(DialogueEngine(memory=memory), make_tts(printer=printer), dispatcher,
                                  on_error=lambda m: printer(f"⚠ 파이프라인 오류: {m}"), background=background,
                                  on_delivery=lambda status, out: printer(f"  [알림 전달] 외부 채널: {DELIVERY_LABEL[status]}"))
    return RealtimeRunner(pipeline, clock=clock)


DELIVERY_LABEL = {"sent": "전송 성공", "failed": "전송 실패 (30초마다 재전송)",
                  "unconfigured": "미설정 (콘솔·파일 기록만, ALERT_WEBHOOK_URL 또는 텔레그램 설정 필요)"}
LIVE_FLUSH_SECS = 3.0      # 종료할 때 남은 알림 발송·TTS 작업을 기다리는 최대 시간(초)


def locked_printer(printer):
    """여러 스레드(메인 루프·TTS·알림)가 같은 잠금으로 출력해 줄 단위로 섞이지 않게 한다."""
    lock = threading.Lock()

    def out(s):
        with lock:
            printer(s)
    return out


def _finish(runner, printer) -> None:
    """종료 직전: 대기·진행 중인 알림 발송과 TTS를 최대 LIVE_FLUSH_SECS초 기다린다 (비상 직후 종료해도 알림이 나가도록)."""
    try:
        if not runner.pipeline.wait_idle(LIVE_FLUSH_SECS):
            printer(f"⚠ 남은 알림·TTS 작업을 {LIVE_FLUSH_SECS:g}초 안에 마치지 못하고 종료합니다.")
    except KeyboardInterrupt:              # 기다리는 중 Ctrl+C를 한 번 더 누르면 바로 종료
        pass


def run_live(stdin=None, clock=time.time, printer=print, root: pathlib.Path = ROOT,
             linger: float = 0.0, poll_timeout=None, memory=None) -> int:
    """linger: 입력(EOF)이 끝난 뒤에도 이 시간(초) 동안 실시간 tick(대기·재질문·비상·재알림·알림 전송)을 계속한다.
    0이면 입력이 끝나면 남은 작업을 마치고 바로 종료. poll_timeout: 입력 대기 최대 시간(기본 tick 간격 1초, 테스트용)"""
    stdin = stdin or sys.stdin
    printer = locked_printer(printer)        # 메인 루프·TTS·알림 스레드가 같은 잠금으로 출력
    runner = build_live_runner(root, clock, printer, background=True, memory=memory)
    channels = ", ".join(n.name for n in runner.pipeline.dispatcher.notifiers)
    printer(LIVE_HELP)
    printer(f"보호자 알림 채널: {channels}  (기록 파일: {root / 'logs' / 'alerts.jsonl'})\n")
    eof = threading.Event()

    def read_lines():                         # 입력 스레드: 한 줄씩 읽어 공식 경로(feed)로 넘긴다
        try:
            for line in iter(stdin.readline, ""):
                try:
                    raw = parse_live_line(line)
                except ValueError as e:
                    printer(f"⚠ {e} → 이 줄은 건너뜁니다")
                    continue
                if raw is not None:
                    runner.feed(raw)
        finally:
            eof.set()

    threading.Thread(target=read_lines, daemon=True).start()
    try:
        start, lingering_since = clock(), None
        while True:                           # 메인 루프: 입력이 없으면 RealtimeRunner가 tick으로 시간을 진행
            try:
                out = runner.poll_once(poll_timeout)
            except Exception as e:            # 한 입력의 처리 오류로 실시간 모드 전체가 멈추지 않게
                printer(f"⚠ 처리 중 오류: {type(e).__name__}: {e}")
                out = None
            if out is not None and (runner.last_input is not None or out.strategy != Strategy.NONE
                                    or out.emergency or out.alert_status or out.notice):
                # 응답 문장은 TTS 연결점(콘솔 대체: 🔊 줄)이 한 번 출력하므로 로그에서는 생략
                printer(format_turn(runner.last_input or {}, out, clock() - start, show_response=False))
                for line in status_lines(out):
                    printer(line)
            if eof.is_set() and runner.pending() == 0:
                if linger > 0:                        # 입력이 끝나도 linger초 동안 시간 흐름을 계속 진행
                    if lingering_since is None:
                        lingering_since = clock()
                        printer(f"입력이 끝났습니다. {linger:g}초 동안 시간 흐름(대기·재질문·비상·재알림)을 계속 진행합니다. "
                                "(Ctrl+C로 중단)")
                    if clock() - lingering_since < linger:
                        continue
                _finish(runner, printer)             # 남은 TTS·알림 작업을 마치고 종료
                printer("--linger 시간이 끝나 실시간 모드를 종료합니다." if linger > 0
                        else "입력이 끝나 실시간 모드를 종료합니다.")
                return 0
    except KeyboardInterrupt:
        printer("\nCtrl+C: 남은 알림·TTS 작업을 마친 뒤(최대 3초) 실시간 모드를 종료합니다.")
        _finish(runner, printer)
        return 0


def status_lines(out) -> list:
    """로그 뒤에 붙이는 상태 줄: 비상 상태, 안전 확인 응답 대기 시간(UI 카운트다운과 같은 값)"""
    lines = []
    if out.response_deadline_sec is not None:
        lines.append(f"  [대기] 응답 대기 {out.response_deadline_sec:g}초 (response_deadline_sec)")
    if out.emergency or out.alert_status:
        lines.append(f"  [비상] emergency={out.emergency} alert_status={out.alert_status or '-'} "
                     f"cause={out.alert_cause or '-'}")
    return lines


def _meaningful(out, has_input: bool) -> bool:
    return has_input or out.strategy != Strategy.NONE or out.emergency or bool(out.alert_status or out.notice)


def run_replay(path, printer=print, memory=None) -> int:
    """JSONL을 timestamp 순서대로 재생한다. 실제로 기다리지 않고 가상 시간으로 진행하며,
    입력 사이에는 1초 간격 tick을 넣어 응답 대기/재질문/비상/재알림이 시간에 따라 일어나게 한다.
    대화 엔진만 실행한다 (TTS·보호자 알림 발송은 하지 않고 로그에 표시만)."""
    p = pathlib.Path(path)
    if not p.exists() and not p.is_absolute() and (ROOT / p).exists():
        p = ROOT / p                                  # 다른 폴더에서 실행해도 samples/... 경로를 찾는다
    if not p.exists():
        printer(f"⚠ 파일을 찾을 수 없습니다: {path}")
        return 1
    printer(f"재생: {p.name}  (timestamp 기준 가상 시간, 입력 사이 1초 간격 tick)")
    engine, prev, count = DialogueEngine(memory=memory), None, 0
    for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise ValueError("JSON 객체 형태가 아닙니다")
        except ValueError as e:
            printer(f"⚠ {n}번째 줄: JSON 형식이 아닙니다 ({e}) → 건너뜁니다")
            continue
        if raw.get("timestamp") is None:              # 시각이 없으면 직전 입력 1초 뒤로 본다
            raw["timestamp"] = 0.0 if prev is None else prev + 1
        inp = DialogueInput.from_dict(raw)
        now = inp.timestamp
        if prev is not None and now < prev:
            printer(f"⚠ {n}번째 줄: 시각이 이전 입력보다 앞섭니다 ({now} < {prev})")
        t = (prev + 1) if prev is not None else now
        while prev is not None and t < now:          # 입력 사이 가상 tick
            out = engine.process(DialogueInput(timestamp=t))
            if _meaningful(out, False):
                printer(format_turn({}, out, t))
                for s in status_lines(out):
                    printer(s)
            t += 1
        out = engine.process(inp)
        printer(format_turn(raw, out, now))
        for s in status_lines(out):
            printer(s)
        prev, count = now, count + 1
    printer(f"재생 완료: 입력 {count}줄")
    return 0


def make_memory(user=None, memory_dir=None, root: pathlib.Path = ROOT):
    """--user / --memory-dir → UserMemory. 둘 다 없으면 None (장기 기억 OFF, 기본).
    --user만 주면 프로젝트 루트의 data/memory/, 상대 경로 --memory-dir도 프로젝트 루트 기준."""
    if user is None and memory_dir is None:
        return None
    d = pathlib.Path(memory_dir) if memory_dir else root / "data" / "memory"
    if not d.is_absolute():
        d = root / d
    try:
        return UserMemory("default" if user is None else user, d)
    except ValueError as e:                           # 빈 ID·공백 ID: default로 섞지 않고 기억을 끈다
        print(f"⚠ 장기 기억: {e} → 기억 기능을 끄고 실행합니다", file=sys.stderr)
        return None


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")      # Windows 콘솔/리다이렉트에서도 한글·이모지 출력
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8-sig")   # Windows 파이프·리다이렉트 입력(UTF-8 JSON, BOM 포함)도 그대로 읽기
    ap = argparse.ArgumentParser(description="실버케어 교감형 대화 알고리즘")
    ap.add_argument("--interactive", action="store_true", help="직접 입력 모드")
    ap.add_argument("--spec", action="store_true", help="입력/출력 JSON 형식 보기")
    ap.add_argument("--live", action="store_true", help="실시간 모드 (stdin JSON 한 줄씩, 실제 시계)")
    ap.add_argument("--replay", metavar="JSONL", help="JSONL 입력을 timestamp 기준 가상 시간으로 재생")
    ap.add_argument("--linger", type=float, default=0.0, metavar="SEC",
                    help="--live: 입력이 끝난 뒤에도 SEC초 동안 시간 흐름을 계속 진행 (기본 0)")
    ap.add_argument("--user", metavar="ID", help="장기 기억을 켜고 이 사용자 ID로 기억 (기본 OFF)")
    ap.add_argument("--memory-dir", metavar="PATH", help="장기 기억 저장 폴더 (기본 data/memory, 이것만 주면 ID는 default)")
    args = ap.parse_args()
    if args.linger < 0:
        ap.error("--linger는 0 이상이어야 합니다")
    memory = make_memory(args.user, args.memory_dir)
    if memory is not None:
        print(f"장기 기억 ON: 사용자 '{memory.user_id}' → {memory.path}"
              + (" (기존 파일이 깨져 있어 빈 기억으로 시작)" if memory.recovered else ""))
        if not (args.interactive or args.live or args.replay):
            print("(장기 기억은 --interactive / --live / --replay에서만 사용합니다)")
    if args.spec:
        show_spec()
    elif args.interactive:
        run_interactive(memory)
    elif args.live:
        sys.exit(run_live(linger=args.linger, memory=memory))
    elif args.replay:
        sys.exit(run_replay(args.replay, memory=memory))
    else:
        run_demo()


if __name__ == "__main__":
    main()