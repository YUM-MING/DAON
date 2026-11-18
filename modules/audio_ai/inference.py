"""
inference.py — 실시간 소리 감지 · 분리 → 대화면 STT

흐름
  마이크(16kHz) → 1초 윈도우(0.5초 간격) → 분류기
      ├ fall     : 낙상·충격음 이벤트 출력  (팀 파이프라인에 전달)
      └ speech   : 발화 구간을 모아두었다가, 말이 끝나면 STT 로 텍스트 변환
      └ ambient  : 무시

사용 예)
  python inference.py                      # 마이크 실시간
  python inference.py --no-stt             # 분류만 (STT 모델 없이 동작 확인)
  python inference.py --file sample.wav    # 마이크 없이 wav 파일로 테스트

출력은 한 줄 JSON 이라 다른 모듈이 그대로 받아쓸 수 있다.
  {"type": "fall",   "confidence": 0.93, "time": 12.5}
  {"type": "speech", "text": "오늘 날씨가 춥네", "time": 15.2}
"""
from __future__ import annotations

import argparse
import json
import queue
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
import torch

from model import CLASSES, CLASS_KO, N_SAMPLES, SAMPLE_RATE, CKPT_PATH, load_model

ROOT = Path(__file__).resolve().parent
HOP_SEC = 0.5                     # 분류 간격
#   낙상 판단 확률. val split 에서 고른 값 (eval_threshold.py --split val).
#   τ=0.60 / K=1  (낙상 원본 12개, 비낙상 음원 38개 기준)
#     val  : 이벤트 재현율 100% (12/12), 오탐 음원 5/38
#     test : 이벤트 재현율 100% (12/12), 오탐 음원 4/38
#   올리면 오탐은 줄지만 미탐이 늘어난다 (test 기준 τ=0.90 → 재현율 66.7%).
#   낙상은 놓치는 쪽이 더 위험하고 뒤에 포즈 교차검증과 음성 확인이 있으므로
#   재현율을 우선했다. 근거: outputs/threshold_val.txt
FALL_THRESHOLD = 0.60
#   낙상 이벤트를 내기 위한 최소 진폭. 0.0 이면 끈다.
#   무음 게이트(SILENCE_PEAK=0.002)는 디지털 무음만 막는다. 폰 AEC 가 남기는
#   아주 작은 잔향(peak 0.02~0.034)은 디지털 무음이 아니라 게이트를 통과하는데,
#   모델은 그런 입력을 배운 적이 없어 P(fall) 0.87~0.98 을 내놓는다 (실측 6건).
#   같은 녹음들에서 진짜 충격음의 최소 peak 은 0.072 로 2.1배 떨어져 있다.
#   data/realworld 회귀: 재현율 29/30 그대로, 오탐 알림 5회 → 3회.
# 2026-11-15 재측정. 0.05 를 고를 때 근거는 "오탐 6건의 최대 peak 0.034" 였는데,
# 표본이 6건뿐이라 여유가 실제보다 커 보였다. 실사용에서 깨졌다.
#     17:10:31  peak 0.093  P(fall) 0.836   ← 보호자 알림까지 나갔다
# 오탐 28창(실사용·STT 세션 녹음, 낙상이 없었던 것만)과 진짜 낙상 30개를
# 겹쳐 놓고 다시 골랐다.
#
#         값     놓친 낙상   남는 오탐 창
#       0.05        1개        18
#       0.10        1개        12      ← 지금 이것
#       0.12        1개        10
#       0.15        2개         8
#
# 0.12 가 오탐이 가장 적지만 쓰지 않는다. 가장 조용한 진짜 낙상이
# fall_이불_3m_004.wav 의 peak 0.121 이라 0.001 차이로 간신히 통과한다.
# 그건 여유가 아니다. 0.10 이면 1.2배 여유가 남는다.
#
# 평가셋(data/realworld, 고를 때 쓰지 않음) 확인:
#   재현율 96.7%(29/30) 그대로 · 놓친 파일도 그대로(fall_카펫_3m_000)
#   오탐 알림 3회 → 2회 · 시간당 44.3 → 29.5회
#
# peak 로는 여기까지다. 남는 오탐에는 peak 1.000 에 P(fall) 0.808 처럼
# 큰 소리도 있다. 음량으로는 못 거른다. 더 줄이려면 모델이나 융합 쪽이다.
FALL_MIN_PEAK = 0.10
#   판정 규칙: 최근 FALL_K 개 윈도우를 어떻게 볼지. eval_threshold.py 로 고른 값.
#   K=1 이면 윈도우 하나만 보고 판정한다(가장 빠름).
#   낙상음은 0.2~0.5초로 짧아 윈도우 하나에만 담기는 경우가 많으므로,
#   "K개가 모두 넘어야 한다"(all)는 조건은 오히려 재현율을 크게 깎는다.
#   실측 결과 K=1 이 가장 좋았다 → outputs/threshold_val.txt 참고
FALL_K = 1
FALL_RULE = "avg"                 # "avg"(최근 K개 평균) 또는 "all"(최근 K개 모두)
FALL_REFRACTORY_SEC = 5.0         # 한 번 알린 뒤 이 시간 동안은 중복 알림을 막는다
#   재알림 억제가 있으므로 한 번의 낙상 = 한 번의 이벤트가 된다.
#   받는 쪽에서 디바운스를 따로 구현할 필요가 없다.
SPEECH_THRESHOLD = 0.50
# 말이 멈춘 뒤 이만큼 기다렸다가 발화가 끝난 것으로 본다. 이 시간은 응답
# 속도에 그대로 더해진다 (여기에 hop 0.5초 격자가 붙어 실제로는 0.7~1.2초).
#
# 1.0 → 0.7 (2026-11-15). 줄여도 문장이 쪼개지지 않는지 확인했다.
#   조용(세션1, 18문장)  0.6/0.7/0.8/1.0/1.2 모두 18구간 — 차이 없음
#   TV 볼륨8(세션3b)     0.6~1.0 모두 21구간 — 차이 없음 (TV 소음 때문에
#                        원래 3개가 더 잡힌다. 줄여도 더 나빠지지 않는다)
# 응답마다 0.3초를 번다.
#
# 더 줄이려면 hop 도 0.25 로 낮춰야 0.25초를 더 버는데, hop 은 낙상 판정의
# 연속 창 조건(FALL_K)까지 바꾼다. 낙상 평가를 다시 돌리기 전에는 건드리지 않는다.
# 받아쓰기 대기열이 밀렸을 때 얼마나 참을 것인가.
#
#   2026-11-17 학교 시연 준비에서 "말하고 10분 뒤에 인식과 답장이 떴다" 는
#   보고가 있었다. 응답 한 번은 2.7~5.2초다. 10분은 그 100~200배라 어느
#   구간이 느려져서 나올 수 있는 값이 아니다. 밀린 것(backlog)이다.
#
#   원인은 이 파일의 STT 대기열이 상한도 버리는 규칙도 없었다는 것이다.
#   발화 하나를 받아쓰는 데 1.3초 걸리는데, 발화가 그보다 빠르게 들어오면
#   대기열이 무한정 쌓이고 지연이 선형으로 늘어난다. 조용해져도 안 풀린다.
#   쌓인 것을 다 처리해야 하기 때문이다.
#
#   집에 어르신 혼자 있으면 발화가 띄엄띄엄 들어와 생기지 않는다. 그러나
#   여러 사람이 계속 말하는 곳이나, 네트워크가 막혔다가 풀리면서 폰이
#   모아 둔 음성을 한꺼번에 보낼 때 생긴다(폰은 최대 23초치를 모은다).
#
#   실시간 대화에서 10분 전 발화에 답하는 것은 의미가 없을 뿐 아니라
#   해롭다. 엉뚱한 때에 엉뚱한 대답이 나간다. 그래서 두 겹으로 막는다.
STT_QUEUE_MAX = 3            # 대기열 상한. 넘으면 **오래된 것부터** 버린다
STT_MAX_LAG_SEC = 8.0        # 받아쓰기를 시작할 때 이보다 오래된 발화는 버린다

