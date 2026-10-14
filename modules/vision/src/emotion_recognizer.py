"""표정(감정) 인식.

포즈 키포인트에서 추정한 얼굴 크롭 → 감정 분류.

모델 우선순위:
1. 파인튜닝 모델 (weights/emotion_finetuned.onnx — train.py 산출물)이 있으면 사용
2. 없으면 HSEmotion (AffectNet 사전학습, 8클래스) 사용

라벨 체계:
- raw   : 모델 원본 라벨 (HSEmotion 8종: Anger, Contempt, Disgust, Fear,
          Happiness, Neutral, Sadness, Surprise / 파인튜닝 모델은 labels.json)
- label : 팀 전송용 라벨 happy / sad / angry / surprised / neutral
          (유진 UI 규약이 4종 확정되면 config.export_surprised=False →
           surprised는 neutral로 전송되고 raw에는 그대로 남음)
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from .config import EmotionConfig
from .events import EventBus
from .pose_tracker import PersonPose

# 원본 라벨 → 팀 전송용 라벨
LABEL_MAP = {
    "Anger": "angry",
    "Contempt": "angry",
    "Disgust": "angry",
    "Fear": "surprised",
    "Happiness": "happy",
    "Neutral": "neutral",
    "Sadness": "sad",
    "Surprise": "surprised",
    # train.py(AIHub 등) 소문자 폴더명 대응
    "anger": "angry", "angry": "angry",
    "happiness": "happy", "happy": "happy",
    "neutral": "neutral",
    "sadness": "sad", "sad": "sad",
    "surprise": "surprised", "surprised": "surprised",
    "fear": "surprised", "disgust": "angry", "contempt": "angry",
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
        self._frame_count = 0
        self._candidate: str | None = None   # 연속 관측 중인 감정
        self._candidate_count = 0
        self.current: str | None = None      # 마지막으로 발행된 감정
        self.current_conf: float = 0.0
        self.last_raw: str | None = None     # 마지막 분류의 원본(8종) 라벨

    def _export_label(self, label: str) -> str:
        if label == "surprised" and not self.config.export_surprised:
            return "neutral"
        return label

    def classify_face(self, face_rgb: np.ndarray) -> tuple[str, str, float]:
        """얼굴 RGB 이미지 → (전송 라벨, 원본 라벨, confidence). 평가 스크립트에서도 사용."""
        raw_label, scores = self.model.predict_emotions(face_rgb, logits=False)
        label = self._export_label(LABEL_MAP.get(raw_label, "neutral"))
        return label, raw_label, float(np.max(scores))

    def process(self, frame: np.ndarray, pose: PersonPose) -> tuple[str, float] | None:
        """every_n_frames마다 표정 분류. 안정화된 변화가 있으면 emotion 이벤트 발행.

        반환: 이번 프레임의 (전송 라벨, confidence). 스킵/실패 시 None.
        """
        self._frame_count += 1
        if self._frame_count % self.config.every_n_frames != 0:
            return None

        box = pose.face_bbox(frame.shape)
        if box is None:
            return None
        x1, y1, x2, y2 = box
        if (x2 - x1) < self.config.min_face_size or (y2 - y1) < self.config.min_face_size:
            return None

        face = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2RGB)
        label, raw_label, conf = self.classify_face(face)
        self.last_raw = raw_label

        # 깜빡임 방지: 같은 감정이 stable_count번 연속 관측될 때만 이벤트 발행
        if label == self._candidate:
            self._candidate_count += 1
        else:
            self._candidate = label
            self._candidate_count = 1

        if self._candidate_count >= self.config.stable_count and label != self.current:
            self.current = label
            self.current_conf = conf
            self.bus.publish("emotion", label=label, raw=raw_label, confidence=round(conf, 3))

        return label, conf
