"""기분(mood) 추정기 — 순간 표정(expression)을 시간 창에서 집계해 팀 전송용 기분 라벨로 바꾼다.

LABELS.md 4절의 2층 구조:
    1층 expression : 모델 원본 7종 (happy/neutral/sad/angry/anxious/surprised/hurt) — 한 장 단위, 흔들림
    2층 mood       : 8종 (happy/calm/sad/anxious/angry/surprised/tired/unknown) — 30초 창 집계, 대화·웹이 쓰는 값

왜 나누나: 한 장 오인식(특히 무표정→슬픔)이 그대로 로봇 발화로 이어지는 것을 막기 위해.
부정 기분은 (비율 ≥ 0.5) AND (평균 확신 ≥ 0.6) AND (3초 지속) AND (개인 기준선 대비 초과) 를 모두 넘겨야 한다.

이 모듈은 카메라·모델을 모르는 순수 로직이라 합성 표본으로 단위 테스트한다 (tests/test_mood.py).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from .config import MoodConfig

EXPRESSIONS = ("happy", "neutral", "sad", "angry", "anxious", "surprised", "hurt")
MOODS = ("happy", "calm", "sad", "anxious", "angry", "surprised", "tired", "unknown")
NEGATIVE_MOODS = ("sad", "anxious", "angry")

# 1층 → 2층 기본 매핑 (hurt 는 확신에 따라 sad 또는 neutral 로 — MoodEstimator 안에서 처리)
EXPR_TO_MOOD = {
    "happy": "happy", "neutral": "calm", "sad": "sad", "angry": "angry",
    "anxious": "anxious", "surprised": "surprised", "hurt": "sad",
}

# mood → 기존 팀 규약 `emotion.label` (대화 모듈 입력값; LABELS.md 6절). unknown 은 보내지 않음
MOOD_TO_LEGACY = {
    "happy": "happy", "calm": "neutral", "sad": "sad", "anxious": "fear",
    "angry": "angry", "surprised": "surprised", "tired": "neutral", "unknown": None,
}

# 모델 원본 라벨(HSEmotion 8종 / AIHub 7종 / 이전 5종) → expression 7종
RAW_TO_EXPRESSION = {
    "anger": "angry", "angry": "angry", "contempt": "angry", "disgust": "angry",
    "fear": "anxious", "anxious": "anxious",
    "happiness": "happy", "happy": "happy",
    "neutral": "neutral",
    "sadness": "sad", "sad": "sad",
    "surprise": "surprised", "surprised": "surprised",
    "hurt": "hurt",
}


@dataclass
class _Sample:
    ts: float
    expr: str
    conf: float
    face_px: int
    droop: float | None      # 고개 숙임 지표 (코 y - 어깨중심 y) / 어깨폭 — 클수록 고개가 내려감
    eye_closed: bool | None  # 눈 감김 신호 (외부 제공, 없으면 None)


class MoodEstimator:
    def __init__(self, config: MoodConfig | None = None) -> None:
        self.cfg = config or MoodConfig()
        self._samples: deque[_Sample] = deque()
        self._first_ts: float | None = None
        self._last_sample_ts: float | None = None
        self._last_pred_ts: float = -1e9
        self._neg_since: dict[str, float | None] = {m: None for m in NEGATIVE_MOODS}
        self._droop_since: float | None = None
        self.baseline: dict[str, float] | None = None   # expression 비율 (평소 얼굴)
        self.baseline_droop: float | None = None
        self._baseline_buf: list[_Sample] = []
        self.current: dict[str, Any] | None = None       # 마지막 mood_pred
        self.changed: bool = False                        # 마지막 update 에서 라벨이 바뀌었는지

    # ---------- 입력 ----------

    def observe(self, ts: float, raw_label: str, conf: float, face_px: int = 0,
                droop: float | None = None, eye_closed: bool | None = None) -> str:
        """분류 1회 결과를 넣는다. 반환: 정규화된 expression."""
        expr = RAW_TO_EXPRESSION.get(str(raw_label).lower(), "neutral")
        if face_px and face_px < self.cfg.min_face_px:
            return expr                     # 너무 작은 얼굴: 판정에 쓰지 않음 (오래 지속되면 unknown)
        s = _Sample(ts, expr, float(conf), int(face_px), droop, eye_closed)
        self._samples.append(s)
        self._last_sample_ts = ts
        if self._first_ts is None:
            self._first_ts = ts
        self._collect_baseline(s)
        return expr

    def reset_baseline(self) -> None:
        self.baseline, self.baseline_droop, self._baseline_buf, self._first_ts = None, None, [], None

    # ---------- 판정 ----------

    def update(self, now: float, force: bool = False) -> dict[str, Any] | None:
        """pred_interval 마다 mood_pred 를 계산해 반환 (그 외에는 None). self.changed 로 라벨 변화 확인."""
        self.changed = False
        if not force and now - self._last_pred_ts < self.cfg.pred_interval_sec:
            return None
        self._last_pred_ts = now
        self._trim(now)

        prev = self.current["pred"] if self.current else None
        pred, detail = self._decide(now)
        if pred is None:                      # 표본 부족 → 이전 값 유지
            return None
        out = {"pred": pred, "conf": detail.pop("conf"), "detail": detail}
        self.current = out
        self.changed = pred != prev
        return out

    @property
    def legacy_label(self) -> str | None:
        return MOOD_TO_LEGACY.get(self.current["pred"]) if self.current else None

    # ---------- 내부 ----------

    def _trim(self, now: float) -> None:
        keep = now - self.cfg.window_sec
        while self._samples and self._samples[0].ts < keep:
            self._samples.popleft()

    def _collect_baseline(self, s: _Sample) -> None:
        if self.baseline is not None or self._first_ts is None:
            return
        self._baseline_buf.append(s)
        elapsed = s.ts - self._first_ts
        if elapsed >= self.cfg.baseline_sec and len(self._baseline_buf) >= self.cfg.baseline_min_samples:
            self.baseline = self._ratios(self._baseline_buf)
            droops = [b.droop for b in self._baseline_buf if b.droop is not None]
            self.baseline_droop = sorted(droops)[len(droops) // 2] if droops else None
            self._baseline_buf = []

    def _ratios(self, samples) -> dict[str, float]:
        """표현 비율. hurt 는 확신이 hurt_conf_min 미만이면 neutral 로 센다 (무표정과 겹치는 클래스)."""
        n = max(len(samples), 1)
        r = {e: 0.0 for e in EXPRESSIONS}
        for s in samples:
            e = s.expr
            if e == "hurt" and s.conf < self.cfg.hurt_conf_min:
                e = "neutral"
            r[e] += 1.0 / n
        return r

    def _decide(self, now: float) -> tuple[str | None, dict[str, Any]]:
        cfg = self.cfg
        samples = list(self._samples)
        last_px = samples[-1].face_px if samples else 0

        # 얼굴이 한동안 없음 → unknown
        if self._last_sample_ts is None or now - self._last_sample_ts > cfg.unknown_after_sec or not samples:
            if self._last_sample_ts is None and not samples:
                return "unknown", self._detail("unknown", 0.0, 0.0, {}, now, 0)
            if now - (self._last_sample_ts or 0) > cfg.unknown_after_sec:
                return "unknown", self._detail("unknown", 0.0, 0.0, {}, now, last_px)
        if len(samples) < cfg.min_samples:
            return None, {}

        ratio = self._ratios(samples)
        conf_mean = {e: 0.0 for e in EXPRESSIONS}
        cnt = {e: 0 for e in EXPRESSIONS}
        for s in samples:
            e = "neutral" if (s.expr == "hurt" and s.conf < cfg.hurt_conf_min) else s.expr
            conf_mean[e] += s.conf
            cnt[e] += 1
        for e in EXPRESSIONS:
            conf_mean[e] = conf_mean[e] / cnt[e] if cnt[e] else 0.0

        # mood 별 비율 (sad = sad + 고확신 hurt)
        mood_ratio = {
            "sad": ratio["sad"] + ratio["hurt"], "anxious": ratio["anxious"], "angry": ratio["angry"],
            "happy": ratio["happy"], "surprised": ratio["surprised"], "calm": ratio["neutral"],
        }
        mood_conf = {
            "sad": _wavg([(ratio["sad"], conf_mean["sad"]), (ratio["hurt"], conf_mean["hurt"])]),
            "anxious": conf_mean["anxious"], "angry": conf_mean["angry"],
            "happy": conf_mean["happy"], "surprised": conf_mean["surprised"], "calm": conf_mean["neutral"],
        }
        base_ratio = None
        if self.baseline is not None:
            base_ratio = {"sad": self.baseline["sad"] + self.baseline["hurt"],
                          "anxious": self.baseline["anxious"], "angry": self.baseline["angry"]}

        # 1) 부정 기분: 비율·확신·기준선 초과·지속 네 조건
        for m in NEGATIVE_MOODS:
            ok = mood_ratio[m] >= cfg.negative_ratio_min and mood_conf[m] >= cfg.negative_conf_min
            if ok and base_ratio is not None:
                ok = mood_ratio[m] - base_ratio[m] >= cfg.baseline_excess_min
            if ok:
                if self._neg_since[m] is None:
                    self._neg_since[m] = now
            else:
                self._neg_since[m] = None
        sustained = [m for m in NEGATIVE_MOODS
                     if self._neg_since[m] is not None and now - self._neg_since[m] >= cfg.negative_sustain_sec]
        if sustained:
            m = max(sustained, key=lambda k: mood_ratio[k])
            return m, self._detail(m, mood_ratio[m], mood_conf[m], ratio, now, last_px)

        # 2) 놀람: 짧은 창
        recent = [s for s in samples if now - s.ts <= cfg.surprised_window_sec]
        if len(recent) >= cfg.min_samples:
            rr = self._ratios(recent)
            # 짧은 창이라 표본 수와 확신도 같이 본다 (원거리 40px 얼굴에서 conf 0.13 짜리 놀람이 튀는 것 방지)
            if rr["surprised"] >= cfg.surprised_ratio_min and conf_mean["surprised"] >= cfg.negative_conf_min:
                return "surprised", self._detail("surprised", rr["surprised"], conf_mean["surprised"], ratio, now, last_px)

        # 3) 피곤: 눈 감김 비율 또는 고개 숙임 지속 (규칙, 분류기 아님)
        eyes = [s.eye_closed for s in samples if s.eye_closed is not None]
        eye_ratio = sum(eyes) / len(eyes) if eyes else 0.0
        droops = [s.droop for s in samples if s.droop is not None]
        droop_now = sorted(droops)[len(droops) // 2] if droops else None
        drooping = (droop_now is not None and self.baseline_droop is not None
                    and droop_now - self.baseline_droop >= cfg.tired_droop_delta)
        if drooping:
            if self._droop_since is None:
                self._droop_since = now
        else:
            self._droop_since = None
        droop_sustained = self._droop_since is not None and now - self._droop_since >= cfg.tired_sustain_sec
        if mood_ratio["happy"] < cfg.happy_ratio_min and (eye_ratio >= cfg.tired_eye_closed_ratio or droop_sustained):
            c = max(eye_ratio, min(1.0, (droop_now - self.baseline_droop) / cfg.tired_droop_delta * 0.5) if drooping else 0.0)
            d = self._detail("tired", max(eye_ratio, 1.0 if droop_sustained else 0.0), c, ratio, now, last_px)
            d["eye_closed_ratio"], d["head_droop"] = round(eye_ratio, 2), round(droop_now, 3) if droop_now is not None else None
            return "tired", d

        # 4) 기쁨 (비율 + 확신)
        if mood_ratio["happy"] >= cfg.happy_ratio_min and mood_conf["happy"] >= cfg.happy_conf_min:
            return "happy", self._detail("happy", mood_ratio["happy"], mood_conf["happy"], ratio, now, last_px)

        # 5) 기본값: 평온
        return "calm", self._detail("calm", mood_ratio["calm"], mood_conf["calm"], ratio, now, last_px)

    def _detail(self, mood: str, stability: float, conf_mean: float, ratio: dict[str, float],
                now: float, face_px: int) -> dict[str, Any]:
        cfg = self.cfg
        conf = round(min(1.0, stability * conf_mean), 3) if mood not in ("unknown",) else 0.0
        intensity = "high" if conf_mean >= cfg.intensity_high else "mid" if conf_mean >= cfg.intensity_mid else "low"
        top = max(ratio, key=ratio.get) if ratio else None
        delta = None
        if self.baseline is not None and ratio:
            delta = round(0.5 * sum(abs(ratio[e] - self.baseline[e]) for e in EXPRESSIONS), 3)
        return {
            "conf": conf,
            "intensity": intensity,
            "stability": round(stability, 3),
            "baseline_delta": delta,
            "baseline_ready": self.baseline is not None,
            "face_px": face_px,
            "top_expression": top,
            "ratios": {e: round(v, 2) for e, v in ratio.items()} if ratio else {},
            "n_samples": len(self._samples),
        }


def _wavg(pairs: list[tuple[float, float]]) -> float:
    w = sum(p[0] for p in pairs)
    return sum(p[0] * p[1] for p in pairs) / w if w > 0 else 0.0