SILENCE_SEC = 0.7
MAX_UTTERANCE_SEC = 15.0
#   발화 버퍼는 창이 speech 로 분류된 뒤에야 담기 시작한다. 창이 1초, hop 이
#   0.5초이므로 말이 시작된 뒤 최대 0.5초가 버려지고, 거기에 첫 낱말이 들어 있다.
#   측정(2026-10-23, 세션 1): 18문장 중 9문장만 첫 어절이 맞았다(전체 CER 25.4%).
#   앞을 PREROLL_SEC 만큼 더 담고, 그중 말이 시작되기 전의 죽은 구간만 잘라내면
#   첫 어절 15/18, 로봇 이름 6/6, CER 14.2% 가 되고 발화 길이는 +6% 에 그친다.
#   (앞자름 없이 1초를 통째로 붙이면 길이가 +28% 늘고 CER 도 17.8% 에 머문다)
#   근거: DAON_patch/프리롤_결과_stt_quiet_base.txt
PREROLL_SEC = 1.0                 # 발화 앞에 더 담을 길이. 0 이면 끈다
PREROLL_TRIM_LEVEL = 0.01         # 이 진폭을 넘으면 말이 시작된 것으로 본다
PREROLL_TRIM_LEAD = 0.2           # 그 지점 앞으로 남겨 둘 여유 (초)
VOICE_UNCLEAR_MIN_SEC = 0.8       # 사람 소리는 났는데 말로 인식되지 않은 최소 길이
#   STT 모델 크기. benchmark.py 로 base / small 을 같은 한국어 문장으로 비교한 결과,
#   받아쓰기 정확도는 차이가 없는데 base 가 3배 빨랐다 (고정 지연 0.7초 vs 2.3초).
#   체감 응답 지연 1.7초 vs 3.3초. 디스크도 140MB vs 500MB.
#   더 어려운 발화를 다뤄야 하면 "small" 로 올린다. 근거: outputs/benchmark.txt
STT_MODEL = "base"
FALL_IDX = CLASSES.index("fall")

