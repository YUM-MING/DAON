"""워커 프로세스 어댑터: 모듈을 별도 파이썬 프로세스로 띄우고 JSON lines로 주고받는다.

왜 프로세스 분리인가
- 비전(cv2)과 음성(faster-whisper의 av)이 서로 다른 libavdevice를 싣고 있어 한 프로세스에서 충돌 경고가 난다.
- 각 모듈은 자기 루트에서 실행된다고 가정한다(상대경로 ./weights, ./checkpoints) → 워커가 cwd를 맞춘다.
- 모듈 하나가 죽어도 서버는 살아 있고, 워커는 자동 재시작된다.

프로토콜
    워커 stdout : 한 줄에 JSON 하나 {"type": ..., "ts": ..., ...} → 내부 버스에 그대로 발행
    워커 stdin  : 한 줄에 JSON 명령 하나 {"cmd": ...}
    워커 stderr : 로그 (서버 stderr로 그대로 흘림)
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("daon.worker")


class WorkerAdapter:
    restart_delay = 3.0

    def __init__(self, ctx, name: str, script: Path, cwd: Path, args: list[str] | None = None) -> None:
        self.ctx, self.name, self.script, self.cwd, self.args = ctx, name, script, cwd, args or []
        self.proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.restarts = 0

    # ---------------- 수명주기 ----------------
    def start(self) -> "WorkerAdapter":
        self._stop.clear()
        threading.Thread(target=self._supervise, name=f"worker-{self.name}", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._kill()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _spawn(self) -> None:
        cmd = [sys.executable, "-u", str(self.script), *self.args]
        log.info("[%s] spawn: %s (cwd=%s)", self.name, " ".join(cmd), self.cwd)
        self.proc = subprocess.Popen(cmd, cwd=str(self.cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=None, text=True, encoding="utf-8", bufsize=1)

    def _kill(self) -> None:
        p = self.proc
        if p and p.poll() is None:
            try:
                self.send({"cmd": "stop"})
                p.wait(timeout=3)
            except Exception:
                p.kill()

    def _supervise(self) -> None:
        while not self._stop.is_set():
            try:
                self._spawn()
                self._read_loop()
            except Exception:
                log.exception("[%s] worker error", self.name)
            if self._stop.is_set():
                break
            code = self.proc.returncode if self.proc else None
            self.restarts += 1
            log.warning("[%s] exited (code=%s) → %.0f초 후 재시작 (%d회)", self.name, code, self.restart_delay, self.restarts)
            self.ctx.bus.publish("worker_exit", worker=self.name, code=code)
            time.sleep(self.restart_delay)

    def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                log.info("[%s] %s", self.name, line)
                continue
            t = obj.pop("type", None)
            if not t:
                continue
            ts = obj.pop("ts", None)
            self.ctx.bus.publish(t, ts=ts, **obj)

    # ---------------- 명령 ----------------
    def send(self, cmd: dict) -> None:
        p = self.proc
        if not p or p.poll() is not None or not p.stdin:
            return
        with self._lock:
            try:
                p.stdin.write(json.dumps(cmd, ensure_ascii=False) + "\n")
                p.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
