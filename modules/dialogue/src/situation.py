"""② 현재 상황 판단: 여러 입력을 하나의 '상황 평가'로 종합 (멀티모달 융합 + 모델 확신도 반영)"""
from __future__ import annotations

from dataclasses import dataclass, field

from dialogue_types import RiskLevel


@dataclass
class Situation:
    risk_score: int = 0
    risk_level: RiskLevel = RiskLevel.NONE
    evidence: list = field(default_factory=list)   # 판단 근거 (설명 가능성)
    valence: str = "neutral"                       # negative | neutral | positive
    emotion_intensity: float = 0.0                 # 0.0 ~ 1.0
    trigger: str = ""                              # fall | lying | fall_persist | utterance (안전 확인 원인)
    mismatch: bool = False                         # 표정은 부정, 발화는 괜찮다

    def to_dict(self) -> dict:
        return {"risk_score": self.risk_score, "risk_level": self.risk_level.name,
                "evidence": self.evidence, "valence": self.valence,
                "emotion_intensity": round(self.emotion_intensity, 2),
                "trigger": self.trigger, "mismatch": self.mismatch}


def _level(score: int, has_severe: bool, cfg) -> RiskLevel:
    if has_severe:
        return RiskLevel.CRITICAL
    if score >= cfg.level_high:
        return RiskLevel.HIGH
    if score >= cfg.level_medium:
        return RiskLevel.MEDIUM
    if score >= cfg.level_low:
        return RiskLevel.LOW
    return RiskLevel.NONE


def assess(inp, sig, ctx, cfg) -> Situation:
    now, w = inp.timestamp, cfg.weights
    ev, score = [], 0

    def conf_of(kind, label):
        """융합 창 안 신호의 유효 확신도. 확신도가 기준 미만이면 무시하고 근거에 남긴다."""
        raw = ctx.best(kind, now)
        if raw is None:
            return 0.0
        if raw < cfg.min_confidence:
            ev.append(f"{label} 확신도 낮아 무시({raw:.2f})")
            return 0.0
        return raw

    def f(c):   # 확신도 → 점수 배율 (full_confidence 이상이면 만점, 그 아래는 비례)
        return min(1.0, c / cfg.full_confidence)

    def tag(c):
        return "" if c >= 1.0 else f", 확신도 {c:.2f}"

    # --- 위험도: 신호별 점수(확신도 비례) + 교차 검증 ---
    cp, cs, cl = conf_of("fall_pose", "낙상 포즈"), conf_of("fall_sound", "낙상음"), conf_of("lying_pose", "누운 자세")
    if cp:
        pts = round(w["fall_pose"] * f(cp)); score += pts; ev.append(f"낙상 포즈(+{pts}{tag(cp)})")
    if cs:
        pts = round(w["fall_sound"] * f(cs)); score += pts; ev.append(f"낙상음(+{pts}{tag(cs)})")
    if cp and cs:
        pts = round(w["pose_sound_agree"] * f(min(cp, cs))); score += pts; ev.append(f"포즈·소리 일치(+{pts})")
    elif cs and cl:
        pts = round(w["sound_then_lying"] * f(min(cs, cl))); score += pts; ev.append(f"낙상음 직후 누운 자세(+{pts})")

    if score > 0:  # 낙상 근거가 있을 때만 mmWave 움직임으로 보정
        motion = ctx.motion(now)                  # 받은 적 없거나 오래된 값이면 None
        if motion in ("still", "none") and ctx.last["pose"] == "sleeping":
            # 자는 사람은 원래 움직이지 않으므로 '움직임 없음'을 위험 근거로 쓰지 않고,
            # 소리만으로는 확인하지 않도록 움직임이 있는 경우와 같이 보정한다.
            score += w["moving_after_fall"]
            ev.append(f"자는 중: 소리 단독은 근거 부족({w['moving_after_fall']})")
        elif motion in ("still", "none"):
            score += w["still_after_fall"]; ev.append(f"움직임 없음(+{w['still_after_fall']})")
        elif motion == "moving":
            score += w["moving_after_fall"]; ev.append(f"움직임 있음({w['moving_after_fall']})")
        else:
            score += w["unknown_motion"]; ev.append(f"움직임 정보 없음({w['unknown_motion']})")

    if ctx.lying_still_secs(now) >= cfg.lying_still_secs:
        score += w["prolonged_lying"]
        ev.append(f"누운 채 {int(ctx.lying_still_secs(now))}초 무동작(+{w['prolonged_lying']})")
    fall_persist = ctx.fall_persist_secs(now)               # 움직임이 있어도 일어나지 않았으면 다시 확인
    if fall_persist >= cfg.fall_recheck_secs:
        score += w["prolonged_fall"]
        ev.append(f"확인 후에도 바닥 자세 {int(fall_persist)}초 지속(+{w['prolonged_fall']})")

    has_severe = sig.distress == "severe" or sig.self_harm
    if sig.self_harm:
        score += w["self_harm"]; ev.append("극단적 선택 암시 발화(즉시 비상)")
    elif sig.distress == "severe":
        score += w["utter_severe"]; ev.append("위험 발화(즉시 비상)")
    elif sig.distress == "mild":
        score += w["utter_mild"]; ev.append(f"위험 의심 발화(+{w['utter_mild']})")
    if sig.reports_fall:
        score += w["utter_fall_report"]; ev.append(f"낙상 언급(+{w['utter_fall_report']})")

    score = max(score, 0)
    if not has_severe:
        score = min(score, 99)   # 위험 발화 없이 즉시 비상으로 가지 않음 (먼저 확인)
    level = _level(score, has_severe, cfg)

    trigger = ""
    if cp or cs:
        trigger = "fall"
    elif ctx.lying_still_secs(now) >= cfg.lying_still_secs:
        trigger = "lying"
    elif fall_persist >= cfg.fall_recheck_secs:
        trigger = "fall_persist"
    elif sig.distress == "mild" or sig.reports_fall:
        trigger = "utterance"

    # --- 정서: 표정(확신도 반영) + 발화 융합 ---
    vw = ctx.emotion_conf
    vision_neg = (ctx.last["emotion"] in cfg.negative_emotions
                  and now - ctx.emotion_ts <= cfg.emotion_ttl)
    valence, intensity, mismatch = "neutral", 0.0, False
    if sig.negative:
        valence, intensity = "negative", 0.5
        if vision_neg:
            intensity = 0.5 + 0.2 * vw; ev.append("표정·발화 모두 부정(교차 확인)")
        if sig.intense:
            intensity += 0.15; ev.append("강조 표현(너무/정말 등)")
    elif vision_neg and (sig.thanks or sig.recovered):
        # 표정 감정이 이월돼도 감사·회복 발화는 공감으로 되돌리지 않고 평상시 응답 (부정 표현이 함께 있으면 위 분기)
        valence = "positive"
        ev.append("표정은 부정이지만 감사·회복 발화 → 평상시 응답")
    elif vision_neg:
        valence, intensity = "negative", 0.4 * (0.5 + 0.5 * vw)
        if sig.positive or sig.ok:
            mismatch, intensity = True, 0.35
            ev.append("표정-발화 불일치: 표정은 부정, 발화는 괜찮다")
    elif sig.positive or ctx.last["emotion"] == "happy":
        valence = "positive"
    if valence == "negative":
        intensity = min(1.0, intensity + 0.1 * min(ctx.negative_streak, 3))
        if ctx.negative_streak:
            ev.append(f"부정 발화 연속 {ctx.negative_streak}회 반영")

    return Situation(score, level, ev, valence, intensity, trigger, mismatch)