# ─────────────────────────────────────────────────────────────────────────
# 무음 게이트 — 신호가 사실상 없는 윈도우는 아예 분류하지 않는다.
#
# 왜 필요한가
#   분류기는 "완전한 디지털 무음"을 학습한 적이 없다. 0으로만 채워진 윈도우를
#   넣으면 P(fall)=0.886 같은 값이 나온다(입력이 같으니 매번 같은 값). 학습
#   분포 밖의 입력에 softmax 는 "모른다"고 답하지 못하고 아무 값이나 내놓는다.
#
# 실제로 두 번 확인됐다
#   1) 2026-10-12  AI Hub 한국어 노인 발화 150개(화자 5명)를 넣었더니
#      τ=0.60 을 넘은 윈도우 62개가 **전부** 파일 앞뒤 무음 구간이었다.
#      무음을 빼면 오탐 0/1454 윈도우, 0/150 파일.
#   2) 2026-10-18  팀 통합 폰 녹음에서 **소리의 74% 가 완전한 디지털 무음**
#      이었고(로봇이 말하는 동안 폰 AEC 가 마이크를 통째로 누름),
#      900번 판정 중 353번(39%)이 낙상음으로 찍혔다. (TUNING_GUIDE.md)
#
# 기준값은 측정해서 골랐다
#   | | peak | rms |
#   |---|---|---|
#   | 측정된 무음 창의 최대          | 0.00034 | ~0.00000 |
#   | **게이트**                   | **0.002** | **0.0008** |
#   | 실환경 낙상 충격 창의 최소     | 0.0275  | 0.00442  |
#   | 한국어 발화 창의 중앙값        | 0.62    | 0.10     |
#   양쪽으로 각각 14배 / 5.5배 여유가 있고, 실환경 낙상 30건의 충격 창 중
#   어느 기준에도 걸리는 것은 0개였다. (근거: outputs/realworld_eval.txt)
#
#   RMS 만 보지 않는 이유: 충격음은 0.2~0.5초로 짧아 1초 평균을 내면 묻힌다.
#   peak 만 보지 않는 이유: AEC 가 누른 구간에 튀는 샘플 하나가 남을 수 있다.
#   0 비율도 보는 이유: 폰 AEC 는 구간을 통째로 0 으로 만든다.
SILENCE_PEAK = 0.002        # 최대 진폭이 이보다 작으면 무음
SILENCE_RMS = 0.0008        # -62 dBFS. 서버 워커(audio_worker.py)와 같은 값
SILENCE_ZERO_FRAC = 0.9     # 창의 90% 이상이 정확히 0 이면 무음 (폰 AEC 억제 구간)

# STT 에 넣기 전 발화 구간의 음량 하한.
#   거의 무음인 버퍼가 whisper 에 들어가면 "대대 목욕수수" 같은 환각 문장이 나온다.
#   한국어 노인 발화 150개의 파일 전체 RMS 최소값이 0.0459 이므로 11배 여유.
UTTERANCE_MIN_RMS = 0.004

# 공통 라벨 사전(LABELS.md v0.2 / labels.json) 에 맞춘 값들.
#   audio_pred.detail.label = speech | impact | ambient | silence
#   · 내부 클래스 이름은 "fall" 을 그대로 쓴다. 체크포인트와 학습·평가 코드가
#     전부 "fall" 로 되어 있어서 지금 바꾸면 재현이 깨진다.
#   · 밖으로 내보낼 때만 LABEL_OUT 으로 바꾼다 (fall → impact).
#   · 이벤트 type 은 아직 "fall" 이다. 서버 TYPE_MAP 이 "fall" 을 기대하므로,
#     type 을 impact 로 바꾸려면 server/workers/audio_worker.py 도 같이 고쳐야 한다.
#     → 팀장과 상의 후 한 번에 바꾼다. (LABELS.md 9절 '네이티브 출력 전환')
SILENCE_LABEL = "silence"
LABEL_OUT = {"speech": "speech", "fall": "impact", "ambient": "ambient",
             SILENCE_LABEL: SILENCE_LABEL}
# voice_unclear 로 돌릴 한글 음절 비율 하한 (LABELS.md: lang_ratio < 0.5)
MIN_LANG_RATIO = 0.5


def rms_dbfs(x: np.ndarray) -> float:
    """창의 음량을 dBFS 로. 완전한 무음은 -120 으로 둔다."""
    if x.size == 0:
        return -120.0
    r = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    return -120.0 if r <= 1e-12 else max(-120.0, 20.0 * np.log10(r))


