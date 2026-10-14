"""장기 기억 1단계: 사용자별 '화제 단위' 기억 (기본 OFF, DialogueEngine(memory=UserMemory(...)))

저장하는 것 : 화제별 언급 횟수, 마지막 화제와 마지막 대화 시각, 대화 세션 수, 감정(표정) 횟수
저장하지 않는 것 : 발화 원문, 이름·주소·병명 같은 개인 정보 (화제 라벨과 숫자만 저장)
안전 판단에는 쓰지 않는다. 일상 대화의 첫 인사에서 지난 화제를 한 번 언급하는 데만 쓴다.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import re
import sys
import tempfile
import time
from typing import Optional

TOPIC_LABELS = {"family": "가족", "health": "건강", "sleep": "잠", "weather": "날씨"}   # 다시 언급해도 되는 화제
RECALL_ORDER = ("family", "health", "sleep")   # 먼저 꺼낼 화제 후보. 날씨는 다른 후보가 없을 때만, 외로움은 꺼내지 않음
RECENT_SESSIONS = 3                            # 화제 선택에 쓰는 최근 세션 수
EMOTIONS = ("happy", "sad", "angry", "fear")                                           # 횟수만 센다 (neutral 제외)
READ_RETRIES = 2            # 읽기 실패(파일 잠김 등) 시 재시도 횟수
READ_RETRY_WAIT = 0.05      # 재시도 간격(초)
REPLACE_WAITS = (0.03, 0.07, 0.15)   # 교체 실패(백신·인덱서가 잠깐 잡는 경우) 재시도 간격, 합계 0.25초


def _empty() -> dict:
    return {"topic_counts": {}, "last_topic": None, "last_ts": None, "sessions": 0, "emotion_counts": {}}


def _count_map(v) -> tuple:
    """{라벨: 0 이상 정수}만 남긴다. (결과, 고친 것이 있는지)"""
    if not isinstance(v, dict):
        return {}, True
    ok = {str(k): n for k, n in v.items()
          if isinstance(n, int) and not isinstance(n, bool) and n >= 0}
    return ok, len(ok) != len(v)


def _repair(raw: dict) -> tuple:
    """필드별 검사: 잘못된 필드만 기본값으로, 나머지는 유지 (부분 복구). (data, 고친 것이 있는지)"""
    data, fixed = _empty(), False
    for key in ("topic_counts", "emotion_counts"):
        if key in raw:
            data[key], bad = _count_map(raw[key])
            fixed |= bad
    s = raw.get("sessions", 0)
    if isinstance(s, int) and not isinstance(s, bool) and s >= 0:
        data["sessions"] = s
    else:
        fixed = True
    lt = raw.get("last_topic")
    if isinstance(lt, str) or lt is None:
        data["last_topic"] = lt
    else:
        fixed = True
    ts = raw.get("last_ts")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and math.isfinite(ts):
        data["last_ts"] = float(ts)
    elif ts is not None:
        fixed = True
    return data, fixed


MAX_STEM = 80               # 파일 이름(확장자 포함) 100자 이하가 되도록 (.json.bak 등 포함)
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _safe_id(user_id: str) -> str:
    """파일 이름으로 쓸 수 있게: 허용 문자(영문·숫자·한글·_·-) 밖은 '_'로 바꾸고 (경로 문자 제거: '../x'로 다른
    폴더에 쓰지 않도록), 바꾼 경우·너무 긴 경우·Windows 예약 이름이면 원래 ID의 짧은 해시를 붙여 서로 겹치지 않게 한다."""
    raw = str(user_id)
    if not raw.strip():
        raise ValueError("사용자 ID가 비어 있습니다")
    s = re.sub(r"[^0-9A-Za-z가-힣_-]", "_", raw)
    if s != raw or len(s) > MAX_STEM or s.upper() in RESERVED:
        s = s[:MAX_STEM - 9] + "_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return s


class UserMemory:
    def __init__(self, user_id: str, root) -> None:
        self.user_id = str(user_id)
        self.path = pathlib.Path(root) / f"{_safe_id(user_id)}.json"
        self.recovered = False                   # 파일 내용이 깨져(또는 일부 필드가 잘못되어) 복구했는지
        self.read_only = False                   # 파일을 읽지 못함: 이번 세션은 저장하지 않는다 (기존 파일 보호)
        self._warned = False                     # 경고는 세션당 한 번만
        self.data = self._load()
        # 보조 파일(<id>.recent.json): 최근 세션 시작 시점의 화제 횟수 스냅숏과 언급 횟수 (본 파일 형식은 그대로 유지)
        self.recent_path = self.path.with_name(self.path.stem + ".recent.json")
        self.meta = self._load_meta()
        self._meta_dirty = False

    def _load_meta(self) -> dict:
        meta = {"snapshots": [], "mentions": 0}
        if self.read_only:
            return meta
        try:
            if not self.recent_path.exists():
                return meta
            raw = json.loads(self.recent_path.read_text(encoding="utf-8-sig"))
        except OSError:
            self.read_only = True                # 보조 파일도 읽지 못하면 이번 세션은 저장하지 않는다
            self._warn("기억 보조 파일을 읽지 못해 이번 세션은 기억을 저장하지 않습니다")
            return meta
        except ValueError:
            return meta                          # 보조 정보는 다시 쌓으면 되므로 기본값으로
        if isinstance(raw, dict):
            snaps = raw.get("snapshots")
            if isinstance(snaps, list):
                meta["snapshots"] = [_count_map(x)[0] for x in snaps if isinstance(x, dict)][-RECENT_SESSIONS:]
            n = raw.get("mentions")
            if isinstance(n, int) and not isinstance(n, bool) and n >= 0:
                meta["mentions"] = n
        return meta

    def _warn(self, msg: str) -> None:
        if not self._warned:
            self._warned = True
            print(f"⚠ 장기 기억: {msg} (대화는 계속합니다)", file=sys.stderr)

    # ───── 파일 ─────
    def _read_text(self) -> Optional[str]:
        """파일 내용. 없으면 None. 읽기 실패(잠김·권한)는 짧게 재시도한 뒤 OSError를 그대로 올린다."""
        for attempt in range(READ_RETRIES + 1):
            try:
                if not self.path.exists():
                    return None
                return self.path.read_text(encoding="utf-8-sig")       # BOM이 있어도 읽기 (메모장 저장 등)
            except OSError:
                if attempt == READ_RETRIES:
                    raise
                time.sleep(READ_RETRY_WAIT)

    def _load(self) -> dict:
        try:
            text = self._read_text()
        except OSError as e:
            self.read_only = True                # 읽지 못한 파일은 깨진 것이 아니다: 덮어쓰지 않는다
            self._warn(f"기억 파일을 읽지 못해 이번 세션은 기억을 저장하지 않습니다 ({type(e).__name__})")
            return _empty()
        if text is None:
            return _empty()
        try:
            raw = json.loads(text)
            if not isinstance(raw, dict):
                raise ValueError("JSON 객체가 아님")
        except ValueError:
            self.recovered = True                # 내용이 깨짐: 원본은 .bak으로 보존하고 빈 기억으로 시작
            try:
                os.replace(self.path, self.path.with_name(self.path.name + ".bak"))
            except OSError:
                self.read_only = True            # 보존하지 못하면 덮어쓰지도 않는다
                self._warn("깨진 기억 파일을 백업하지 못해 이번 세션은 저장하지 않습니다")
            return _empty()
        data, repaired = _repair(raw)
        self.recovered = repaired
        return data

    def save(self) -> bool:
        return self._write(self.path, self.data)

    def _write(self, path: pathlib.Path, obj) -> bool:
        """임시 파일에 쓰고 fsync 후 교체 (쓰는 도중 꺼져도 기존 파일이 반쯤 쓰인 채로 남지 않게).
        교체가 실패하면 짧게 재시도, 끝내 실패하면 임시 파일을 지우고 경고 한 줄 (대화는 계속)."""
        if self.read_only:
            return False
        tmp = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            for wait in REPLACE_WAITS + (None,):
                try:
                    os.replace(tmp, path)
                    return True
                except OSError:
                    if wait is None:
                        raise
                    time.sleep(wait)
        except OSError as e:
            if tmp is not None:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            self._warn(f"기억을 저장하지 못했습니다 ({type(e).__name__})")
        return False

    # ───── 세션 / 관찰 / 활용 ─────
    def begin_session(self) -> Optional[str]:
        """새 대화 세션 시작. 최근 3세션에서 꺼낼 화제를 돌려준다 (없으면 None)."""
        topic = self.recall_topic()
        self._warned = False                     # 저장 실패 경고는 세션마다 첫 1회
        self.meta["snapshots"] = (self.meta["snapshots"] + [dict(self.data["topic_counts"])])[-RECENT_SESSIONS:]
        self._meta_dirty = True                  # 보조 파일은 이 세션에 화제가 나오거나 언급할 때 한 번 쓴다
        self.data["sessions"] += 1
        self.save()
        return topic

    def recall_topic(self) -> Optional[str]:
        """최근 3세션(화제 이야기가 있었던 세션 기준, 스냅숏 차이)에서 가장 많이 나온 화제, 동률이면 더 최근에 나온 화제.
        외로움은 건너뛰고, 날씨는 다른 후보가 없을 때만. 스냅숏이 없는 예전 파일은 전체 횟수로 본다."""
        cur = self.data["topic_counts"]
        bounds = self.meta["snapshots"] + [cur]          # 각 세션 시작 시점 횟수 … 현재
        if len(self.meta["snapshots"]) < RECENT_SESSIONS:
            bounds = [{}] + bounds                       # 기록이 적으면 처음부터
        diff = lambda a, b, t: max(0, b.get(t, 0) - a.get(t, 0))
        total = {t: diff(bounds[0], cur, t) for t in TOPIC_LABELS}
        last_seen = {t: max((i for i in range(len(bounds) - 1) if diff(bounds[i], bounds[i + 1], t)), default=-1)
                     for t in TOPIC_LABELS}
        for pool in (RECALL_ORDER, ("weather",)):
            cands = [t for t in pool if total[t] > 0]
            if cands:
                return max(cands, key=lambda t: (total[t], last_seen[t], t == self.data["last_topic"]))
        return None

    def mention_index(self) -> int:
        """이번 언급 문장 번호 (세션이 바뀌어도 같은 문장이 연속되지 않게 언급 횟수로 회전)"""
        return self.meta["mentions"]

    def mark_mentioned(self) -> None:
        self.meta["mentions"] += 1
        self._meta_dirty = False
        self._write(self.recent_path, self.meta)

    def observe(self, topics, emotion: Optional[str], now: float) -> None:
        """화제 라벨과 감정 라벨만 기록한다 (원문 없음). 바뀐 것이 없으면 저장하지 않는다."""
        changed = False
        for t in topics or ():
            self.data["topic_counts"][t] = self.data["topic_counts"].get(t, 0) + 1
            changed = True
        if topics:
            self.data["last_topic"] = topics[0]
            self.data["last_ts"] = now
        if emotion in EMOTIONS:
            self.data["emotion_counts"][emotion] = self.data["emotion_counts"].get(emotion, 0) + 1
            changed = True
        if changed:
            self.save()
        if topics and self._meta_dirty:
            self._meta_dirty = False
            self._write(self.recent_path, self.meta)

    def top_topic(self) -> Optional[str]:
        counts = {k: v for k, v in self.data["topic_counts"].items() if k in TOPIC_LABELS}
        return max(counts, key=counts.get) if counts else None
