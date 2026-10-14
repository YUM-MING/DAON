"""한 턴의 처리 결과를 사람이 읽기 쉬운 [입력] [판단] [대화] 로그로 만든다. (시연·디버깅용, 로직에는 영향 없음)"""
from __future__ import annotations

CAUSE_LABEL = {"no_response": "무응답", "danger": "위급 발화", "crisis": "극단적 선택 암시"}


def format_turn(raw: dict, out, t: float | None = None, show_response: bool = True) -> str:
    """raw: 들어온 입력 dict, out: DialogueOutput, t: 표시할 시각(초)
    show_response=False: 응답 문장을 TTS(🔊 줄)가 따로 출력하는 경우 로그에서는 생략 (--live)"""
    shown = {k: v for k, v in raw.items() if k != "timestamp" and v is not None}
    head = f"[t={t:>5.0f}s]" if t is not None else "[입력]"
    lines = []

    # 아무 일도 없는 턴은 한 줄로
    if out.strategy.value == "NONE" and not out.emergency and not out.notice and not shown:
        return f"{head} (입력 없음 · 대기 중)  {out.reason}"

    lines.append(f"{head} 입력: " + (" ".join(f"{k}={v}" for k, v in shown.items()) or "(없음)"))

    a = out.assessment or {}
    ev = a.get("evidence") or []
    if a.get("risk_score") or ev or a.get("valence", "neutral") != "neutral":
        lines.append(f"  [판단] 위험도 {a.get('risk_score', 0)}점 ({a.get('risk_level', 'NONE')})")
        for e in ev:
            lines.append(f"         - {e}")
        if a.get("valence", "neutral") != "neutral":
            lines.append(f"         · 감정 {a['valence']} (강도 {a.get('emotion_intensity', 0)})"
                         + (" · 표정-발화 불일치" if a.get("mismatch") else ""))
    lines.append(f"  [대화] 상태={out.state.value}  전략={out.strategy.value}")
    if out.response_text and show_response:
        lines.append(f"         🔊 \"{out.response_text}\"")
    if out.emergency:
        cause = CAUSE_LABEL.get(out.alert_cause, out.alert_cause)
        again = " (재알림)" if out.alert_seq > 1 else ""
        lines.append(f"  [알림] 🚨 보호자 비상 알림{again} — 사유: {cause}")
    if out.notice:
        lines.append(f"  [알림] 📋 보호자 정서 알림: {out.notice}")
    if out.warnings:
        lines.append(f"  [경고] ⚠ 입력 문제: {out.warnings}")
    lines.append(f"  [사유] {out.reason}")
    return "\n".join(lines)