def lang_ratio(text: str) -> float:
    """한글 음절 비율. STT 가 엉뚱한 언어로 받아쓴 결과를 걸러내는 데 쓴다.

    whisper 는 알아듣지 못하면 학습 데이터에 많던 문구를 뱉는 버릇이 있다
    ("Thank you", "ご視聴ありがとうございました"). 한글 비율이 낮으면 발화로 보지 않는다.
    """
    chars = [c for c in text if not c.isspace() and c.isalnum()]
    if not chars:
        return 0.0
    hangul = sum(1 for c in chars if "가" <= c <= "힣" or "ㄱ" <= c <= "ㆎ")
    return hangul / len(chars)


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _trim_front(wav: np.ndarray) -> np.ndarray:
    """말이 시작되기 전의 빈 구간을 잘라낸다 (앞으로 PREROLL_TRIM_LEAD 초만 남긴다).

    앞을 1초 더 담으면 첫 낱말은 살지만 whisper 에 넣는 길이가 그만큼 늘어난다.
    그 1초의 대부분은 폰 AEC 가 눌러 놓은 빈 구간이므로 걷어내도 손해가 없다.
    기준을 넘는 샘플이 하나도 없으면(아주 조용한 발화) 그대로 둔다.
    """
    if wav.size == 0 or PREROLL_SEC <= 0:
        return wav
    idx = np.nonzero(np.abs(wav) > PREROLL_TRIM_LEVEL)[0]
    if idx.size == 0:
        return wav
    return wav[max(0, int(idx[0]) - int(PREROLL_TRIM_LEAD * SAMPLE_RATE)):]


class SpeechToText:
    """faster-whisper 래퍼. 모델이 없으면 비활성 상태로 동작한다."""

    #   로봇 이름은 학습 데이터에 없는 고유명사라 whisper 가 음이 비슷한 흔한 낱말로
    #   바꿔 버린다. 측정(2026-10-23, 세션 1): "다온아" 가 들어간 6문장이 전부 실패
    #   했다 — 가우나 / 가운아 / 다운아 / 다화 / 하워나. 정답이 0번 나왔다.
    #   혜원님 모듈은 부름을 글자 그대로 본다(CALL_WORDS, CALL_PREFIXES)라서
    #   "다운아" 는 매칭되지 않는다 — 불러도 부른 것으로 인식되지 않는다.
    #
    #   hotwords 로 이름을 알려주면 0/6 → 4/6 이 되고, 전체 CER 은 오히려
    #   25.9% → 25.4% 로 좋아졌다. 지연은 발화당 +0.1초.
    #
    #   낱말을 여러 개 넣으면 안 된다. "다온아 다온이 다온" 으로 주면 이름은
    #   6/6 이 되지만 모델이 그 낱말만 반복하는 환각에 빠진다(CER 134.5%).
    #   근거: DAON_patch/이름인식_결과_stt_quiet_base.txt
    #   2026-11-15 재측정. 시연 대본 발화 12개(발표자 녹음, rec/대본발화/)로
    #   핫워드를 "다온" → "다온아" 로 바꾸고 낱말 프롬프트를 더했다.
    #
    #                        깨어남   CER    그대로 맞음   발화당
    #     "다온" (이전)         X    14.9%    6/12      0.91초
    #     "다온아"+프롬프트       O     5.9%   10/12      0.80초   ← 지금 이것
    #     small+프롬프트        O     3.0%    9/12      2.91초   (3.6배 느려 안 쓴다)
    #
    #   '깨어남' 은 혜원님 text_analyzer 의 CALL_WORDS 판정이다. 이전 설정은
    #   "다온아" 를 "다온어" 로 받아썼고 그 낱말이 호출어 집합에 없어서
    #   **불러도 로봇이 깨어나지 않았다.** CER 보다 이게 먼저다.
    #   근거: DAON_patch/대본STT2_결과.txt
    HOTWORDS = "다온아"

    # 나올 낱말을 앞 문맥으로 알려 준다. 점심은→잠시만, 화장실→하자실,
    # 손녀한테→소년한테 가 이것으로 고쳐졌다.
    #
    #   대본 문장을 그대로 넣으면 안 된다. 모델이 그걸 베껴 쓴다. 낱말만 담되,
    #   낱말 목록만 주는 것(CER 11.9%)보다 한 문장에 담는 쪽(5.9%)이 나았다.
    INITIAL_PROMPT = ("어르신과 돌봄 로봇 다온이 나누는 대화입니다. "
                      "쓸쓸하다, 그럭저럭, 손녀, 김치찌개, 된장찌개, 약, 창문, "
                      "화장실, 점심, 주말 같은 말이 나옵니다.")

    # 짧은 발화에서 모델이 위 프롬프트를 그대로 읽어 버리는 일이 있다.
    #   "다온아" (0.9초)  →  "다온 어르신과 돌봄 다온이 나누는"
    # 글자 토막으로 잡으려 했더니 모델이 '로봇' 을 빼먹어서 못 잡았다. 그래서
    # 프롬프트 낱말이 **적어 둔 순서대로 3개 이상 이어 나오는지**로 본다.
    # 낱말 한둘이 겹치는 것은 당연하다 — 프롬프트는 나올 낱말을 적어 둔 것이다.
    # 걸리면 그 발화를 못 들은 것으로 치는 쪽이, 헛소리를 대화에 넘기는 쪽보다 낫다.
    PROMPT_ECHO_RUN = 3

    def __init__(self, model_size: str = STT_MODEL, enabled: bool = True):
        self.model = None
        self.model_size = model_size
        self._hotwords_ok = True        # faster-whisper 1.0+ 여부. 첫 호출에서 확인한다
        if not enabled:
            return
        try:
            from faster_whisper import WhisperModel
            device = "cuda" if torch.cuda.is_available() else "cpu"
            compute = "float16" if device == "cuda" else "int8"
            self.model = WhisperModel(model_size, device=device, compute_type=compute)
            print(f"[STT] faster-whisper '{model_size}' 로드 ({device}/{compute})", file=sys.stderr)
        except Exception as e:                                   # noqa: BLE001
            print(f"[STT] 비활성 ({e})", file=sys.stderr)

    @classmethod
    def _prompt_echo(cls, text: str) -> bool:
        """받아쓴 글이 initial_prompt 를 베낀 것인지 본다 (PROMPT_ECHO_RUN 참고)."""
        if not text or not cls.INITIAL_PROMPT:
            return False
        plain = re.sub(r"[^\w가-힣]", "", text)
        run = gap = longest = 0
        for w in re.split(r"[,.\s]+", cls.INITIAL_PROMPT):
            w = re.sub(r"[^\w가-힣]", "", w)
            if w and w in plain:
                run += 1
                gap = 0
                longest = max(longest, run)
            elif run and gap == 0:
                gap = 1                      # 한 낱말 빠뜨린 것은 눈감아 준다
            else:
                run = gap = 0
        return longest >= cls.PROMPT_ECHO_RUN

    def transcribe(self, wav: np.ndarray) -> str:
        if self.model is None:
            return ""
        kw = dict(language="ko", beam_size=1, vad_filter=True,
                  # 어르신 발화는 느리고 쉼이 길어 기본값보다 여유를 둔다
                  no_speech_threshold=0.5)
        if self.INITIAL_PROMPT:
            kw["initial_prompt"] = self.INITIAL_PROMPT
        if self.HOTWORDS and self._hotwords_ok:
            kw["hotwords"] = self.HOTWORDS
        try:
            segments, _ = self.model.transcribe(wav, **kw)
        except TypeError:
            # faster-whisper 1.0 미만에는 hotwords 가 없다. 한 번 겪으면 다시 쓰지 않는다.
            self._hotwords_ok = False
            kw.pop("hotwords", None)
            segments, _ = self.model.transcribe(wav, **kw)
        text = " ".join(s.text.strip() for s in segments).strip()
        if self._prompt_echo(text):
            print(f"[STT] 프롬프트를 베껴 써서 버림: {text[:48]}", file=sys.stderr)
            return ""
        return text


