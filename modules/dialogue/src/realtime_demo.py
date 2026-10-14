"""전체 흐름 데모: 입력 → 대화 로직 → 음성 출력 + 보호자 알림 (가상 시계로 시간 흐름을 압축해서 보여줌)"""
import os
from dialogue_algorithm import DialogueEngine
from notifier import AlertDispatcher, ConsoleNotifier, FileNotifier
from pipeline import RealtimeRunner, SilverCarePipeline
from tts_adapter import CallbackTTS


class FakeClock:
    t = 0.0
    def __call__(self):
        return self.t


def run(title, events, seconds, log_path):
    print(f"\n{'#' * 8} {title} {'#' * 8}")
    clock = FakeClock()
    stamp = lambda s: print("\n".join(f"[t={int(clock.t):>3}] {line}" for line in s.splitlines()))
    pipeline = SilverCarePipeline(
        DialogueEngine(),
        CallbackTTS(lambda text: stamp(f"🔊 {text}")),
        AlertDispatcher([ConsoleNotifier(stamp), FileNotifier(log_path)], sleep=lambda s: None),
        on_error=lambda m: stamp(f"⚠ {m}"))
    runner = RealtimeRunner(pipeline, clock=clock)
    for t in range(seconds):
        clock.t = t
        if t in events:
            print(f"[t={t:>3}] 📥 입력: {events[t]}")
            runner.feed(events[t])
        runner.poll_once(0)          # 입력이 없으면 1초 tick


if __name__ == "__main__":
    log = "logs/demo_alerts.jsonl"
    if os.path.exists(log):
        os.remove(log)
    run("A. 낙상 → 무응답 → 비상 알림 → 안심 멘트 → 재알림 → 사용자 응답",
        {0: {"pose": "fall", "motion": "still"}, 80: {"stt": "응 괜찮아"}}, 85, log)
    run("B. 낙상 → 사용자가 바로 응답 (알림 없음)",
        {0: {"pose": "fall"}, 3: {"stt": "응 괜찮아"}}, 8, log)
    run("C. 도움 요청 → 즉시 비상", {0: {"stt": "도와줘"}}, 3, log)
    print(f"\n발송 기록 파일: {log}  ({sum(1 for _ in open(log, encoding='utf-8'))}건)")