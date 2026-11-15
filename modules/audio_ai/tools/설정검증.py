"""설정검증.py — 새 STT 설정이 대본 밖의 말도 잘 받아쓰는지 확인한다.

왜 이걸 또 하는가
-----------------------------------------------------------------
새 설정(hotwords="다온아" + 낱말 프롬프트)은 **시연 대본 녹음으로 골랐다.**
그러면 대본에서만 좋고 다른 말은 나빠질 수 있다. 동작점을 평가셋으로 고르면
그 결과를 믿을 수 없다는 것과 같은 이야기다.

그래서 **고를 때 쓰지 않은 녹음**으로 다시 잰다.

    rec/stt_quiet_base     세션 1 · 조용 · 폰 50cm
    rec/stt_tv_base_vol8   세션 3b · TV 볼륨 8 · 폰 1m

이 녹음의 대본(대본_공통.txt, 18문장)은 시연 대본과 문장이 전혀 다르다.
약·창문·손녀·무릎·점심처럼 겹치는 낱말도 있고, 날씨·파전·노래·텔레비전처럼
프롬프트에 없는 말도 있다. 프롬프트가 없는 낱말을 망치는지 보기에 알맞다.

무엇을 비교하는가
-----------------------------------------------------------------
    A 이전 설정    hotwords="다온"        · 프롬프트 없음
    B 새 설정      hotwords="다온아"      · 낱말 프롬프트 + 베껴쓰기 방어
    C 새 설정(방어 끔)                    방어가 몇 번 걸리는지 보려고

설정값은 코드에 박지 않고 **DAON 의 inference.py 에서 직접 읽는다.** 그래야
"재본 것" 과 "서버가 쓰는 것" 이 어긋나지 않는다.

구간 나누기와 CER 은 기존 stt_설정비교.py 것을 그대로 쓴다. 같은 구간에
설정만 바꿔 넣어야 공정한 비교가 된다. CER 은 이어붙인 전체 글로 재므로
whisper 가 구간을 어떻게 묶든 영향받지 않는다.

쓰는 법 (DAON venv 에서)
-----------------------------------------------------------------
    cd C:\\Users\\hugh\\Desktop\\DAON
    .\\.venv\\Scripts\\Activate.ps1
    python "C:\\Users\\hugh\\Desktop\\audio_ai\\DAON_patch\\설정검증.py"

    python ...\\설정검증.py --녹음 rec\\stt_quiet_base      한 세션만

API 호출 없음. 결과는 `설정검증_결과.txt` 에도 남는다. CPU 로 3~5분.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUDIO_AI = HERE.parent                       # C:\Users\hugh\Desktop\audio_ai
DAON = Path(r"C:\Users\hugh\Desktop\DAON")
if not DAON.exists():
    DAON = AUDIO_AI.parent / "DAON"

sys.path.insert(0, str(HERE))                # stt_설정비교.py
sys.path.insert(0, str(AUDIO_AI))            # inference.py, model.py (구간 나누기용)

기본세션 = ["rec/stt_quiet_base", "rec/stt_tv_base_vol8"]

# 혜원님 text_analyzer.py 와 같은 집합
CALL_WORDS = {"야", "저기요", "저기", "다온아", "다온이", "다온", "다온이야",
              "여보세요", "얘야", "이봐"}
CALL_STT_VARIANTS = {"다오나", "다온나", "다오냐", "다온냐"}


class Tee:
    def __init__(self, path: Path):
        self.f = open(path, "w", encoding="utf-8"); self.out = sys.stdout

    def write(self, s):
        self.out.write(s); self.f.write(s)

    def flush(self):
        self.out.flush(); self.f.flush()


def main() -> None:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--녹음", dest="sessions", default="", help="쉼표로 여러 개")
    args = ap.parse_args()

    tee = Tee(HERE / "설정검증_결과.txt")
    sys.stdout = tee

    import stt_설정비교 as S                                   # 구간 나누기 · CER · 짝짓기

    # 서버가 실제로 쓰는 값을 그대로 가져온다 (여기 따로 적어 두지 않는다)
    sys.path.insert(0, str(DAON / "modules" / "audio_ai"))
    import importlib
    inf = importlib.import_module("inference")
    새핫워드 = inf.SpeechToText.HOTWORDS
    새프롬프트 = getattr(inf.SpeechToText, "INITIAL_PROMPT", None)
    방어 = getattr(inf.SpeechToText, "_prompt_echo", None)

    print("=" * 86)
    print("새 STT 설정 검증 — 고를 때 쓰지 않은 녹음으로")
    print("=" * 86)
    print(f"DAON  {DAON}")
    print(f"새 핫워드     {새핫워드!r}")
    print(f"새 프롬프트   {(새프롬프트 or '(없음)')[:60]}…")
    print(f"베껴쓰기 방어 {'있음' if 방어 else '없음 (inference.py 에 아직 안 들어갔습니다)'}")
    if 새프롬프트 is None:
        print("\n★ inference.py 에 INITIAL_PROMPT 가 없습니다. 먼저 코드를 넣어야 합니다.")
        return

    설정들 = [
        ("A 이전 설정",        dict(hotwords="다온"),                          False),
        ("B 새 설정",          dict(hotwords=새핫워드, initial_prompt=새프롬프트), True),
        ("C 새 설정(방어 끔)",  dict(hotwords=새핫워드, initial_prompt=새프롬프트), False),
    ]

    from faster_whisper import WhisperModel
    모델 = None

    세션들 = [s.strip() for s in args.sessions.split(",") if s.strip()] or 기본세션
    총 = {}

    for 세션 in 세션들:
        폴더 = (AUDIO_AI / 세션) if not Path(세션).is_absolute() else Path(세션)
        if not 폴더.exists():
            print(f"\n★ 없는 폴더: {폴더}")
            continue
        wavs = sorted(폴더.glob("mic_*.wav"))
        if not wavs:
            print(f"\n★ mic_*.wav 가 없습니다: {폴더}")
            continue

        대본, 대본경로 = S.load_script(폴더, None)
        print("\n" + "=" * 86)
        print(f"{폴더.name}")
        print("=" * 86)
        조건 = 폴더 / "조건.txt"
        if 조건.exists():
            print("  " + 조건.read_text(encoding="utf-8", errors="replace").strip().splitlines()[0])
        print(f"  대본 {대본경로.name} · {len(대본)}문장 · wav {len(wavs)}개")

        # segment() 는 (파일별 목록, 짧아서 버린 수, 조용해서 버린 수) 를 돌려준다
        결과, 짧음, 조용 = S.segment(wavs)
        구간 = [a for _, found, _ in 결과 for a in found]
        print(f"  구간 {len(구간)}개 (대본 {len(대본)}문장)"
              + (f" · 짧아서 버림 {짧음} · 조용해서 버림 {조용}" if (짧음 or 조용) else ""))
        if not 구간:
            continue
        정답 = " ".join(대본)

        for 이름, kw, 방어쓰기 in 설정들:
            if 모델 is None:
                print("   (base 모델 로드…)")
                import torch
                dev = "cuda" if torch.cuda.is_available() else "cpu"
                모델 = WhisperModel("base", device=dev,
                                   compute_type="float16" if dev == "cuda" else "int8")
            t0 = time.time()
            받음, 버린수 = [], 0
            for a in 구간:
                opts = dict(language="ko", beam_size=1, vad_filter=True,
                            condition_on_previous_text=False, no_speech_threshold=0.5, **kw)
                it, _ = 모델.transcribe(a, **opts)
                t = " ".join(x.text.strip() for x in it).strip()
                if 방어쓰기 and 방어 and 방어(t):
                    버린수 += 1
                    t = ""
                받음.append(t)
            걸림 = (time.time() - t0) / len(구간)
            c = S.cer(정답, " ".join(x for x in 받음 if x))
            빈것 = sum(1 for x in 받음 if not x)
            깬것 = sum(1 for x in 받음
                      if (w := x.split()) and (w[0].strip(",.?!") in CALL_WORDS
                                               or w[0].strip(",.?!") in CALL_STT_VARIANTS))
            print(f"\n  ■ {이름}")
            print(f"     CER {c*100:5.1f}%  ·  발화당 {걸림:.2f}초  ·  빈 결과 {빈것}개"
                  + (f"  ·  방어로 버림 {버린수}개" if 방어쓰기 else "")
                  + f"  ·  호출어로 시작한 구간 {깬것}개")
            총.setdefault(이름, []).append((폴더.name, c, 걸림, 버린수))
            for i, 문장, got in S.align(대본, 받음):
                if i < 0:
                    if got:
                        print(f"       (대본 밖) {got[:58]}")
                    continue
                cc = S.cer(문장, got)
                if cc > 0.001:
                    print(f"       CER{cc*100:4.0f}%  {문장[:46]}")
                    print(f"               → {got[:58] or '(못 받아씀)'}")

    print("\n" + "=" * 86)
    print("요약")
    print("=" * 86)
    print(f"  {'설정':<20}{'세션':<22}{'CER':>8}{'발화당':>9}{'버림':>7}")
    for 이름, rows in 총.items():
        for 세션, c, s, bk in rows:
            print(f"  {이름:<20}{세션:<22}{c*100:7.1f}%{s:7.2f}초{bk:>6}개")
    if "A 이전 설정" in 총 and "B 새 설정" in 총:
        print("\n  이전 → 새 설정")
        for (세션, a, sa, _), (_, b, sb, bk) in zip(총["A 이전 설정"], 총["B 새 설정"]):
            판정 = "좋아짐" if b < a - 0.005 else ("나빠짐 ★" if b > a + 0.005 else "비슷")
            print(f"    {세션:<22}CER {a*100:5.1f}% → {b*100:5.1f}%  ({b*100-a*100:+.1f}p)"
                  f"   지연 {sa:.2f}→{sb:.2f}초   {판정}")
    print("\n읽는 법")
    print("  · 새 설정이 '나빠짐' 이면 프롬프트가 대본 밖의 말을 해치고 있다는 뜻이다.")
    print("    그러면 시연 대본만 쓰는 설정으로 두거나 프롬프트를 줄여야 한다")
    print("  · '방어로 버림' 이 많으면 프롬프트 베껴쓰기가 실제로 자주 일어난다는 뜻이다.")
    print("    버린 발화는 못 들은 셈이 되므로, 많으면 프롬프트를 바꿔야 한다")
    print("  · B 와 C 의 CER 차이가 크면 방어가 실제로 일을 하고 있다는 뜻이다")

    sys.stdout = tee.out
    print(f"\n저장: {HERE / '설정검증_결과.txt'}")


if __name__ == "__main__":
    main()