class AudioRouter:
    """분류 결과에 따라 낙상 이벤트와 발화 구간을 나누는 상태 기계.

    emit 을 넘기면 결과를 표준출력이 아니라 그 함수로 보낸다.
    (팀 통합용 audio_module.AudioSensor 가 이 방식으로 콜백을 연결한다)
    """

    def __init__(self, model, stt: SpeechToText, device: str = "cpu", verbose: bool = False,
                 emit_fn=None, fall_threshold: float = FALL_THRESHOLD,
                 speech_threshold: float = SPEECH_THRESHOLD,
                 fall_k: int = FALL_K, fall_rule: str = FALL_RULE,
                 fall_refractory_sec: float = FALL_REFRACTORY_SEC,
                 fall_min_peak: float = FALL_MIN_PEAK,
                 clock=None, stt_async: bool = False):
        self.model = model
        self.stt = stt
        self.device = device
        self.verbose = verbose
        self.emit = emit_fn if emit_fn is not None else emit
        self.fall_threshold = fall_threshold
        self.speech_threshold = speech_threshold
        self.fall_k = max(1, int(fall_k))
        self.fall_rule = fall_rule
        self.fall_refractory_sec = fall_refractory_sec
        self.fall_min_peak = fall_min_peak
        self._fall_hist: deque[float] = deque(maxlen=self.fall_k)   # 최근 K개 P(fall)
        # 발화 시작 직전의 hop 들. 첫 낱말이 잘리지 않게 앞에 붙인다 (PREROLL_SEC)
        self._preroll: deque[np.ndarray] = deque(
            maxlen=max(1, int(np.ceil(PREROLL_SEC / HOP_SEC))))
        self._last_fall_t = -1e9          # 마지막 낙상 알림 시각
        self.speech_buf: list[np.ndarray] = []
        self.silence = 0.0
        # 진단용 카운터. 서버에서 주기적으로 읽어 "마이크가 눌리고 있다"를 알 수 있다.
        self.windows = 0                  # push 된 전체 창
        self.gated = 0                    # 그중 무음으로 걸러낸 창
        self.quiet_utterances = 0         # 음량 부족으로 STT 를 건너뛴 발화
        self.rejected_lang = 0            # 한글 비율이 낮아 버린 STT 결과
        # 마지막 창의 상태. 서버가 audio_pred 를 만들 때 읽어 간다 (LABELS.md).
        self.last_label = SILENCE_LABEL
        self.last_rms_dbfs = -120.0
        # 시계를 주입할 수 있게 둔다. 파일 입력 테스트에서는 실제 시간이 아니라
        # 오디오 위치를 시간으로 써야 재알림 억제가 올바르게 동작한다.
        self.clock = clock if clock is not None else time.time
        self.t0 = self.clock()
        self._now = self.t0

        # STT 는 0.7초쯤 걸린다. 같은 스레드에서 돌리면 그동안 오디오 처리가 멈춰
        # 낙상 감지가 늦어진다. 마이크 입력에서는 별도 스레드로 보낸다.
        # (파일 입력은 결과 순서를 보장해야 하므로 동기 처리)
        self.stt_async = stt_async
        self._stt_q: queue.Queue | None = None
        self._stt_thread: threading.Thread | None = None
        self.start_stt()

    def start_stt(self) -> None:
        """STT 워커 스레드를 띄운다. 이미 돌고 있으면 아무것도 하지 않는다.
        close() 로 내린 뒤 다시 쓰려면 이 메서드를 호출한다."""
        if not self.stt_async or self._stt_thread is not None:
            return
        self._stt_q = queue.Queue(maxsize=STT_QUEUE_MAX)
        self.stt_dropped = 0                 # 밀려서 버린 발화 수 (상태에 실어 보낸다)
        self._stt_thread = threading.Thread(target=self._stt_loop,
                                            name="stt-worker", daemon=True)
        self._stt_thread.start()

    def _stt_loop(self) -> None:
        while True:
            item = self._stt_q.get()                    # type: ignore[union-attr]
            if item is None:
                return
            wav, t = item
            lag = self._now - t
            if lag > STT_MAX_LAG_SEC:
                # 이미 지난 말이다. 지금 답하면 엉뚱한 때에 엉뚱한 대답이 나간다.
                self.stt_dropped += 1
                print(f"[STT] {lag:.1f}초 밀린 발화를 버립니다 "
                      f"(누적 {self.stt_dropped}건). 소리가 끊이지 않거나 "
                      f"네트워크가 밀리고 있습니다.", file=sys.stderr)
                continue
            try:
                self._transcribe_and_emit(wav, t)
            except Exception as e:                      # noqa: BLE001
                print(f"[STT] 변환 실패: {e!r}", file=sys.stderr)

    def _classify(self, window: np.ndarray) -> tuple[str, float, np.ndarray]:
        x = torch.from_numpy(window).unsqueeze(0).to(self.device)
        with torch.no_grad():
            prob = torch.softmax(self.model(x), dim=1)[0].cpu().numpy()
        i = int(prob.argmax())
        return CLASSES[i], float(prob[i]), prob

    def _transcribe_and_emit(self, wav: np.ndarray, t: float) -> None:
        dur = round(len(wav) / SAMPLE_RATE, 2)
        text = self.stt.transcribe(wav)
        lr = lang_ratio(text)
        if text and lr < MIN_LANG_RATIO:
            # 한국어로 받아쓴 결과가 아니다. whisper 가 못 알아들을 때 학습 데이터에
            # 많던 문구를 뱉는 경우("Thank you", "ご視聴…"). 발화로 올리지 않는다.
            # LABELS.md: voice_unclear = STT 가 비었거나 lang_ratio < 0.5
            if self.verbose:
                print(f"[STT] 한글 비율 {lr:.2f} < {MIN_LANG_RATIO} → 버림: {text!r}",
                      file=sys.stderr)
            self.rejected_lang += 1
            text = ""
        if text:
            self.emit({"type": "speech", "text": text, "duration": dur,
                       "time": round(t - self.t0, 2),
                       "detail": {"stt_model": self.stt.model_size,
                                  "lang_ratio": round(lr, 2)}})
        elif self.stt.model is not None and dur >= VOICE_UNCLEAR_MIN_SEC:
            # 사람 목소리로 분류됐는데 텍스트가 안 나온 구간.
            # 신음·비명·울음처럼 '말이 아닌 소리'일 가능성이 있다.
            # 낙상 직후에 이 이벤트가 오면 긴급도가 높다고 보는 근거가 된다.
            # (음향으로 비명 클래스를 따로 학습하지 않고도 쓸 수 있는 신호)
            self.emit({"type": "voice_unclear", "duration": dur,
                       "time": round(t - self.t0, 2)})

    def _flush_speech(self) -> None:
        if not self.speech_buf:
            return
        wav = np.concatenate(self.speech_buf)
        self.speech_buf.clear()
        if len(wav) < SAMPLE_RATE * 0.4:        # 너무 짧으면 버림
            return
        # 거의 무음인 버퍼를 whisper 에 넣으면 "대대 목욕수수" 같은 환각 문장이 나온다.
        # 소리는 났지만 말로 쓸 수 없는 구간이므로 voice_unclear 로 내보낸다.
        rms = float(np.sqrt(np.mean(wav.astype(np.float64) ** 2)))
        if rms < UTTERANCE_MIN_RMS:
            self.quiet_utterances += 1
            dur = round(len(wav) / SAMPLE_RATE, 2)
            if self.verbose:
                print(f"  [발화 {dur}s] 음량 부족 rms={rms:.5f} → STT 생략", file=sys.stderr)
            if dur >= VOICE_UNCLEAR_MIN_SEC:
                self.emit({"type": "voice_unclear", "duration": dur,
                           "reason": "too_quiet", "rms": round(rms, 5),
                           "time": round(self._now - self.t0, 2)})
            return
        # 앞에 붙인 구간 중 말이 시작되기 전의 빈 부분만 걷어낸다.
        # 길이·음량 판정은 걷어내기 **전**의 버퍼로 한다 (기존 동작을 바꾸지 않기 위해).
        wav = _trim_front(wav)
        if self._stt_q is not None:
            # 가득 차 있으면 **오래된 것부터** 버린다. 방금 한 말이 가장 중요하다.
            while True:
                try:
                    self._stt_q.put_nowait((wav, self._now))
                    break
                except queue.Full:
                    try:
                        self._stt_q.get_nowait()
                        self.stt_dropped += 1
                    except queue.Empty:
                        break
            if self.stt_dropped and self.stt_dropped % 5 == 0:
                print(f"[STT] 대기열이 밀려 오래된 발화를 버리는 중 "
                      f"(누적 {self.stt_dropped}건)", file=sys.stderr)
        else:
            self._transcribe_and_emit(wav, self._now)

    def push(self, window: np.ndarray, hop: np.ndarray, t: float | None = None) -> None:
        """window: 분류용 1초, hop: 새로 들어온 구간(발화 버퍼에 쌓을 부분)
        t: 이 윈도우의 시각. None 이면 self.clock() 을 쓴다."""
        self._now = self.clock() if t is None else t
        self.windows += 1
        pre = list(self._preroll)        # 이 창보다 앞선 hop 들 (아래에서 발화 앞에 붙인다)
        self._preroll.append(hop)

        # ---- 무음 게이트 -----------------------------------------------------
        # 학습 분포 밖(디지털 무음)의 입력은 모델에 넣지 않는다. 넣으면 아무 값이나
        # 나오고, 그게 낙상 임계값을 넘으면 그대로 오탐이 된다. 위 상수 주석 참고.
        self.last_rms_dbfs = rms_dbfs(window)        # LABELS.md audio_pred.detail.rms_dbfs
        if self._is_silent(window):
            self.gated += 1
            if self.verbose:
                print(f"  [{self._now - self.t0:6.1f}s] 무음      "
                      f"{self.last_rms_dbfs:6.1f} dBFS  (분류 생략)", file=sys.stderr)
            self.last_label = SILENCE_LABEL          # 서버가 audio_pred.detail.label 로 쓴다
            self._fall_hist.append(0.0)
            self._silence_hop(hop)
            return

        label, conf, prob = self._classify(window)
        self.last_label = label
        if self.verbose:                     # 디버깅용: 매 윈도우 판정 결과를 보여준다
            print(f"  [{self._now - self.t0:6.1f}s] {CLASS_KO.get(label, label):<8} {conf:.2f}"
                  f"  P(fall)={float(prob[FALL_IDX]):.2f}", file=sys.stderr)

        # ---- 낙상 판정: 최근 K개 규칙 + 재알림 억제 ----------------------------
        # argmax 가 아니라 P(fall) 을 직접 본다 (eval_threshold.py 와 같은 기준).
        p_fall = float(prob[FALL_IDX])
        self._fall_hist.append(p_fall)
        if len(self._fall_hist) < self.fall_k:
            score = 0.0                                   # 아직 K개가 안 모였다
        elif self.fall_k == 1:
            score = p_fall
        elif self.fall_rule == "all":
            score = min(self._fall_hist)
        else:                                             # "avg"
            score = sum(self._fall_hist) / len(self._fall_hist)

        now = self._now
        if (score >= self.fall_threshold
                and float(np.abs(window).max()) >= self.fall_min_peak
                and now - self._last_fall_t >= self.fall_refractory_sec):
            self._last_fall_t = now
            self.emit({"type": "fall", "confidence": round(p_fall, 3),
                       "score": round(score, 3),
                       "time": round(now - self.t0, 2),
                       "probs": {c: round(float(p), 3) for c, p in zip(CLASSES, prob)}})
            # 낙상 직후에는 "괜찮으세요?" 응답을 받아야 하므로 음성 버퍼를 비우지 않는다

        if label == "speech" and conf >= self.speech_threshold:
            if not self.speech_buf and pre and PREROLL_SEC > 0:
                # 발화가 막 시작됐다. 분류가 끝나기 전에 흘러간 앞부분을 되살린다.
                want = int(PREROLL_SEC * SAMPLE_RATE)
                self.speech_buf.append(np.concatenate(pre)[-want:])
            self.speech_buf.append(hop)
            self.silence = 0.0
            if sum(len(b) for b in self.speech_buf) > MAX_UTTERANCE_SEC * SAMPLE_RATE:
                self._flush_speech()
        else:
            self._silence_hop(hop)

    @staticmethod
    def _is_silent(window: np.ndarray) -> bool:
        """사실상 신호가 없는 창인가. 셋 중 하나라도 해당하면 무음으로 본다."""
        if window.size == 0:
            return True
        if float(np.abs(window).max()) < SILENCE_PEAK:
            return True
        if float(np.sqrt(np.mean(window.astype(np.float64) ** 2))) < SILENCE_RMS:
            return True
        return float(np.mean(window == 0.0)) > SILENCE_ZERO_FRAC

    def _silence_hop(self, hop: np.ndarray) -> None:
        """말이 아닌 구간을 처리한다. 발화 중이었다면 종료 여부를 센다."""
        if self.speech_buf:
            self.silence += HOP_SEC
            self.speech_buf.append(hop)              # 끝말이 잘리지 않도록 조금 더 담는다
            if self.silence >= SILENCE_SEC:
                self._flush_speech()
                self.silence = 0.0

    def close(self, timeout: float = 10.0) -> None:
        """남은 발화를 변환하고 STT 스레드를 정리한다."""
        self._flush_speech()
        if self._stt_q is not None:
            self._stt_q.put(None)                       # 종료 신호
            if self._stt_thread is not None:
                self._stt_thread.join(timeout=timeout)
            self._stt_q = None
            self._stt_thread = None


