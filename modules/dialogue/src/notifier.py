"""비상 알림 모듈: 대화 알고리즘의 emergency / notice 출력 → 보호자 메시지 발송

- 채널은 Notifier 하나만 구현하면 갈아끼울 수 있다 (콘솔, 파일 기록, 텔레그램, 웹훅(웹/앱 연동))
- 발송 실패 시 재시도하고, 한 채널이 실패해도 다른 채널은 계속 보낸다
- 같은 알림이 짧은 시간에 중복 발송되지 않게 하고, 정서 알림(notice)은 너무 자주 보내지 않는다
- 콘솔·파일은 로컬 기록일 뿐이므로, 보호자에게 실제로 전달됐는지는 외부 채널 기준으로 따로 본다
  (external_status: sent | failed | unconfigured)
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Callable, Optional


# ───────────── 메시지 ─────────────
@dataclass
class AlertMessage:
    """보호자 웹앱/파일 기록/웹훅이 같은 형태(to_dict)로 받는 알림 한 건"""
    kind: str            # emergency | notice | resolved
    title: str
    body: str
    seq: int = 0         # 이번 비상 사건의 알림 순번 (정서 알림·해제는 0)
    cause: str = ""      # no_response | danger | crisis | emotional
    timestamp: float = 0.0
    status: str = ""     # 알림 상태: raised | updated | realert | resolved | notice

    def text(self) -> str:
        return f"{self.title}\n{self.body}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["time"] = _fmt(self.timestamp)
        return d


CAUSES = {
    "no_response": ("낙상 후 무응답",
                    "낙상이 의심되는 상황 이후 말을 걸었지만 응답이 없고 움직임도 확인되지 않았어요."),
    "danger": ("도움 요청 / 위험 발화",
               "어르신이 도움을 요청하시거나 몸이 많이 불편하다고 말씀하셨어요."),
    "crisis": ("정서적 위기 신호",
               "어르신이 매우 힘들다는 취지의 말씀을 하셨어요."),
}


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "-"


RESPONDED = ("응답 있음 · 안전 미확인",
             "어르신이 말씀은 하셨지만, 아직 괜찮다는 확인은 없어요. 이후 질문에는 대답이 없어요.")


def _label(cause: str, responded: bool):
    if cause == "no_response" and responded:      # 말을 했으면 더 이상 '무응답'이라고 하지 않는다
        return RESPONDED
    return CAUSES.get(cause, CAUSES["no_response"])


def build_emergency(out, now: float) -> AlertMessage:
    label, desc = _label(out.alert_cause, out.user_responded)
    if out.alert_status == "updated":
        origin = _label(out.alert_origin, False)[0]
        title = f"[실버케어 긴급 갱신] {label}"
        head = f"상황이 바뀌었어요: {origin} → {label}. {desc}"
    elif out.alert_seq > 1:
        title = f"[실버케어 긴급 재알림] {label}"
        head = ("어르신이 말씀은 하셨지만 아직 안전이 확인되지 않았어요. 가능하면 직접 연락하거나 방문해 주세요."
                if out.user_responded else
                "아직 어르신의 응답이 확인되지 않았어요. 가능하면 직접 연락하거나 방문해 주세요.")
        if not out.alert_status:                   # 이전 형식(상태 정보 없음)과의 호환
            title = f"[실버케어 긴급 재알림 {out.alert_seq - 1}차] {label}"
    else:
        title, head = f"[실버케어 긴급] {label}", desc
    lines = [head, f"발생 시각: {_fmt(now)}"]
    evidence = (out.assessment or {}).get("evidence") or []
    if evidence:                                   # 최근 근거 우선 (갱신된 정보가 잘리지 않게)
        lines.append("판단 근거: " + " / ".join(evidence[-4:]))
    if out.alert_cause == "crisis":
        lines.append("지금 바로 연락해 주세요. 전문 상담이 필요하면 자살예방상담전화 109(24시간)를 이용할 수 있어요.")
    else:
        lines.append("연락이 닿지 않으면 119 신고를 고려해 주세요.")
    status = out.alert_status or ("realert" if out.alert_seq > 1 else "raised")   # 상태 정보 없는 이전 형식 대비
    return AlertMessage("emergency", title, "\n".join(lines), out.alert_seq, out.alert_cause, now, status)


def build_resolved(out, now: float) -> AlertMessage:
    label = CAUSES.get(out.alert_cause, CAUSES["no_response"])[0]
    lines = ["어르신이 괜찮다고 직접 말씀하셔서 비상 상황을 해제했어요.",
             f"비상 사유: {label}", f"해제 시각: {_fmt(now)}"]
    if out.alert_cause == "crisis":
        lines.append("다만 매우 힘들다는 말씀이 있었으니 꼭 직접 연락해 주세요. "
                     "전문 상담이 필요하면 자살예방상담전화 109(24시간)를 이용할 수 있어요.")
    else:
        lines.append("가능하면 한 번 연락해서 상태를 확인해 주세요.")
    return AlertMessage("resolved", f"[실버케어 비상 해제] {label}", "\n".join(lines), 0, out.alert_cause, now,
                        "resolved")


def build_notice(out, now: float) -> AlertMessage:
    body = f"{out.notice}\n발생 시각: {_fmt(now)}\n가볍게 안부 전화를 해 보시면 좋아요."
    return AlertMessage("notice", "[실버케어 안부 알림]", body, 0, "emotional", now, "notice")


# ───────────── 채널 (Notifier) ─────────────
class Notifier:
    """채널 하나. send()가 예외를 던지면 '실패'로 본다."""
    name = "base"

    def send(self, msg: AlertMessage) -> None:
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    name = "console"

    def __init__(self, printer: Callable[[str], None] = print):
        self.printer = printer

    def send(self, msg: AlertMessage) -> None:
        bar = "=" * 44
        self.printer(f"{bar}\n📨 보호자 알림 발송\n{msg.text()}\n{bar}")


class FileNotifier(Notifier):
    """발송 기록을 JSON Lines로 남긴다 (언제 어떤 사유로 알림이 나갔는지 증빙/디버깅용)."""
    name = "file"

    def __init__(self, path: str = "logs/alerts.jsonl"):
        self.path = path

    def send(self, msg: AlertMessage) -> None:
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg.to_dict(), ensure_ascii=False) + "\n")


def _http_post(url: str, data: dict, timeout: float):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode("utf-8"), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


class TelegramNotifier(Notifier):
    """텔레그램 봇 발송. 토큰/채팅 ID는 코드에 쓰지 말고 .env에서 읽는다."""
    name = "telegram"

    def __init__(self, token: str, chat_id: str, post: Optional[Callable] = None, timeout: float = 5.0):
        self.token, self.chat_id, self.timeout = token, chat_id, timeout
        self.post = post or _http_post

    def send(self, msg: AlertMessage) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        status, _ = self.post(url, {"chat_id": self.chat_id, "text": msg.text()}, self.timeout)
        if status != 200:
            raise RuntimeError(f"telegram HTTP {status}")


def _http_post_json(url: str, payload: dict, timeout: float):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


class WebhookChannel(Notifier):
    """보호자 웹/앱 연동: 알림 JSON(파일 기록 한 줄과 동일한 to_dict())을 URL로 HTTP POST.
    ALERT_WEBHOOK_URL이 설정됐을 때만 기본 dispatcher에 추가된다. 2xx가 아니면 실패(재시도는 dispatcher)."""
    name = "webhook"

    def __init__(self, url: str, post: Optional[Callable] = None, timeout: float = 3.0):
        self.url, self.timeout = url, timeout
        self.post = post or _http_post_json

    def send(self, msg: AlertMessage) -> None:
        status, _ = self.post(self.url, msg.to_dict(), self.timeout)
        if not 200 <= status < 300:
            raise RuntimeError(f"webhook HTTP {status}")


# ───────────── 발송 관리 (재시도 / 중복 방지 / 실패 보관) ─────────────
@dataclass
class Delivery:
    channel: str
    ok: bool
    attempts: int
    error: str = ""
    kind: str = ""
    seq: int = 0
    gave_up: bool = False      # 재전송 상한(MAX_RETRY_ROUNDS)까지 실패해 더 이상 보내지 않음


MAX_RETRY_ROUNDS = 10          # 실패한 알림을 retry_failed()로 다시 보내는 최대 횟수 (넘으면 gave_up)


LOCAL_CHANNELS = ("console", "file")      # 로컬 기록 채널: 성공해도 보호자에게 전달된 것은 아니다


class AlertDispatcher:
    @staticmethod
    def external_status(results) -> str:
        """발송 결과 → 외부 채널(웹훅·텔레그램 등) 기준 상태.
        sent: 하나 이상 성공 / failed: 설정됐지만 전부 실패 / unconfigured: 외부 채널 없음(로컬 기록만)"""
        external = [r for r in results if r.channel not in LOCAL_CHANNELS]
        if not external:
            return "unconfigured"
        return "sent" if any(r.ok for r in external) else "failed"

    def __init__(self, notifiers, max_retries: int = 2, retry_delay: float = 0.5,
                 dedup_window: float = 30.0, notice_cooldown: float = 3600.0,
                 sleep: Callable[[float], None] = time.sleep):
        self.notifiers = list(notifiers)
        self.max_retries, self.retry_delay = max_retries, retry_delay
        self.dedup_window, self.notice_cooldown, self.sleep = dedup_window, notice_cooldown, sleep
        self.history: list = []
        self.failed: list = []                 # (message, notifier, 재전송 횟수) — 재시도 대기
        self.gave_up: list = []                # (message, notifier) — 재전송 상한까지 실패해 포기
        self._recent: dict = {}
        self._last_notice: Optional[float] = None

    def dispatch(self, out, now: float) -> list:
        results = []
        if out.emergency:
            results += self._send(build_emergency(out, now), now)
        if getattr(out, "alert_status", "") == "resolved":
            results += self._send(build_resolved(out, now), now)
            self._recent.clear()                   # 사건 종료: 이후 새 비상은 중복으로 보지 않는다
        if out.notice:
            results += self._send(build_notice(out, now), now)
        return results

    def retry_failed(self) -> list:
        """이전에 실패한 발송을 다시 시도한다 (네트워크 복구 후 등). 한 건당 최대 MAX_RETRY_ROUNDS번."""
        pending, self.failed = self.failed, []
        return [self._deliver(n, m, rounds + 1) for m, n, rounds in pending]

    # --- 내부 ---
    def _send(self, msg: AlertMessage, now: float) -> list:
        if self._duplicate(msg, now):
            return []
        return [self._deliver(n, msg) for n in self.notifiers]

    def _duplicate(self, msg: AlertMessage, now: float) -> bool:
        if msg.kind == "notice":
            if self._last_notice is not None and now - self._last_notice < self.notice_cooldown:
                return True
            self._last_notice = now
            return False
        key = (msg.cause, msg.seq)
        last = self._recent.get(key)
        if last is not None and now - last < self.dedup_window:
            return True
        self._recent[key] = now
        return False

    def _deliver(self, notifier, msg: AlertMessage, rounds: int = 0) -> Delivery:
        err, total = "", self.max_retries + 1
        for attempt in range(1, total + 1):
            try:
                notifier.send(msg)
                d = Delivery(notifier.name, True, attempt, "", msg.kind, msg.seq)
                self.history.append(d)
                return d
            except Exception as e:             # 한 채널의 실패가 다른 채널/시스템을 멈추지 않게
                err = f"{type(e).__name__}: {e}"
                if attempt < total and self.retry_delay:
                    self.sleep(self.retry_delay)
        if rounds >= MAX_RETRY_ROUNDS:         # 재전송 상한 도달: 보관하지 않고 포기로 기록
            d = Delivery(notifier.name, False, total, f"gave_up ({rounds}회 재전송 후): {err}", msg.kind, msg.seq,
                         gave_up=True)
            self.history.append(d)
            self.gave_up.append((msg, notifier))
            return d
        d = Delivery(notifier.name, False, total, err, msg.kind, msg.seq)
        self.history.append(d)
        self.failed.append((msg, notifier, rounds))
        return d


# ───────────── 설정 ─────────────
def load_env(path: str = ".env") -> dict:
    """의존성 없이 .env를 읽는다 (KEY=VALUE). 이미 설정된 환경변수는 덮어쓰지 않는다."""
    loaded = {}
    if not os.path.exists(path):
        return loaded
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            loaded[k] = v
            os.environ.setdefault(k, v)
    return loaded


def build_default_dispatcher(env_path: str = ".env", log_path: str = "logs/alerts.jsonl",
                             printer: Callable[[str], None] = print, **kwargs) -> AlertDispatcher:
    """콘솔 + 파일 기록은 항상, 텔레그램은 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID가 둘 다 있을 때만,
    웹훅(보호자 웹/앱 연동)은 ALERT_WEBHOOK_URL이 있을 때만 추가한다 (.env 또는 환경변수)."""
    load_env(env_path)
    notifiers = [ConsoleNotifier(printer), FileNotifier(log_path)]
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        notifiers.append(TelegramNotifier(token, chat))
    webhook = os.environ.get("ALERT_WEBHOOK_URL")
    if webhook:
        notifiers.append(WebhookChannel(webhook))
    return AlertDispatcher(notifiers, **kwargs)