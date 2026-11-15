"""대본STT2.py — 두 번째 비교. '로봇이 깨어나는가' 를 제대로 따진다.

1차에서 알게 된 것
-----------------------------------------------------------------
1) 지금 설정은 "다온아" 를 **"다온어"** 로 받아쓴다. 그런데 혜원님 코드의

       CALL_WORDS        = {"야","저기요","저기","다온아","다온이","다온",
                            "다온이야","여보세요","얘야","이봐"}
       CALL_STT_VARIANTS = {"다오나","다온나","다오냐","다온냐"}

   에 "다온어" 가 없다. 즉 **불러도 로봇이 깨어나지 않는다.**
   1차에서 내가 "다온 글자가 있으면 인식" 으로 셌던 건 틀린 기준이었다.
   이번에는 위 집합으로 직접 판정한다.

2) initial_prompt 은 CER 을 14.9% → 7.9% 로 낮췄다 (점심은·화장실·손녀한테
   가 고쳐졌다). 쓸 가치가 있다.

3) 그런데 initial_prompt 은 **짧은 발화에서 프롬프트를 베껴 쓴다.**

       다온아. (0.9초)  →  "다온 어르신과 돌봄 다온이 나누는"   CER 400%

   받아쓸 내용이 적으면 모델이 앞 문맥을 읽어버린다. "다온아", "응",
   "고마워" 같은 짧은 말은 실제 대화에서 흔하므로 그냥 둘 수 없다.

무엇을 하는가
-----------------------------------------------------------------
  · 핫워드를 "다온아" 로 바꾼 조합들을 비교한다 (1차에선 "다온" 으로만 봤다)
  · 프롬프트를 문장형 / 낱말목록형 / 짧은목록형으로 나눠 베껴쓰기 차이를 본다
  · **베껴쓰기 방어**를 넣은 결과도 같이 낸다.
    프롬프트 낱말이 프롬프트 순서대로 3개 이상 이어 나오면 헛소리로 보고 버린다.
    서버에 넣을 코드와 같은 판정이다.
  · 발화마다 **깨어나는가(호출 판정)** 를 찍는다. CER 보다 이게 먼저다

쓰는 법
-----------------------------------------------------------------
    cd C:\\Users\\hugh\\Desktop\\DAON
    .\\.venv\\Scripts\\Activate.ps1
    python "C:\\Users\\hugh\\Desktop\\audio_ai\\DAON_patch\\대본STT2.py"

    python ...\\대본STT2.py --빠르게      small 건너뛰기

API 호출 없음. 결과는 `대본STT2_결과.txt` 에도 남는다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import wave
from pathlib import Path

REC = Path(r"C:\Users\hugh\Desktop\audio_ai\rec\대본발화")
if not REC.exists():
    REC = Path(__file__).resolve().parent.parent / "rec" / "대본발화"

기본 = dict(language="ko", beam_size=1, vad_filter=True,
            condition_on_previous_text=False, no_speech_threshold=0.5)

# 혜원님 text_analyzer.py 와 같은 집합 (판정을 똑같이 하려고 그대로 옮겼다)
CALL_WORDS = {"야", "저기요", "저기", "다온아", "다온이", "다온", "다온이야",
              "여보세요", "얘야", "이봐"}
CALL_STT_VARIANTS = {"다오나", "다온나", "다오냐", "다온냐"}
CALL_PREFIXES = ("다온아", "다온이", "저기요", "야")        # "다온" 은 접두어가 아니다

P문장 = ("어르신과 돌봄 로봇 다온이 나누는 대화입니다. "
         "쓸쓸하다, 그럭저럭, 손녀, 김치찌개, 된장찌개, 약, 창문, "
         "화장실, 점심, 주말 같은 말이 나옵니다.")
P목록 = "다온아, 쓸쓸하다, 그럭저럭, 손녀, 김치찌개, 된장찌개, 약, 창문, 화장실, 점심, 주말"
P짧음 = "손녀, 김치찌개, 된장찌개, 약, 화장실, 점심"

설정들 = [
    ("1 지금 설정",            dict(hotwords="다온")),
    ("2 핫워드 다온아",         dict(hotwords="다온아")),
    ("3 다온아+낱말목록",       dict(hotwords="다온아", initial_prompt=P목록)),
    ("4 다온아+낱말문장",       dict(hotwords="다온아", initial_prompt=P문장)),
    ("5 다온아+짧은목록",       dict(hotwords="다온아", initial_prompt=P짧음)),
    ("6 다온아+목록+beam5",    dict(hotwords="다온아", initial_prompt=P목록, beam_size=5)),
    ("7 낱말목록만",           dict(initial_prompt=P목록)),
    ("8 핫워드 여러낱말",       dict(hotwords="다온아 손녀 김치찌개 화장실")),
    ("9 small+다온아",         dict(hotwords="다온아", _모델="small")),
    ("10 small+다온아+목록",    dict(hotwords="다온아", initial_prompt=P목록, _모델="small")),
]
느린것 = {"9", "10"}


# ───────────────── 베껴쓰기 방어 (서버에 넣을 판정과 같다) ─────────────────
# 글자 토막으로 잡으려 했더니 실제 사례를 놓쳤다.
#     프롬프트  "어르신과 돌봄 로봇 다온이 나누는 대화입니다…"
#     받아쓴 것 "다온 어르신과 돌봄 다온이 나누는"
# 모델이 '로봇' 을 빼먹어서 이어진 글자가 끊긴다. 그래서 글자가 아니라
# **프롬프트 낱말이 프롬프트 순서대로 줄줄이 나오는지**로 본다.
#
# 낱말 하나나 둘이 겹치는 건 당연하다 — 프롬프트는 나올 낱말을 적어 둔 것이니까.
# 그러나 세 개 이상이 적어 둔 순서대로 이어 나오면 모델이 프롬프트를 읽고 있는 것이다.
# 틀려도 그 발화를 '못 들은 것' 으로 치는 쪽이, 헛소리를 대화에 넣는 쪽보다 낫다.
연속_기준 = 3

def 낱말들(s: str) -> list[str]:
    return [w for w in re.split(r"[,\.\s]+", re.sub(r"[^\w가-힣,\.\s]", "", s or "")) if w]


def 베껴썼나(받음: str, prompt: str | None) -> bool:
    if not prompt or not 받음:
        return False
    순서 = 낱말들(prompt)
    받은글 = re.sub(r"\s+", "", 정규화(받음))
    최장 = 이어짐 = 0
    건너뜀 = 0
    for w in 순서:
        if 정규화(w) and 정규화(w) in 받은글:
            이어짐 += 1 + 건너뜀 * 0        # 건너뛴 낱말은 세지 않되 끊지도 않는다
            건너뜀 = 0
            최장 = max(최장, 이어짐)
        elif 이어짐 and 건너뜀 == 0:
            건너뜀 = 1                      # 한 낱말 빠뜨린 것은 눈감아 준다
        else:
            이어짐 = 건너뜀 = 0
    return 최장 >= 연속_기준


# ───────────────────── 재는 법 ─────────────────────
def 정규화(s: str) -> str:
    return re.sub(r"[^\w가-힣]", "", s or "").replace("_", "")


def 거리(a: str, b: str) -> int:
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def 깨어나나(받음: str) -> bool:
    """혜원님 판정과 같게: 짧은 발화(6자 이내)가 호출어 집합에 있으면 호출."""
    t = re.sub(r"[^\w가-힣\s]", "", 받음 or "").strip()
    낱말 = t.split()
    if len(낱말) == 1 and len(낱말[0]) <= 6:
        return 낱말[0] in CALL_WORDS or 낱말[0] in CALL_STT_VARIANTS
    return bool(낱말) and 낱말[0] in CALL_PREFIXES


def 틀린낱말(정답: str, 받음: str) -> list[str]:
    a, b = 정답.split(), 받음.split()
    out = [f"{w}→{b[i] if i < len(b) else '(없음)'}"
           for i, w in enumerate(a) if 정규화(w) != 정규화(b[i] if i < len(b) else "")]
    if len(b) > len(a):
        out.append(f"(남는 말: {' '.join(b[len(a):])[:26]})")
    return out


def wav읽기(path: Path):
    import numpy as np
    with wave.open(str(path), "rb") as w:
        if (w.getnchannels(), w.getframerate(), w.getsampwidth()) != (1, 16000, 2):
            raise SystemExit(f"{path.name}: 16kHz 모노 16bit 이어야 합니다")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


class Tee:
    def __init__(self, path: Path):
        self.f = open(path, "w", encoding="utf-8"); self.out = sys.stdout

    def write(self, s):
        self.out.write(s); self.f.write(s)

    def flush(self):
        self.out.flush(); self.f.flush()


# ───────────────────── 본체 ─────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--설정", dest="pick", default="")
    ap.add_argument("--빠르게", dest="fast", action="store_true")
    args = ap.parse_args()

    tee = Tee(Path(__file__).resolve().parent / "대본STT2_결과.txt")
    sys.stdout = tee

    목록파일 = REC / "대본.json"
    if not 목록파일.exists():
        print(f"발화 파일을 찾지 못했습니다: {REC}")
        return
    목록 = json.loads(목록파일.read_text(encoding="utf-8"))

    from faster_whisper import WhisperModel
    import faster_whisper
    print(f"faster-whisper {faster_whisper.__version__}")
    try:
        import torch
        gpu = torch.cuda.is_available()
    except Exception:                                            # noqa: BLE001
        gpu = False
    dev, comp = ("cuda", "float16") if gpu else ("cpu", "int8")
    print(f"장치 {dev}/{comp}")
    print(f"발화 {len(목록)}개 · 대본 {sum(len(정규화(r['대본'])) for r in 목록)}자")
    print(f"\n호출어 판정: CALL_WORDS {len(CALL_WORDS)}개 + 변형 {len(CALL_STT_VARIANTS)}개")
    print(f"베껴쓰기 방어: 프롬프트 낱말이 순서대로 {연속_기준}개 이상 이어 나오면 버린다\n")

    소리 = {r["파일"]: wav읽기(REC / r["파일"]) for r in 목록}
    캐시 = {}

    def 모델(size):
        if size not in 캐시:
            print(f"   ({size} 모델 로드…)")
            캐시[size] = WhisperModel(size, device=dev, compute_type=comp)
        return 캐시[size]

    고를것 = {s.strip() for s in args.pick.split(",") if s.strip()}
    돌릴것 = []
    for 이름, kw in 설정들:
        n = 이름.split()[0]
        if (고를것 and n not in 고를것) or (args.fast and n in 느린것):
            continue
        돌릴것.append((이름, dict(kw)))

    표 = []
    for 이름, kw in 돌릴것:
        size = kw.pop("_모델", "base")
        prompt = kw.get("initial_prompt")
        print("\n" + "=" * 88)
        print(f"{이름}    {size} · " +
              ", ".join(f"{k}={str(v)[:26]}" for k, v in kw.items()) or "기본값")
        print("=" * 88)
        정답수 = 틀림 = 맞은줄 = 베낀수 = 0
        초 = 0.0
        깸 = None
        for r in 목록:
            t0 = time.time()
            segs, _ = 모델(size).transcribe(소리[r["파일"]], **{**기본, **kw})
            원본 = " ".join(s.text.strip() for s in segs).strip()
            dt = time.time() - t0
            초 += dt
            베낌 = 베껴썼나(원본, prompt)
            베낀수 += 베낌
            받음 = "" if 베낌 else 원본                 # 방어가 걸리면 버린다
            g = 정규화(r["대본"])
            d = 거리(g, 정규화(받음))
            정답수 += len(g); 틀림 += d
            ok = d == 0
            맞은줄 += ok
            if r["파일"] == "낙상_01.wav":
                깸 = 깨어나나(받음)
            꼬리 = ""
            if r["파일"] == "낙상_01.wav":
                꼬리 = "   [깨어남]" if 깸 else "   [★ 안 깨어남]"
            표시 = " 일치  " if ok else f"CER{d/max(len(g),1)*100:4.0f}%"
            print(f"  {표시} {dt:4.1f}초  {r['대본']}{꼬리}")
            if 베낌:
                print(f"            ★ 프롬프트 베껴씀 → 버림: {원본[:52]}")
            elif not ok:
                print(f"            → {원본 or '(못 받아씀)'}")
                w = 틀린낱말(r["대본"], 원본)
                if w:
                    print(f"              {' · '.join(w)}")
        cer = 틀림 / max(정답수, 1) * 100
        print(f"\n  → CER {cer:.1f}%  ·  맞은 줄 {맞은줄}/{len(목록)}"
              f"  ·  깨어남 {'O' if 깸 else '★X'}"
              f"  ·  베껴쓰기 {베낀수}건  ·  발화당 {초/len(목록):.2f}초")
        표.append((이름, cer, 맞은줄, len(목록), 깸, 베낀수, 초 / len(목록)))

    print("\n" + "=" * 88)
    print("요약")
    print("=" * 88)
    print(f"  {'설정':<22}{'깨어남':>7}{'CER':>9}{'맞은 줄':>10}{'베껴씀':>8}{'발화당':>9}")
    for 이름, c, ok, tot, 깸, bk, s in 표:
        print(f"  {이름:<22}{'O' if 깸 else 'X':>6}{c:8.1f}%{ok:>7}/{tot:<3}"
              f"{bk:>6}건{s:7.2f}초")
    쓸만 = [r for r in 표 if r[4]]
    if 쓸만:
        고름 = min(쓸만, key=lambda r: (r[1], r[6]))
        print(f"\n  깨어나는 것 중 CER 이 가장 낮은 설정:  {고름[0]}"
              f"   CER {고름[1]:.1f}% · 발화당 {고름[6]:.2f}초")
    else:
        print("\n  ★ 깨어나는 설정이 없습니다. 호출어 변형을 코드에 추가해야 합니다.")
    print("\n읽는 법")
    print("  · 깨어남이 X 면 못 쓴다. 불러도 로봇이 반응하지 않는다")
    print("  · 베껴쓰기 건수가 0 이 아니면 그 설정은 짧은 발화에서 위험하다")
    print("    (방어로 버리긴 하지만, 버리면 그 발화는 못 들은 셈이 된다)")
    print("  · 발화당 시간은 응답 속도에 그대로 더해진다")
    print("  · 고른 뒤 data/realworld 로 한 번 더 돌려 다른 말이 나빠지지 않는지 본다")

    sys.stdout = tee.out
    print(f"\n저장: {Path(__file__).resolve().parent / '대본STT2_결과.txt'}")


if __name__ == "__main__":
    main()