def run_file(path: Path, router: AudioRouter) -> None:
    import soundfile as sf
    from scipy.signal import resample_poly
    wav, sr = sf.read(str(path), dtype="float32", always_2d=True)
    wav = wav.mean(axis=1)
    if sr != SAMPLE_RATE:
        g = np.gcd(int(sr), SAMPLE_RATE)
        wav = resample_poly(wav, SAMPLE_RATE // g, sr // g).astype("float32")
    hop = int(HOP_SEC * SAMPLE_RATE)
    # 파일 입력에서는 '오디오 안에서의 위치'를 시간으로 쓴다
    router.t0 = 0.0
    for start in range(0, max(len(wav) - N_SAMPLES, 0) + 1, hop):
        window = wav[start:start + N_SAMPLES]
        t = (start + N_SAMPLES) / SAMPLE_RATE
        router.push(window, wav[start + N_SAMPLES - hop: start + N_SAMPLES], t=t)
    router.close()


def run_mic(router: AudioRouter) -> None:
    import sounddevice as sd
    hop = int(HOP_SEC * SAMPLE_RATE)
    q: queue.Queue[np.ndarray] = queue.Queue()
    ring = deque(maxlen=N_SAMPLES)
    ring.extend(np.zeros(N_SAMPLES, dtype="float32"))

    def callback(indata, frames, t, status):               # noqa: ANN001
        if status:
            print(f"[audio] {status}", file=sys.stderr)
        q.put(indata[:, 0].copy())

    print("[mic] 듣는 중… Ctrl+C 로 종료", file=sys.stderr)
    filled = 0          # 링 버퍼가 실제 입력으로 다 채워질 때까지 판정하지 않는다
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=hop, callback=callback):
        try:
            while True:
                chunk = q.get()
                ring.extend(chunk)
                if filled < N_SAMPLES:
                    # 시작 직후 1초는 0으로 채운 버퍼와 섞여 가짜 신호가 된다.
                    # 그대로 판정하면 마이크를 켜자마자 낙상 오탐이 난다.
                    filled += len(chunk)
                    continue
                router.push(np.array(ring, dtype="float32"), chunk.astype("float32"))
        except KeyboardInterrupt:
            print("\n[mic] 정리 중…", file=sys.stderr)
            router.close()
            print("[mic] 종료", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", default=str(CKPT_PATH.relative_to(ROOT)))
    ap.add_argument("--file", help="마이크 대신 사용할 wav 경로")
    ap.add_argument("--no-stt", action="store_true", help="STT 끄고 분류만")
    ap.add_argument("--stt-model", default=STT_MODEL,
                    help="tiny / base / small / medium")
    ap.add_argument("--fall-threshold", type=float, default=FALL_THRESHOLD)
    ap.add_argument("--fall-k", type=int, default=FALL_K,
                    help="최근 K개 윈도우를 보고 판정 (1이면 윈도우 하나)")
    ap.add_argument("--fall-rule", default=FALL_RULE, choices=["avg", "all"],
                    help="avg=최근 K개 평균, all=최근 K개 모두 임계값 이상")
    ap.add_argument("--verbose", action="store_true", help="매 윈도우 판정 결과 출력")
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    args = ap.parse_args()

    ckpt = (ROOT / args.ckpt).resolve()
    if not ckpt.exists():
        raise SystemExit(f"가중치가 없습니다: {ckpt}\n먼저 python train.py 를 실행하세요.")

    model = load_model(ckpt, args.device)
    print(f"[model] {ckpt.name} 로드, 클래스={[CLASS_KO[c] for c in CLASSES]}", file=sys.stderr)
    router = AudioRouter(model, SpeechToText(args.stt_model, not args.no_stt),
                         args.device, verbose=args.verbose,
                         fall_threshold=args.fall_threshold,
                         fall_k=args.fall_k, fall_rule=args.fall_rule,
                         stt_async=not args.file)   # 마이크일 때만 STT 를 분리
    rule = "윈도우 1개" if args.fall_k == 1 else f"최근 {args.fall_k}개 {args.fall_rule}"
    print(f"[설정] 낙상 임계값 {args.fall_threshold} / {rule} / "
          f"재알림 억제 {FALL_REFRACTORY_SEC}초", file=sys.stderr)

    if args.file:
        run_file(Path(args.file), router)
    else:
        run_mic(router)


if __name__ == "__main__":
    main()
