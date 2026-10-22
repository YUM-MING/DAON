"""공용 타입: 상태/전략/위험도/설정/입출력 구조 + 입력값 정규화(adapter)"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime
from enum import Enum, IntEnum
from typing import Optional
import time


class State(str, Enum):
    NORMAL = "NORMAL"
    EMOTIONAL = "EMOTIONAL"
    SAFETY_CHECK = "SAFETY_CHECK"


class Strategy(str, Enum):
    NONE = "NONE"  # 말하지 않음(대기)
    SMALL_TALK = "SMALL_TALK"  # 일상 대화
    EMPATHIZE = "EMPATHIZE"  # 공감 (강도: light/deep)
    FOLLOW_UP = "FOLLOW_UP"  # 반복되는 감정/주제를 짚어서 되묻기
    GENTLE_PROBE = "GENTLE_PROBE"  # 표정은 어두운데 괜찮다고 할 때 조심스럽게 되묻기
    ASK_CHECK = "ASK_CHECK"  # "괜찮으세요?" 최초 확인
    REASK = "REASK"  # 무응답 → 재질문
    RETRY_LISTEN = "RETRY_LISTEN"  # 소리는 났는데 인식 실패 → 다시 말해달라고 요청
    CLARIFY = "CLARIFY"  # 모호한 응답 → 구체 확인
    RESUME = "RESUME"  # 정상 확인 → 일상 복귀
    ESCALATE = "ESCALATE"  # 비상 알림 단계
    REASSURE = "REASSURE"  # 비상 알림 후 안심시키기


class RiskLevel(IntEnum):
    NONE = 0
    LOW = 1
    MEDIUM = 2  # 안전 확인 시작
    HIGH = 3  # 안전 확인 + 대기시간 단축
    CRITICAL = 4  # 즉시 비상 (위험 발화)


@dataclass
class Config:
    # 안전 확인
    response_timeout: float = 10.0  # "괜찮으세요?" 후 응답 대기(초)
    high_risk_timeout_ratio: float = 0.6  # HIGH 위험일 때 대기시간 배율
    max_asks: int = 2  # 최초 질문 포함 총 질문 횟수
    max_listen_retries: int = 2  # 인식 실패 시 다시 듣기 최대 횟수
    # 비상 이후
    escalated_prompt_gap: float = 20.0  # 비상 후 안심 멘트 간격(초)
    realert_after: float = 60.0  # 비상 후 이 시간마다 응답이 없으면 재알림(초)
    max_realerts: int = 2
    # 멀티모달 융합
    fusion_window: float = 15.0  # 이 시간 안에 들어온 신호끼리 교차 검증(초)
    lying_still_secs: float = 60.0  # 누운 채 움직임 없음 → 의심 기준(초)
    motion_ttl: float = 30.0  # mmWave 값 유효시간(초). 이보다 오래된 값은 '정보 없음'으로 본다
    fall_repeat_gap: float = 30.0  # 바닥 자세(fall/lying)가 이 간격 안에 이어서 들어오면 같은 낙상 사건으로 본다(초)
    recover_secs: float = 3.0  # 낙상 후 normal 자세가 이 시간 이상 이어져야 '일어남'으로 본다(초, 프로토타입 기본값)
    fall_recheck_secs: float = 120.0  # 확인 후에도 바닥 자세가 이 시간 이상 이어지면 다시 확인(초, 프로토타입 기본값)
    # 소리 단독 충격 (D): 낙상음만 있고 바닥 자세(fall/lying) 근거가 없을 때
    sound_pose_fresh_secs: float = 10.0  # 낙상음 전후 이 시간 안의 정상 자세 관측만 '가벼운 확인' 근거로 인정(초). 무응답 종료 때도 마지막 정상 관측이 이 안이어야 함
    sound_evidence_hold_secs: float = 60.0  # 소리 단독 확인 중·종료 뒤 이 시간 안에 바닥 자세가 오면 소리 근거를 합쳐 기존 낙상 경로로 승격(초)
    sound_recheck_cooldown_secs: float = 60.0  # 소리 단독 확인이 괜찮다는 대답·보호자 해제로 끝난 뒤, 정상 자세가 보이는 소리 단독 재확인을 보류(초)
    lying_recheck_after_ok_secs: float = 300.0  # 누움 확인에서 괜찮다고 한 뒤 다음 누움 무동작 재확인까지(초)
    sound_dup_window_secs: float = 5.0  # 안전 확인 시작 직후 이 시간 안에 따로 도착한 낙상 자세 근거는 질문을 다시 하지 않고 단계·시한만 조정(초)
    # 선택적 LLM (환경변수 DIALOGUE_LLM으로만 켜짐, 일상·공감 문장에만)
    llm_max_chars: int = 90  # LLM 문장 최대 길이(자). 넘으면 템플릿
    llm_min_hangul: float = 0.6  # LLM 문장의 한글 비율 하한
    llm_history_turns: int = 8  # 프롬프트에 넣는 최근 대화 턴 수
    llm_breaker_failures: int = 3  # 연속 실패 횟수 → 서킷 브레이커
    llm_breaker_secs: float = 60.0  # 서킷 브레이커가 열려 있는 시간(초, 그동안 템플릿만)
    emotion_ttl: float = 60.0  # 표정 감정 이벤트 유효시간(초)
    min_confidence: float = 0.4  # 모델 확신도가 이보다 낮은 신호는 무시
    full_confidence: float = 0.9  # 이 이상이면 확신도 100%와 같은 점수
    # 위험 점수 가중치
    weights: dict = field(
        default_factory=lambda: {
            "fall_pose": 50,
            "fall_sound": 35,
            "pose_sound_agree": 15,
            "sound_then_lying": 15,
            "still_after_fall": 20,
            "moving_after_fall": -5,
            "unknown_motion": -5,  # 움직임 정보 없음: 단독 신호로는 확인하지 않도록 소폭 감점 (안전 해제 근거로는 쓰지 않음)
            "prolonged_lying": 45,
            "prolonged_fall": 45,  # 확인 후에도 낙상(바닥) 자세가 오래 이어짐
            "utter_severe": 100,
            "utter_mild": 45,
            "utter_fall_report": 40,
            "self_harm": 100,
        }
    )
    level_low: int = 20
    level_medium: int = 35
    level_high: int = 70
    # 정서/맥락
    negative_emotions: tuple = ("sad", "angry", "fear")
    proactive_gap: float = 30.0  # AI가 먼저 말 건 뒤 최소 간격(초)
    streak_for_followup: int = 3  # 부정 발화 연속 N회 → 되묻기
    topic_repeat_for_followup: int = 3  # 같은 주제 호소 N회 → 되묻기
    recent_window: int = 6  # 최근 N개 응답과 같은 문장은 피함
    streak_for_notice: int = 5  # 부정 발화 연속 N회 → 보호자 정서 알림(비상 아님)
    # 일상 통증 (위험 신호 없는 "머리가 좀 아프네")
    symptom_repeat_secs: float = 600.0  # 같은 증상을 이 시간 안에 다시 호소하면 안전 확인(초)
    pain_question_secs: float = 60.0  # "견딜 만하세요?" 질문에 대한 대답으로 보는 시간(초)


# ───────────── 입력값 정규화 ─────────────
VALID = {
    "pose": {"normal", "lying", "sleeping", "fall"},
    "emotion": {"neutral", "happy", "sad", "angry", "fear"},
    "sound_event": {"none", "speech", "fall_sound", "daily", "voice_unclear"},
    "motion": {"moving", "still", "none"},
}
ALIASES = {  # 인식 모듈(포즈·표정·환경음 등)이 다른 표기로 보내도 받아들인다
    "pose": {
        "falling": "fall",
        "fall_down": "fall",
        "falldown": "fall",
        "fallen": "fall",
        "down": "fall",
        "낙상": "fall",
        "쓰러짐": "fall",
        "넘어짐": "fall",
        "lie": "lying",
        "laying": "lying",
        "lying_down": "lying",
        "누움": "lying",
        "누운": "lying",
        "sleep": "sleeping",
        "asleep": "sleeping",
        "수면": "sleeping",
        "자는중": "sleeping",
        "stand": "normal",
        "standing": "normal",
        "sit": "normal",
        "sitting": "normal",
        "walk": "normal",
        "walking": "normal",
        "idle": "normal",
        "정상": "normal",
        "서있음": "normal",
    },
    "emotion": {
        "sadness": "sad",
        "슬픔": "sad",
        "우울": "sad",
        "happiness": "happy",
        "joy": "happy",
        "기쁨": "happy",
        "행복": "happy",
        "anger": "angry",
        "분노": "angry",
        "화남": "angry",
        "fearful": "fear",
        "scared": "fear",
        "두려움": "fear",
        "공포": "fear",
        "불안": "fear",
        "surprise": "neutral",
        "surprised": "neutral",
        "중립": "neutral",
        "평온": "neutral",
        # 비전 mood 8종 (LABELS.md 4.2·6절, vision/src/mood.py MOOD_TO_LEGACY와 같은 매핑)
        "calm": "neutral",
        "anxious": "fear",
        "hurt": "sad",      # 1층 expression '상처' → 2층 mood에서 sad로 합침
        "tired": "neutral",  # 임시: 정식 값으로 넣을지는 추후 결정 (LABELS.md 6절)
    },
    "sound_event": {
        "fall": "fall_sound",
        "falling": "fall_sound",
        "impact": "fall_sound",
        "낙상음": "fall_sound",
        "voice": "speech",
        "talk": "speech",
        "talking": "speech",
        "conversation": "speech",
        "대화": "speech",
        "음성": "speech",
        "noise": "daily",
        "ambient": "daily",
        "일상음": "daily",
        "silence": "none",
        "silent": "none",
    },
    "motion": {
        "move": "moving",
        "movement": "moving",
        "active": "moving",
        "움직임": "moving",
        "stationary": "still",
        "static": "still",
        "idle": "still",
        "정지": "still",
        "미동": "still",
        "absent": "none",
        "empty": "none",
        "없음": "none",
    },
}


# 소리 분류기: "사람 목소리는 감지됐지만 STT 텍스트로 바꾸지 못함". 주 이름 + 예비 별칭.
# 실제 이름이 확정되면 여기만 고친다. 안전 확인 중에는 speech + 빈 텍스트와 같이 '다시 듣기'로 처리한다.
VOICE_UNCLEAR = "voice_unclear"
VOICE_UNCLEAR_ALIASES = ("unclear_voice", "unclear_speech", "speech_unclear")
SPEECH_EVENTS = ("speech", VOICE_UNCLEAR)          # 발화는 감지됨 (텍스트가 비어 있으면 무응답이 아님)
ALIASES["sound_event"].update({a: VOICE_UNCLEAR for a in VOICE_UNCLEAR_ALIASES})


KNOWN_INPUT_KEYS = frozenset({"timestamp", "pose", "emotion", "stt", "sound_event", "motion",
                              "pose_conf", "pose_confidence", "emotion_conf", "emotion_confidence",
                              "sound_conf", "sound_confidence"})
_WARNED_KEYS: set = set()          # 이미 경고한 알 수 없는 입력 키 (매 프레임 같은 경고 반복 방지)
# timestamp는 초 단위. 이 값보다 크면 밀리초로 보고 초로 바꾼다 (초 단위 현재 시각 ≈ 1.8e9, 밀리초 ≈ 1.8e12)
MS_TIMESTAMP_THRESHOLD = 1e11
_WARNED_MS_TIMESTAMP = False       # 밀리초 변환 경고는 프로세스당 한 번만


def _norm(kind: str, value, warnings: list) -> Optional[str]:
    if value is None or str(value).strip() == "":
        return None
    v = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    if v in VALID[kind]:
        return v
    if v in ALIASES[kind]:
        return ALIASES[kind][v]
    warnings.append(f"알 수 없는 {kind} 값 '{value}' → 무시")
    return None


def _conf(name: str, value, warnings: list) -> Optional[float]:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        warnings.append(f"{name} 값 '{value}'을 숫자로 읽을 수 없음 → 무시")
        return None
    if 1.0 < v <= 100.0:  # 95 같은 퍼센트 표기
        v /= 100.0
    if not 0.0 <= v <= 1.0:
        warnings.append(f"{name} 값 {value}이 0~1 범위를 벗어남 → 무시")
        return None
    return v


@dataclass
class DialogueInput:
    """None = 이번에 새로 들어온 값 없음 (pose/emotion은 이벤트 방식).
    생성 시 라벨을 정규화하고, 알 수 없는 값은 무시하며 warnings에 남긴다."""

    timestamp: float = field(default_factory=time.time)
    pose: Optional[str] = None  # normal | lying | sleeping | fall
    emotion: Optional[str] = None  # neutral | happy | sad | angry | fear
    stt: Optional[str] = None
    sound_event: Optional[str] = None  # none | speech | fall_sound | daily | voice_unclear
    motion: Optional[str] = None  # moving | still | none (mmWave)
    pose_conf: Optional[float] = None  # 모델 확신도 0~1 (없으면 1.0으로 간주)
    emotion_conf: Optional[float] = None
    sound_conf: Optional[float] = None
    warnings: list = field(default_factory=list, compare=False)

    def __post_init__(self):
        w = self.warnings
        global _WARNED_MS_TIMESTAMP
        if isinstance(self.timestamp, (int, float)) and self.timestamp > MS_TIMESTAMP_THRESHOLD:
            self.timestamp = self.timestamp / 1000.0       # 밀리초(예: JavaScript Date.now())로 보고 초로 변환
            if not _WARNED_MS_TIMESTAMP:
                _WARNED_MS_TIMESTAMP = True
                w.append("timestamp가 1e11보다 커서 밀리초로 보고 초로 변환함 (이후 같은 변환은 경고하지 않음)")
        self.pose = _norm("pose", self.pose, w)
        self.emotion = _norm("emotion", self.emotion, w)
        self.sound_event = _norm("sound_event", self.sound_event, w)
        self.motion = _norm("motion", self.motion, w)
        self.pose_conf = _conf("pose_conf", self.pose_conf, w)
        self.emotion_conf = _conf("emotion_conf", self.emotion_conf, w)
        self.sound_conf = _conf("sound_conf", self.sound_conf, w)
        if self.stt is not None and not isinstance(self.stt, str):
            w.append("stt가 문자열이 아님 → 무시")
            self.stt = None

    @classmethod
    def from_dict(cls, d: dict) -> "DialogueInput":
        """모듈 간 데이터 형식이 확정되면 여기만 고치면 된다 (adapter).
        정의되지 않은 키(예: text)는 지금처럼 무시하되, 같은 키는 처음 한 번만 warnings에 남긴다."""
        unknown = sorted(str(k) for k in d if k not in KNOWN_INPUT_KEYS and str(k) not in _WARNED_KEYS)
        _WARNED_KEYS.update(unknown)
        inp = cls._from_dict(d)
        if unknown:
            inp.warnings.append(f"알 수 없는 입력 키 {unknown} → 무시 (같은 키는 한 번만 경고, 발화는 'stt' 키)")
        return inp

    @classmethod
    def _from_dict(cls, d: dict) -> "DialogueInput":
        ts = d.get("timestamp")
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts).timestamp()
            except ValueError:
                ts = None
        if ts is None:
            ts = time.time()
        g = d.get
        return cls(
            timestamp=ts,
            pose=g("pose"),
            emotion=g("emotion"),
            stt=g("stt"),
            sound_event=g("sound_event"),
            motion=g("motion"),
            pose_conf=g("pose_conf", g("pose_confidence")),
            emotion_conf=g("emotion_conf", g("emotion_confidence")),
            sound_conf=g("sound_conf", g("sound_confidence")),
        )


@dataclass
class DialogueOutput:
    state: State
    strategy: Strategy
    response_text: Optional[str]
    emergency: bool = False  # True인 턴에만 비상 알림 모듈 호출
    alert_seq: int = 0  # 비상 알림 순번 (1=최초, 2 이상=재알림)
    alert_cause: str = ""  # 비상 사유(현재): no_response | danger | crisis
    alert_status: str = ""  # raised(최초) | updated(사유 갱신) | realert(재알림) | resolved(해제) | ""(변화 없음)
    alert_origin: str = ""  # 이번 비상의 최초 사유 (사유가 갱신돼도 보존)
    user_responded: bool = False  # 비상 후 어르신 발화가 있었는지 (응답 있음 ≠ 안전 확인)
    notice: Optional[str] = None  # 비상은 아니지만 보호자에게 알릴 만한 정서 신호
    reason: str = ""
    assessment: Optional[dict] = None  # 상황 판단 결과(근거 로그용)
    warnings: list = field(default_factory=list)  # 입력값 문제(알 수 없는 라벨 등)
    response_deadline_sec: Optional[float] = None  # 안전 확인 질문 후 응답을 기다리는 시간(초). UI 카운트다운용
    emergency_active: bool = False  # 비상 알림 발생 후 해제 전까지 매 턴 True (UI의 '현재 비상 중' 표시용)
    alert_incident: int = 0  # 비상 사건 번호 (엔진마다 1부터, raised마다 증가). 알림 출력·해제 출력에만, 0=없음
    hazard: str = ""  # 이번 비상 사건의 환경 위험 종류 ("gas", "fire,smoke" …). 비상 알림 출력에만, 없으면 ""
    check_level: str = ""  # light: 소리 단독 + 정상 자세의 '가벼운 확인' 중(또는 그 무응답 종료) | "": 그 밖
    response_source: str = "template"  # 응답 문장 출처: template | llm
    llm_ms: Optional[int] = None  # LLM 응답에 걸린 시간(ms). LLM을 부르지 않았거나 시간 초과면 None
    llm_fallback_reason: Optional[str] = None  # LLM을 쓰려다 템플릿으로 대체한 이유 (timeout·trigger·medical 등), 없으면 None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"], d["strategy"] = self.state.value, self.strategy.value
        return d
