"""
model.py — 환경음 3-클래스 분류 모델
  입력: 1초 오디오(16kHz) → log-Mel 스펙트로그램 → 소형 CNN → 3 클래스

클래스
  0 speech  : 사람의 대화 소리
  1 fall    : 낙상 / 충격음 (쿵, 쓰러짐, 물건 떨어짐)
  2 ambient : 그 외 일상 생활 소음

모든 경로는 이 파일 기준 상대경로(./)를 사용한다.
"""
from pathlib import Path

import torch
import torch.nn as nn
import torchaudio

# 설정값은 config.py 에서 가져온다 (다른 모듈과 공유)
from config import (SAMPLE_RATE, WINDOW_SEC, N_SAMPLES, N_MELS, N_FFT, HOP_LENGTH,
                    CLASSES, CLASS_KO, NUM_CLASSES, ROOT, CKPT_PATH)

# ----------------------------------------------------------------------------
# 특징 추출: waveform → log-Mel (모델 안에 포함시켜 추론 코드를 단순화)
# ----------------------------------------------------------------------------
class LogMel(nn.Module):
    def __init__(self):
        super().__init__()
        self.mel = torchaudio.transforms.MelSpectrogram(
            sample_rate=SAMPLE_RATE,
            n_fft=N_FFT,
            hop_length=HOP_LENGTH,
            n_mels=N_MELS,
            f_min=20,
            f_max=SAMPLE_RATE // 2,
            power=2.0,
        )
        self.to_db = torchaudio.transforms.AmplitudeToDB(top_db=80)

    def forward(self, wav: torch.Tensor) -> torch.Tensor:
        """wav: (B, T) → (B, 1, N_MELS, frames)"""
        spec = self.to_db(self.mel(wav))
        # 샘플 단위 정규화 (녹음 볼륨 차이를 지움)
        mean = spec.mean(dim=(-2, -1), keepdim=True)
        std = spec.std(dim=(-2, -1), keepdim=True).clamp_min(1e-5)
        return ((spec - mean) / std).unsqueeze(1)


def conv_block(cin: int, cout: int) -> nn.Sequential:
    return nn.Sequential(
        nn.Conv2d(cin, cout, 3, padding=1, bias=False),
        nn.BatchNorm2d(cout),
        nn.ReLU(inplace=True),
        nn.Conv2d(cout, cout, 3, padding=1, bias=False),
        nn.BatchNorm2d(cout),
        nn.ReLU(inplace=True),
        nn.MaxPool2d(2),
    )


class SoundClassifier(nn.Module):
    """log-Mel + 4블록 CNN. 파라미터 약 0.6M 으로 CPU 실시간 추론 가능."""

    def __init__(self, num_classes: int = NUM_CLASSES, dropout: float = 0.3):
        super().__init__()
        self.frontend = LogMel()
        self.features = nn.Sequential(
            conv_block(1, 16),
            conv_block(16, 32),
            conv_block(32, 64),
            conv_block(64, 128),
        )
        self.pool = nn.AdaptiveAvgPool2d(1)
        self.head = nn.Sequential(
            nn.Flatten(),
            nn.Dropout(dropout),
            nn.Linear(128, num_classes),
        )

    def forward(self, wav: torch.Tensor) -> torch.Tensor:
        """wav: (B, N_SAMPLES) 파형 → (B, num_classes) 로짓"""
        x = self.frontend(wav)
        x = self.features(x)
        x = self.pool(x)
        return self.head(x)


def build_model(**kwargs) -> SoundClassifier:
    return SoundClassifier(**kwargs)


def load_model(ckpt_path: Path = CKPT_PATH, device: str = "cpu") -> SoundClassifier:
    """학습된 가중치(.pt)를 불러온다."""
    model = build_model()
    state = torch.load(ckpt_path, map_location=device)
    model.load_state_dict(state["model_state"] if "model_state" in state else state)
    model.to(device).eval()
    return model


if __name__ == "__main__":
    m = build_model()
    n = sum(p.numel() for p in m.parameters())
    dummy = torch.randn(2, N_SAMPLES)
    print(f"파라미터 수: {n:,}")
    print(f"출력 shape : {tuple(m(dummy).shape)}")
