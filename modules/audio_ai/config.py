"""
config.py — 모든 모듈이 공유하는 설정값
(torch 없이도 불러올 수 있도록 의존성을 두지 않는다)
"""
from pathlib import Path

SAMPLE_RATE = 16000          # 16kHz 모노
WINDOW_SEC = 1.0             # 분류 단위 길이 (초)
N_SAMPLES = int(SAMPLE_RATE * WINDOW_SEC)

N_MELS = 64
N_FFT = 512
HOP_LENGTH = 160             # 10ms → 1초당 약 100 프레임

CLASSES = ["speech", "fall", "ambient"]
CLASS_KO = {"speech": "대화", "fall": "낙상·충격음", "ambient": "일상 소음"}
NUM_CLASSES = len(CLASSES)

ROOT = Path(__file__).resolve().parent
DATA_RAW = ROOT / "data" / "raw"
DATA_PROC = ROOT / "data" / "processed"
CKPT_PATH = ROOT / "checkpoints" / "sound_cls.pt"
OUT_DIR = ROOT / "outputs"
