"""응답속도.py — 어르신이 말을 멈춘 뒤 로봇이 말하기까지, 어디서 몇 초 쓰는가.

왜 재는가
-----------------------------------------------------------------
회의에서 "응답 속도가 느리다" 는 말이 나왔지만 **어디가 느린지는 아무도 모른다.**
숫자가 없으면 LLM 을 끌지, 모델을 바꿀지, 대기 시간을 줄일지 고를 수 없다.

응답은 이런 순서로 흐른다. 괄호 안은 코드에 박혀 있는 고정 대기다.

    어르신이 말을 멈춤
      ↓  발화 종료 판정      SILENCE_SEC 1.0초 + hop 0.5초 격자  → 1.0~1.5초
      ↓  받아쓰기(STT)       whisper base
      ↓  버스 → 대화 모듈     COALESCE_SEC 0.25초 (이벤트 합치기 창)
      ↓  대화 판단           규칙은 거의 0초
      ↓  LLM (켜져 있으면)    DIALOGUE_LLM_TIMEOUT 2.5초까지 기다린다
      ↓  음성                미리 만든 것은 0초, 처음 나오는 문장은 OpenAI 호출
    로봇이 말하기 시작

고정 대기만 2.3초쯤 된다. 그래서 두 가지를 같이 본다.

  1부  각 구간이 실제로 몇 초인가 (STT · 대화 · LLM · 음성)
  2부  고정 대기를 줄여도 발화가 쪼개지지 않는가
       SILENCE_SEC 을 1.0 → 0.7 로 줄이면 응답마다 0.3초를 그냥 번다.
       대신 말 중간에 쉬는 구간에서 문장이 잘릴 수 있다. 그 경계를 찾는다.

쓰는 법 (DAON venv 에서)
-----------------------------------------------------------------
    cd C:\\Users\\hugh\\Desktop\\DAON
    .\\.venv\\Scripts\\Activate.ps1
    python "C:\\Users\\hugh\\Desktop\\audio_ai\\DAON_patch\\응답속도.py"

    python ...\\응답속도.py --돈안씀     LLM·음성 호출 없이 (STT 와 2부만)
    python ...\\응답속도.py --2부만      고정 대기만 빠르게 보기

주의: --돈안씀 을 안 주면 LLM 12회 + 음성 몇 회가 교수님 키로 호출됩니다.
      짧은 문장이라 수십 원 수준입니다. 결과는 `응답속도_결과.txt` 에도 남습니다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUDIO_AI = HERE.parent
DAON = Path(r"C:\Users\hugh\Desktop\DAON")
if not DAON.exists():
    DAON = AUDIO_AI.parent / "DAON"

발화폴더 = AUDIO_AI / "rec" / "대본발화"
세션 = AUDIO_AI / "rec" / "stt_quiet_base"

COALESCE_SEC = 0.25          # server/adapters/dialogue.py
TICK = 0.25                  # 같은 루프가 LLM 응답도 집어 온다


class Tee:
    def __init__(self, path: Path):
        self.f = open(path, "w", encoding="utf-8"); self.out = sys.stdout

    def write(self, s):
        self.out.write(s); self.f.write(s)

    def flush(self):
        self.out.flush(); self.f.flush()


def wav읽기(path: Path):
    import numpy as np
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


# ═══════════════════════ 1부 · 구간별 시간 ═══════════════════════
def 일부(돈안씀: bool) -> None:
    import numpy as np
    sys.path.insert(0, str(DAON / "modules" / "audio_ai"))
    sys.path.insert(0, str(DAON / "modules" / "dialogue" / "src"))
    import inference as INF

    목록 = json.loads((발화폴더 / "대본.json").read_text(encoding="utf-8"))
    print("=" * 88)
    print("1부 · 구간별 시간")
    print("=" * 88)

    # ── STT (서버가 쓰는 설정 그대로)
    stt = INF.SpeechToText()
    if stt.model is None:
        print("★ whisper 를 못 띄웠습니다.")
        return
    print(f"STT  모델 {stt.model_size} · 핫워드 {stt.HOTWORDS!r}"
          f" · 프롬프트 {'있음' if getattr(stt, 'INITIAL_PROMPT', None) else '없음'}")

    stt_시간, 받은글 = [], []
    for r in 목록:
        a = wav읽기(발화폴더 / r["파일"])
        t0 = time.time()
        t = stt.transcribe(a)
        stt_시간.append(time.time() - t0)
        받은글.append(t)
    print(f"     발화 {len(목록)}개 · 평균 {np.mean(stt_시간):.2f}초"
          f" · 가장 느린 것 {max(stt_시간):.2f}초\n")

    # ── 대화 판단
    from notifier import load_env
    env = DAON / "modules" / "dialogue" / ".env"
    try:
        load_env(str(env))
    except Exception as e:                                       # noqa: BLE001
        print(f"(.env 를 못 읽었습니다: {e})")
    import os
    llm설정 = os.environ.get("DIALOGUE_LLM", "off")
    print(f"대화  .env DIALOGUE_LLM={llm설정}"
          f" · 모델 {os.environ.get('DIALOGUE_LLM_MODEL','-')}"
          f" · 제한 {os.environ.get('DIALOGUE_LLM_TIMEOUT','-')}초")

    def 돌리기(llm켬: bool):
        """서버와 같게: 발화를 넣고, 응답이 없으면 0.25초마다 두드린다."""
        keep = os.environ.get("DIALOGUE_LLM")
        os.environ["DIALOGUE_LLM"] = llm설정 if llm켬 else "off"
        for m in ("dialogue_algorithm",):
            sys.modules.pop(m, None)
        from dialogue_algorithm import DialogueEngine, DialogueInput
        eng = DialogueEngine()
        산다 = eng._llm is not None
        rows = []
        t = time.time()
        for r, said in zip(목록, 받은글):
            t += 12.0
            w0 = time.time()
            out = eng.process(DialogueInput(timestamp=t, pose="normal", emotion="neutral",
                                            stt=said or r["대본"], sound_event=None,
                                            motion="moving"))
            두드림 = 0
            while (not (out.response_text or "").strip()) and 두드림 < 20:
                time.sleep(TICK)
                t += TICK
                두드림 += 1
                out = eng.process(DialogueInput(timestamp=t, pose="normal", emotion="neutral",
                                                stt=None, sound_event=None, motion="moving"))
            rows.append({"대본": r["대본"],
                         "글": (out.response_text or "").strip(),
                         "초": time.time() - w0,
                         "두드림": 두드림,
                         "출처": getattr(out, "response_source", "") or ""})
        if keep is None:
            os.environ.pop("DIALOGUE_LLM", None)
        else:
            os.environ["DIALOGUE_LLM"] = keep
        return rows, 산다

    규칙, _ = 돌리기(False)
    print(f"     규칙만(LLM 끔)  평균 {np.mean([r['초'] for r in 규칙])*1000:.0f}ms"
          f" · 가장 느린 것 {max(r['초'] for r in 규칙)*1000:.0f}ms")

    llm = None
    if not 돈안씀:
        llm, 산다 = 돌리기(True)
        if not 산다:
            print("     ★ LLM 이 꺼져 있습니다 (.env 확인)")
            llm = None
        else:
            쓴것 = [r for r in llm if r["출처"] == "llm"]
            print(f"     LLM 켬          평균 {np.mean([r['초'] for r in llm]):.2f}초"
                  f" · 가장 느린 것 {max(r['초'] for r in llm):.2f}초"
                  f" · LLM 이 답한 것 {len(쓴것)}/{len(llm)}개")
            if 쓴것:
                print(f"       LLM 이 답한 것만: 평균 {np.mean([r['초'] for r in 쓴것]):.2f}초"
                      f" · 두드린 횟수 평균 {np.mean([r['두드림'] for r in 쓴것]):.1f}회")
    print()

    # ── 음성
    man = DAON / "web" / "tts" / "cedar" / "manifest.json"
    맞음 = 없음 = 0
    if man.exists():
        import re
        m = json.loads(man.read_text(encoding="utf-8"))["map"]
        표 = {re.sub(r"\s+", "", k): v for k, v in m.items()}
        대상 = llm or 규칙
        for r in 대상:
            if re.sub(r"\s+", "", r["글"]) in 표:
                맞음 += 1
            else:
                없음 += 1
        print(f"음성  미리 만든 것에 있음 {맞음}개 (0초) · 없음 {없음}개 (서버가 만든다)")
    else:
        print(f"음성  manifest 가 없습니다: {man}")

    만듦 = None
    if not 돈안씀 and 없음:
        만듦 = 음성시간(DAON)
        if 만듦:
            print(f"      처음 만들 때 {만듦:.2f}초 (저장되므로 두 번째부터는 0초)")
    print()

    # ── 합계
    print("─" * 88)
    print("한 번 응답하는 데 (어르신이 말을 멈춘 순간부터)")
    print("─" * 88)
    stt평균 = float(np.mean(stt_시간))
    대화평균 = float(np.mean([r["초"] for r in (llm or 규칙)]))
    항목 = [
        ("발화 종료 판정", INF.SILENCE_SEC, INF.SILENCE_SEC + INF.HOP_SEC,
         f"SILENCE_SEC {INF.SILENCE_SEC}초 + hop {INF.HOP_SEC}초 격자"),
        ("받아쓰기(STT)", stt평균, max(stt_시간), f"whisper {stt.model_size}"),
        ("버스 → 대화 모듈", 0.0, COALESCE_SEC, f"COALESCE_SEC {COALESCE_SEC}초"),
        ("대화 판단" + (" + LLM" if llm else ""), 대화평균, 대화평균,
         "LLM 켬" if llm else "규칙만"),
    ]
    if 맞음 or 없음:
        항목.append(("음성", 0.0, 만듦 or 0.0,
                     f"미리 만든 것 {맞음}개 / 새로 만들 것 {없음}개"))
    print(f"  {'구간':<22}{'빠를 때':>9}{'느릴 때':>9}   근거")
    for 이름, lo, hi, 근거 in 항목:
        print(f"  {이름:<22}{lo:8.2f}초{hi:8.2f}초   {근거}")
    print(f"  {'─'*20:<22}{'─'*8:>9}{'─'*8:>9}")
    print(f"  {'합계':<22}{sum(x[1] for x in 항목):8.2f}초{sum(x[2] for x in 항목):8.2f}초")
    print()
    print("  ※ 폰이 소리를 내기 시작하는 시점까지다. 말을 다 읽는 시간은 별도.")


def 음성시간(DAON: Path) -> float | None:
    """미리 만든 것에 없는 문장을 서버처럼 한 번 만들어 보고 시간을 잰다."""
    import os
    import urllib.request
    키 = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not 키:
        env = DAON / "modules" / "dialogue" / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                if line.strip().startswith("OPENAI_API_KEY="):
                    키 = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not 키:
        return None
    body = {"model": "gpt-4o-mini-tts", "voice": "cedar",
            "input": "지금은 오후 세 시 이십 분이에요. 점심은 드셨나요?",
            "response_format": "mp3",
            "instructions": ("혼자 사시는 어르신에게 말하듯, 따뜻하고 차분하게. "
                             "또박또박 천천히, 다정하지만 과장되지 않게.")}
    req = urllib.request.Request("https://api.openai.com/v1/audio/speech",
                                 data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {키}",
                                          "Content-Type": "application/json"},
                                 method="POST")
    try:
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        return time.time() - t0
    except Exception as e:                                       # noqa: BLE001
        print(f"      (음성 호출 실패: {type(e).__name__}: {str(e)[:60]})")
        return None


# ═══════════════════════ 2부 · 고정 대기를 줄일 수 있는가 ═══════════════════════
def 이부() -> None:
    import numpy as np
    import soundfile as sf
    sys.path.insert(0, str(DAON / "modules" / "audio_ai"))
    import inference as INF
    from config import N_SAMPLES, SAMPLE_RATE
    from model import load_model
    import torch

    print("\n" + "=" * 88)
    print("2부 · 발화 종료 대기를 줄여도 문장이 쪼개지지 않는가")
    print("=" * 88)
    wavs = sorted(세션.glob("mic_*.wav"))
    대본파일 = HERE / "대본_공통.txt"
    문장수 = len([l for l in 대본파일.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.startswith("#")]) if 대본파일.exists() else 18
    if not wavs:
        print(f"★ 녹음이 없습니다: {세션}")
        return
    print(f"녹음 {세션.name} · wav {len(wavs)}개 · 대본 {문장수}문장")
    print("구간이 대본 문장 수보다 많으면 문장이 쪼개진 것, 적으면 붙은 것이다.\n")

    모델 = load_model(INF.CKPT_PATH, "cuda" if torch.cuda.is_available() else "cpu")
    원래silence, 원래hop = INF.SILENCE_SEC, INF.HOP_SEC

    class Catcher(INF.AudioRouter):
        def _flush_speech(self):
            if not self.speech_buf:
                return
            wav = np.concatenate(self.speech_buf)
            self.speech_buf.clear()
            if len(wav) < SAMPLE_RATE * 0.4:
                self.짧음 += 1
                return
            if float(np.sqrt(np.mean(wav.astype(np.float64) ** 2))) < INF.UTTERANCE_MIN_RMS:
                self.조용 += 1
                return
            self.찾음.append(len(wav) / SAMPLE_RATE)

    def 세기(silence: float, hop: float):
        INF.SILENCE_SEC, INF.HOP_SEC = silence, hop
        h = int(hop * SAMPLE_RATE)
        전체, 짧음, 조용 = [], 0, 0
        for p in wavs:
            x, _ = sf.read(str(p), dtype="float32")
            if x.ndim > 1:
                x = x.mean(1)
            r = Catcher(모델, INF.SpeechToText(enabled=False), "cpu", stt_async=False)
            r.찾음, r.짧음, r.조용 = [], 0, 0
            r.t0 = 0.0
            for i in range(0, max(len(x) - N_SAMPLES + 1, 1), h):
                w = x[i:i + N_SAMPLES]
                if len(w) < N_SAMPLES:
                    break
                r.push(w, w[-h:], t=(i + N_SAMPLES) / SAMPLE_RATE)
            r._flush_speech()
            전체 += r.찾음; 짧음 += r.짧음; 조용 += r.조용
        return 전체, 짧음, 조용

    print(f"  {'SILENCE':>8}{'hop':>6}{'구간':>7}{'대본대비':>10}{'발화 평균':>11}{'아낀 시간':>11}")
    기준 = 원래silence + 원래hop
    표 = []
    for silence, hop in [(0.6, 0.5), (0.7, 0.5), (0.8, 0.5), (1.0, 0.5), (1.2, 0.5),
                         (0.7, 0.25), (0.8, 0.25), (1.0, 0.25)]:
        전체, 짧음, 조용 = 세기(silence, hop)
        차 = len(전체) - 문장수
        아낌 = 기준 - (silence + hop)
        표시 = "맞음" if 차 == 0 else (f"+{차} 쪼개짐" if 차 > 0 else f"{차} 붙음")
        이것 = "  ← 지금" if (silence, hop) == (원래silence, 원래hop) else ""
        print(f"  {silence:8.2f}{hop:6.2f}{len(전체):7}{표시:>10}"
              f"{np.mean(전체) if 전체 else 0:10.2f}초{아낌:+10.2f}초{이것}")
        표.append((silence, hop, 차, 아낌))

    INF.SILENCE_SEC, INF.HOP_SEC = 원래silence, 원래hop
    맞는것 = [r for r in 표 if r[2] == 0]
    if 맞는것:
        좋은것 = max(맞는것, key=lambda r: r[3])
        if 좋은것[3] > 0.01:
            print(f"\n  → 문장이 쪼개지지 않는 가장 짧은 대기: "
                  f"SILENCE_SEC {좋은것[0]} · hop {좋은것[1]}")
            print(f"     응답마다 {좋은것[3]:.2f}초를 그냥 번다. 정확도는 그대로다.")
        else:
            print("\n  → 지금 값이 이미 가장 짧다. 여기서는 더 줄일 수 없다.")
    else:
        print("\n  → 어느 값도 대본 문장 수와 맞지 않는다. 이 녹음으로는 판단할 수 없다.")
    print("\n  주의: hop 을 줄이면 분류를 그만큼 더 자주 돌린다. CPU 사용이 늘어난다.")
    print("        한 세션만으로 정하지 말고, TV 소음 세션으로도 한 번 더 봐야 한다.")


def main() -> None:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--돈안씀", dest="free", action="store_true")
    ap.add_argument("--2부만", dest="only2", action="store_true")
    ap.add_argument("--1부만", dest="only1", action="store_true")
    args = ap.parse_args()

    tee = Tee(HERE / "응답속도_결과.txt")
    sys.stdout = tee
    try:
        if not args.only2:
            일부(args.free)
        if not args.only1:
            이부()
    finally:
        sys.stdout = tee.out
        print(f"\n저장: {HERE / '응답속도_결과.txt'}")


if __name__ == "__main__":
    main()
