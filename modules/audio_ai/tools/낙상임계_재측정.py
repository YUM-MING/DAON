"""낙상임계_재측정.py — FALL_MIN_PEAK 을 다시 고른다.

왜 다시 재는가
-----------------------------------------------------------------
FALL_MIN_PEAK = 0.05 을 넣을 때 근거는 이랬다.

    오탐 6건의 최대 peak 0.034  vs  진짜 충격음의 최소 peak 0.072   (2.1배 여유)

그런데 2026-11-15 실사용에서 이 여유가 깨졌다.

    17:10:31   peak 0.093   P(fall) 0.836     ← 보호자 알림까지 나갔다
    15:38:37   peak 0.057   P(fall) 0.755
    15:38:38   peak 0.040   P(fall) 0.603

0.034 이 아니라 0.093 짜리 오탐이 있었다. 표본 6건으로 고른 값이었으니
여유가 실제보다 커 보였던 것이다.

오탐으로 보호자에게 신고가 나가는 것은 시연에서 가장 치명적이다. 놓친
낙상은 한 번 더 확인하면 되지만, 잘못 나간 신고는 되돌릴 수 없다.

어떻게 고르는가
-----------------------------------------------------------------
  오탐 쪽   실제로 낙상이 없었던 녹음. 여기서 P(fall) 이 임계값을 넘는 창은
            전부 오탐이다. 그 창의 peak 를 모은다.
  진짜 쪽   data/realworld/fall/ (라벨 있음). 각 파일에서 낙상으로 잡히는
            창의 peak 중 가장 큰 값을 모은다. 그 값이 FALL_MIN_PEAK 보다
            작으면 그 낙상은 놓친다.

  두 분포를 겹쳐 놓고 FALL_MIN_PEAK 을 훑는다. **재현율을 깎지 않으면서
  오탐을 가장 많이 없애는 값**을 고른다.

  고른 값은 반드시 eval_realworld.py 로 다시 확인한다. 여기서 고르고
  여기서 보고하면 그 숫자는 믿을 수 없다.

쓰는 법 (DAON venv 에서)
-----------------------------------------------------------------
    cd C:\\Users\\hugh\\Desktop\\DAON
    .\\.venv\\Scripts\\Activate.ps1
    python "C:\\Users\\hugh\\Desktop\\audio_ai\\DAON_patch\\낙상임계_재측정.py"

    python ...\\낙상임계_재측정.py --오탐녹음 "경로1,경로2"   다른 녹음으로

API 호출 없음. CPU 로 5~10분. 결과는 `낙상임계_결과.txt` 에도 남는다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUDIO_AI = HERE.parent
DAON = Path(r"C:\Users\hugh\Desktop\DAON")
if not DAON.exists():
    DAON = AUDIO_AI.parent / "DAON"

# 실제로 낙상이 없었던 녹음 — 여기서 나오는 낙상 판정은 전부 오탐이다
오탐녹음 = [
    DAON / "logs" / "home_2",          # 2026-11-15 실사용 (오늘 알림이 나간 그 녹음)
    DAON / "logs" / "home_1",
    AUDIO_AI / "rec" / "stt_quiet_base",
    AUDIO_AI / "rec" / "stt_tv_base_vol8",
    AUDIO_AI / "rec" / "stt_tv_base_vol20",
    AUDIO_AI / "rec" / "stt_far_weak",
]
진짜낙상 = AUDIO_AI / "data" / "realworld" / "fall"


class Tee:
    def __init__(self, path: Path):
        self.f = open(path, "w", encoding="utf-8"); self.out = sys.stdout

    def write(self, s):
        self.out.write(s); self.f.write(s)

    def flush(self):
        self.out.flush(); self.f.flush()


def main() -> None:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--오탐녹음", dest="fp", default="")
    args = ap.parse_args()

    tee = Tee(HERE / "낙상임계_결과.txt")
    sys.stdout = tee

    import numpy as np
    import soundfile as sf
    import torch
    sys.path.insert(0, str(DAON / "modules" / "audio_ai"))
    import inference as INF
    from config import N_SAMPLES, SAMPLE_RATE
    from model import load_model

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model = load_model(INF.CKPT_PATH, dev)
    router = INF.AudioRouter(model, INF.SpeechToText(enabled=False), dev, stt_async=False)
    hop = int(INF.HOP_SEC * SAMPLE_RATE)
    τ = INF.FALL_THRESHOLD

    print("=" * 84)
    print("FALL_MIN_PEAK 다시 고르기")
    print("=" * 84)
    print(f"지금 값 {INF.FALL_MIN_PEAK} · 낙상 임계값 {τ} · 장치 {dev}\n")

    def 창들(p: Path):
        """파일 하나를 창 단위로 돌려 (peak, P(fall)) 를 내놓는다."""
        try:
            if p.stat().st_size < 1000:
                return
            x, _ = sf.read(str(p), dtype="float32")
        except Exception:                                        # noqa: BLE001
            return
        if x.ndim > 1:
            x = x.mean(1)
        for i in range(0, max(len(x) - N_SAMPLES + 1, 1), hop):
            w = x[i:i + N_SAMPLES]
            if len(w) < N_SAMPLES:
                break
            if router._is_silent(w):
                continue
            probs = router._probs(w) if hasattr(router, "_probs") else None
            if probs is None:
                import torch as T
                with T.no_grad():
                    t = T.from_numpy(w).unsqueeze(0).to(dev)
                    probs = T.softmax(model(t), dim=-1)[0].cpu().numpy()
            yield float(np.abs(w).max()), float(probs[INF.CLASSES.index("fall")])

    # ── 오탐 쪽
    폴더 = [Path(s.strip()) for s in args.fp.split(",") if s.strip()] or 오탐녹음
    오탐 = []          # (peak, p, 파일)
    본파일 = 0
    print("■ 오탐 쪽 — 실제로 낙상이 없었던 녹음")
    for d in 폴더:
        if not d.exists():
            print(f"   (없음: {d})")
            continue
        wavs = sorted(d.glob("*.wav"))
        n = 0
        for p in wavs:
            본파일 += 1
            for pk, pf in 창들(p):
                if pf >= τ:
                    오탐.append((pk, pf, p.name)); n += 1
        print(f"   {d.name:<22} wav {len(wavs):>3}개 · 낙상으로 잡힌 창 {n}개")
    print(f"\n   오탐 창 모두 {len(오탐)}개 (파일 {본파일}개를 봤다)")
    if 오탐:
        pk = sorted(p for p, _, _ in 오탐)
        print(f"   peak 분포: 최소 {pk[0]:.3f} · 중앙 {pk[len(pk)//2]:.3f} · 최대 {pk[-1]:.3f}")
        print("   큰 것 10개 (이 값들이 FALL_MIN_PEAK 을 정한다)")
        for p, f, nm in sorted(오탐, reverse=True)[:10]:
            print(f"     peak {p:.3f}  P(fall) {f:.3f}  {nm}")

    # ── 진짜 낙상 쪽
    print(f"\n■ 진짜 낙상 — {진짜낙상}")
    if not 진짜낙상.exists():
        print("   ★ 없습니다. 재현율을 확인할 수 없어 여기서 멈춥니다.")
        return
    진짜 = []          # 파일마다: 낙상으로 잡히는 창 중 가장 큰 peak
    못잡음 = []
    for p in sorted(진짜낙상.glob("*.wav")):
        후보 = [pk for pk, pf in 창들(p) if pf >= τ]
        if 후보:
            진짜.append((max(후보), p.name))
        else:
            못잡음.append(p.name)
    print(f"   낙상 {len(진짜)+len(못잡음)}개 중 {len(진짜)}개가 P(fall) {τ} 를 넘는다"
          + (f" (임계값 자체로 {len(못잡음)}개는 원래 못 잡는다)" if 못잡음 else ""))
    if 진짜:
        v = sorted(p for p, _ in 진짜)
        print(f"   peak 분포: 최소 {v[0]:.3f} · 중앙 {v[len(v)//2]:.3f} · 최대 {v[-1]:.3f}")
        print("   작은 것 8개 (이 값들이 FALL_MIN_PEAK 에 먼저 걸린다)")
        for p, nm in sorted(진짜)[:8]:
            print(f"     peak {p:.3f}  {nm}")

    # ── 훑기
    전체낙상 = len(진짜) + len(못잡음)
    print("\n" + "=" * 84)
    print("FALL_MIN_PEAK 훑기")
    print("=" * 84)
    print(f"  {'값':>6}{'놓친 낙상':>11}{'재현율':>9}{'남는 오탐 창':>13}{'오탐 줄어듦':>12}")
    기준오탐 = sum(1 for p, _, _ in 오탐 if p >= INF.FALL_MIN_PEAK)
    표 = []
    for m in [0.00, 0.03, 0.05, 0.07, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30]:
        잡음 = sum(1 for p, _ in 진짜 if p >= m)
        fp = sum(1 for p, _, _ in 오탐 if p >= m)
        줄어듦 = 기준오탐 - fp
        이것 = "  ← 지금" if abs(m - INF.FALL_MIN_PEAK) < 1e-9 else ""
        print(f"  {m:6.2f}{전체낙상-잡음:>9}개{잡음/max(전체낙상,1)*100:8.1f}%"
              f"{fp:>11}개{줄어듦:>+10}개{이것}")
        표.append((m, 잡음, fp))

    # 재현율을 깎지 않는 선에서 오탐이 가장 적은 값
    최고재현 = max(j for _, j, _ in 표)
    후보 = [(m, j, f) for m, j, f in 표 if j == 최고재현]
    고름 = min(후보, key=lambda r: r[2])
    print(f"\n  → 재현율을 깎지 않으면서 오탐이 가장 적은 값: FALL_MIN_PEAK = {고름[0]:.2f}")
    print(f"     낙상 {고름[1]}/{전체낙상}개 그대로 · 남는 오탐 창 {고름[2]}개"
          f" (지금 {기준오탐}개)")
    if 고름[2] > 0:
        남은 = sorted((p, nm) for p, _, nm in 오탐 if p >= 고름[0])[-5:]
        print("     그래도 남는 오탐 (peak 가 진짜 낙상과 겹치는 구간):")
        for p, nm in reversed(남은):
            print(f"       peak {p:.3f}  {nm}")
        print("     peak 만으로는 여기까지다. 더 줄이려면 모델이나 융합 쪽을 봐야 한다.")
    print("\n  다음: 고른 값으로 eval_realworld.py 를 돌려 확인한다.")
    print(f"     python eval_realworld.py --fall-min-peak {고름[0]:.2f}")
    print("     여기서 고르고 여기서 보고하면 그 숫자는 믿을 수 없다.")

    sys.stdout = tee.out
    print(f"\n저장: {HERE / '낙상임계_결과.txt'}")


if __name__ == "__main__":
    main()
