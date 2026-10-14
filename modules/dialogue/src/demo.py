"""터미널 데모: 입력 → 상황 판단 → 상태/전략/응답 출력"""
import json
from dialogue_algorithm import DialogueEngine, DialogueInput, Config


def run(title, steps, cfg=None):
    print(f"\n=== {title} ===")
    eng = DialogueEngine(cfg or Config())
    for d in steps:
        out = eng.process(DialogueInput.from_dict(d))
        shown = {k: v for k, v in d.items() if k != "timestamp"}
        if out.strategy.value == "NONE" and not out.emergency:
            print(f"[t={d['timestamp']:>3}] in : {json.dumps(shown, ensure_ascii=False)}  → ({out.reason})")
            continue
        print(f"[t={d['timestamp']:>3}] in : {json.dumps(shown, ensure_ascii=False)}")
        print(f"        out: {out.state.value} / {out.strategy.value} / {out.response_text!r}"
              f"{'  🚨 EMERGENCY' if out.emergency else ''}")
        print(f"        why: {out.reason}")
        if out.assessment and out.assessment["risk_score"]:
            a = out.assessment
            print(f"        risk: {a['risk_score']} ({a['risk_level']})")
        if out.alert_seq:
            print(f"        📨 알림 순번: {out.alert_seq}{' (재알림)' if out.alert_seq > 1 else ''}")
        if out.warnings:
            print(f"        ⚠ 입력 경고: {out.warnings}")
        if out.notice:
            print(f"        📋 보호자 정서 알림: {out.notice}")


if __name__ == "__main__":
    run("1. 일상 대화", [{"timestamp": 0, "stt": "오늘 날씨가 좋네"}])
    run("2. 슬픔 공감 (표정+발화 일치 → 깊은 공감)",
        [{"timestamp": 0, "emotion": "sad", "stt": "오늘 기분이 별로야"}])
    run("3. 낙상 → 응답 있음(정상)", [{"timestamp": 0, "pose": "fall"}, {"timestamp": 3, "stt": "응 괜찮아"}])
    run("4. 낙상 + 움직임 없음 → 무응답 → 비상 (위험 HIGH라 대기시간 단축)", [
        {"timestamp": 0, "pose": "fall", "motion": "still"},
        {"timestamp": 5}, {"timestamp": 7}, {"timestamp": 14}])
    run("5. 낙상 → '도와줘'", [{"timestamp": 0, "pose": "fall"}, {"timestamp": 4, "stt": "도와줘"}])
    run("6. 부정문: '안 괜찮아' 는 정상이 아님", [{"timestamp": 0, "pose": "fall"}, {"timestamp": 3, "stt": "안 괜찮아"}])
    run("7. 멀티모달 융합: 낙상음 단독은 대기, 직후 누운 자세 + 무동작이면 확인 시작", [
        {"timestamp": 0, "sound_event": "fall_sound"},
        {"timestamp": 3, "pose": "lying", "motion": "still"}])
    run("8. 확인 중 말소리는 났지만 STT 실패 → 무응답으로 보지 않고 다시 듣기", [
        {"timestamp": 0, "pose": "fall"}, {"timestamp": 4, "sound_event": "speech"}])
    run("9. 누운 채 60초 무동작 (낙상 이벤트 없이도 감지)", [
        {"timestamp": 0, "pose": "lying", "motion": "still"}, {"timestamp": 30}, {"timestamp": 61}])
    run("10. 외로움 반복 → 되묻기 → 보호자 정서 알림(비상 아님)", [
        {"timestamp": 0, "stt": "오늘도 혼자라 외로워"},
        {"timestamp": 20, "stt": "아무도 안 와서 쓸쓸해"},
        {"timestamp": 40, "stt": "너무 외롭네"},
        {"timestamp": 60, "stt": "기분이 우울해"},
        {"timestamp": 80, "stt": "오늘도 힘들었어"}])
    run("12. 통증 호소 → 낙상 때와 다른 첫 질문 → 괜찮아 → 복귀", [
        {"timestamp": 0, "stt": "허리가 너무 아파"}, {"timestamp": 3, "stt": "아니 좀 쉬면 괜찮아"}])
    run("13. '방금 넘어졌어' → 낙상 보고도 확인", [{"timestamp": 0, "stt": "방금 넘어졌어"}])
    run("14. 극단적 선택 암시 → 즉시 보호자 연락 (비상)", [{"timestamp": 0, "stt": "더 살기 싫다"}])
    run("15. 부정문: '안 외로워' 는 외로움이 아님", [{"timestamp": 0, "stt": "별로 안 외로워"}])
    run("16. 일상 화제별 반응", [
        {"timestamp": 0, "stt": "안녕"}, {"timestamp": 10, "stt": "밥 먹었어"},
        {"timestamp": 20, "stt": "고마워"}, {"timestamp": 30, "stt": "너 누구야"}, {"timestamp": 40, "stt": "잘 자"}])
    run("17. 같은 말이 반복되지 않는지: 슬픈 발화 6번 연속", [
        {"timestamp": t * 20, "emotion": "sad", "stt": s} for t, s in enumerate(
            ["너무 속상해", "정말 우울해", "기분이 별로야", "마음이 답답해", "짜증이 나", "너무 슬퍼"])])
    run("18. 팀원 모델의 다른 표기/한글 라벨도 받아들이기 + 알 수 없는 값은 경고", [
        {"timestamp": 0, "pose": "FALLING", "motion": "stationary"},
        {"timestamp": 3, "stt": "응 괜찮아", "pose": "jumping"}])
    run("19. 모델 확신도 반영: 낮으면 무시, 낮은 포즈 + 낙상음이 겹치면 교차 확인", [
        {"timestamp": 0, "pose": "fall", "pose_conf": 0.3},
        {"timestamp": 10, "pose": "fall", "pose_conf": 0.6, "sound_event": "fall_sound", "sound_conf": 0.9}])
    run("20. STT 오인식 보정: 낙상 후 '괜찬아'(괜찮아의 오인식)도 정상 응답으로 인식", [
        {"timestamp": 0, "pose": "fall"}, {"timestamp": 3, "stt": "괜찬아"}])
    run("21. 평범한 말을 비상으로 오판하지 않기", [
        {"timestamp": 0, "stt": "도와줘서 고마워"}, {"timestamp": 10, "stt": "오래 살려고 운동해"},
        {"timestamp": 20, "stt": "아무도 도와줄 사람이 없어"}])
    run("22. 비상 알림 이후: 안심 멘트 → 재알림 (응답이 없을 때)", [
        {"timestamp": 0, "pose": "fall", "motion": "still"}, {"timestamp": 7}, {"timestamp": 14},
        {"timestamp": 34}, {"timestamp": 74}, {"timestamp": 94}])
    run("11. 표정은 슬픈데 '괜찮아' 라고 말하는 경우", [
        {"timestamp": 0, "emotion": "sad"}, {"timestamp": 40, "stt": "응 좋아 괜찮아"}])