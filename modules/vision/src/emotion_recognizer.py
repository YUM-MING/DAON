"""표정(감정) 인식 — 1층 순간 표정 분류 + 2층 기분(mood) 집계.

포즈 키포인트에서 추정한 얼굴 크롭 → 표정 분류(expression) → MoodEstimator 가 30초 창에서 집계(mood).

모델 우선순위:
1. 파인튜닝 모델 (weights/emotion_finetuned.onnx — train.py 산출물, AIHub 7종)이 있으면 사용
2. 없으면 HSEmotion (AffectNet 사전학습, 8클래스) 사용

발행 이벤트 (LABELS.md 4절):
- expression_debug : 분류 1회마다. raw(모델 원본), expression(7종 정규화), conf, scores, face_px
- mood_pred        : 1초마다. pred(8종) + conf + detail(intensity/stability/baseline_delta/…)
- mood_changed     : mood 라벨이 바뀔 때 1회 (mood_pred 와 같은 필드)
- emotion          : 기존 팀 규약 (label 5종 + raw + confidence). mood_changed 에서 파생 — 서버·대화 모듈 호환용

라벨 체계:
- raw        : 모델 원본 라벨
- expression : happy / neutral / sad / angry / anxious / surprised / hurt
- mood       : happy / calm / sad / anxious / angry / surprised / tired / unknown
- label(구)  : happy / sad / angry / surprised / neutral (+ fear) — MOOD_TO_LEGACY
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import cv2
import numpy as np

from .config import EmotionConfig
from .events import EventBus
from .mood import MOOD_TO_LEGACY, RAW_TO_EXPRESSION, MoodEstimator
from .pose_tracker import L_SHOULDER, NOSE, R_SHOULDER, PersonPose

# 원본 라벨 → (구) 팀 전송용 5종 라벨. 프레임 단위 반환값과 viz 에만 쓰고, 이벤트 `emotion` 은 mood 에서 파생한다.
LABEL_MAP = {
    "Anger": "angry", "Contempt": "angry", "Disgust": "angry", "Fear": "surprised",
    "Happiness": "happy", "Neutral": "neutral", "Sadness": "sad", "Surprise": "surprised",
    "anger": "angry", "angry": "angry", "happiness": "happy", "happy": "happy", "neutral": "neutral",
    "sadness": "sad", "sad": "sad", "surprise": "surprised", "surprised": "surprised",
    "fear": "surprised", "disgust": "angry", "contempt": "angry",
    "anxious": "surprised", "hurt": "sad",      # 7종 모델 라벨 → 구 5종 (이전 파인튜닝과 같은 접기)
}


class _FinetunedModel:
    """train.py로 파인튜닝한 ONNX 분류기 (224x224, ImageNet 정규화)."""

    def __init__(self, model_path: str, labels_path: str) -> None:
        import onnxruntime as ort

        self.session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self.labels: list[str] = json.loads(Path(labels_path).read_text(encoding="utf-8"))
        self._input = self.session.get_inputs()[0].name

    def predict_emotions(self, face_rgb: np.ndarray, logits: bool = False):
        img = cv2.resize(face_rgb, (224, 224)).astype(np.float32) / 255.0
        img = (img - np.array([0.485, 0.456, 0.406])) / np.array([0.229, 0.224, 0.225])
        x = img.transpose(2, 0, 1)[None].astype(np.float32)
        out = self.session.run(None, {self._input: x})[0][0]
        scores = np.exp(out - out.max())
        scores /= scores.sum()
        return self.labels[int(scores.argmax())], scores


class EmotionRecognizer:
    def __init__(self, config: EmotionConfig, bus: EventBus) -> None:
        self.config = config
        self.bus = bus
        if Path(config.custom_model_path).exists() and Path(config.custom_labels_path).exists():
            self.model = _FinetunedModel(config.custom_model_path, config.custom_labels_path)
            self.model_kind = "finetuned"
            self._class_names = list(self.model.labels)
        else:
            # 동봉 가중치가 있으면 HSEmotion 캐시(~/.hsemotion)로 복사 → 오프라인 구동 가능
            bundled = Path("./weights") / f"{config.model_name}.onnx"
            cache = Path.home() / ".hsemotion" / f"{config.model_name}.onnx"
            if bundled.exists() and not cache.exists():
                import shutil

                cache.parent.mkdir(exist_ok=True)
                shutil.copy(bundled, cache)

            from hsemotion_onnx.facial_emotions import HSEmotionRecognizer

            self.model = HSEmotionRecognizer(model_name=config.model_name)
            self.model_kind = "pretrained"
            idx = getattr(self.model, "idx_to_class", None)
            self._class_names = [idx[i] for i in range(len(idx))] if idx else []
        self.mood = MoodEstimator(config.mood)
        self._frame_count = 0
        # (구) 호환 필드: current = 현재 mood 의 구 5종 라벨, current_conf = mood conf
        self.current: str | None = None
        self.current_conf: float = 0.0
        self.last_raw: str | None = None          # 마지막 분류의 모델 원본 라벨
        self.last_expression: str | None = None   # 마지막 분류의 7종 라벨
        self.last_scores: dict[str, float] | None = None

    def _export_label(self, label: str) -> str:
        if label == "surprised" and not self.config.export_surprised:
            return "neutral"
        return label

    def classify_face(self, face_rgb: np.ndarray) -> tuple[str, str, float]:
        """얼굴 RGB 이미지 → (구 5종 라벨, 원본 라벨, confidence). 평가 스크립트에서도 사용."""
        raw_label, scores = self.model.predict_emotions(face_rgb, logits=False)
        scores = np.asarray(scores, dtype=float).ravel()
        if self._class_names and len(self._class_names) == len(scores):
            self.last_scores = {RAW_TO_EXPRESSION.get(str(n).lower(), str(n)): round(float(s), 3)
                                for n, s in zip(self._class_names, scores)}
        label = self._export_label(LABEL_MAP.get(raw_label, "neutral"))
        return label, raw_label, float(np.max(scores))

    @staticmethod
    def head_droop(pose: PersonPose) -> float | None:
        """고개 숙임 지표: (코 y - 어깨중심 y) / 어깨폭. 고개를 떨구면 커진다. 피곤(tired) 규칙 입력."""
        if not (pose.valid(NOSE) and pose.valid(L_SHOULDER) and pose.valid(R_SHOULDER)):
            return None
        ls, rs = pose.keypoints[L_SHOULDER], pose.keypoints[R_SHOULDER]
        width = float(np.hypot(*(ls - rs)))
        if width < 1e-3:
            return None
        return float((pose.keypoints[NOSE][1] - (ls[1] + rs[1]) / 2.0) / width)

    def tick(self, now: float | None = None) -> None:
        """얼굴/사람이 없는 프레임에서도 호출 — 일정 시간 얼굴이 없으면 mood=unknown 을 내보낸다."""
        self._emit_mood(now if now is not None else time.time())

    def process(self, frame: np.ndarray, pose: PersonPose,
                now: float | None = None) -> tuple[str, float] | None:
        """every_n_frames마다 표정 분류 → MoodEstimator 에 넣고, 주기마다 mood_pred 발행.

        반환: 이번 프레임의 (구 5종 라벨, confidence). 스킵/실패 시 None.
        """
        now = now if now is not None else time.time()
        self._frame_count += 1
        if self._frame_count % self.config.every_n_frames != 0:
            self._emit_mood(now)
            return None

        box = pose.face_bbox(frame.shape)
        if box is None:
            self._emit_mood(now)
            return None
        x1, y1, x2, y2 = box
        face_px = int(min(x2 - x1, y2 - y1))
        if face_px < self.config.min_face_size:
            self._emit_mood(now)
            return None

        face = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2RGB)
        label, raw_label, conf = self.classify_face(face)
        self.last_raw = raw_label
        expr = self.mood.observe(now, raw_label, conf, face_px=face_px, droop=self.head_droop(pose))
        self.last_expression = expr
        if self.config.publish_expression:
            self.bus.publish("expression_debug", raw=raw_label, expression=expr, conf=round(conf, 3),
                             scores=self.last_scores, face_px=face_px)
        self._emit_mood(now)
        return label, conf

    def _emit_mood(self, now: float) -> None:
        out = self.mood.update(now)
        if out is None:
            return
        self.bus.publish("mood_pred", pred=out["pred"], conf=out["conf"], detail=out["detail"])
        if self.mood.changed:
            self.bus.publish("mood_changed", pred=out["pred"], conf=out["conf"], detail=out["detail"])
            legacy = MOOD_TO_LEGACY.get(out["pred"])
            if legacy is not None:
                legacy = self._export_label(legacy)
                self.current, self.current_conf = legacy, out["conf"]
                # (구) 팀 규약 이벤트 — 서버 어댑터·대화 모듈이 그대로 받는다
                self.bus.publish("emotion", label=legacy, raw=out["detail"].get("top_expression"),
                                 confidence=out["conf"], mood=out["pred"])
