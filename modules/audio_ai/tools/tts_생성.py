"""tts_생성.py — 로봇이 할 말을 미리 음성으로 만들어 둔다.

왜 미리 만드는가
-----------------------------------------------------------------
지금은 폰 브라우저의 내장 음성(speechSynthesis)이 읽는다. 그래서
  · 목소리가 딱딱하다 (폰 OS 기본 음성, 고를 수도 없다)
  · 다 읽었다는 신호(onend)가 안 올 때가 있어 마이크가 8초 넘게 막힌다

응답 문구의 대부분은 responses.py 에 **글자 그대로 박혀 있다.** 그러면 미리
만들어 두면 된다. 그러면 한 번에 셋이 풀린다.

  · 목소리가 자연스러워진다        (OpenAI TTS)
  · 합성 지연이 0 이 된다          (이미 만들어 둔 파일을 재생)
  · 다 읽었다는 신호가 안정적이다   (<audio> 의 onended)

상황마다 말투를 다르게 한다
-----------------------------------------------------------------
"가족 생각에 웃으시니 저도 행복해요" 와 "지금 보호자분께 연락하고 있어요" 를
같은 톤으로 읽으면 어느 한쪽은 반드시 어색하다. 미리 만드는 방식이라
문구마다 다른 지시를 넣을 수 있고, 비용은 같다.

    ESCALATE · REASSURE · REASSURE_CRISIS       →  침착하게
    REASSURE_HAZARD · HAZARD_HOLD
    나머지 전부 (SMALL_TALK, ASK_CHECK, …)       →  따뜻하게

치환자가 든 문구({said} 같은)는 만들지 않는다. 그런 문장은 그때그때 내용이
달라지므로 미리 만들 수 없다. 재생 쪽에서 기존 방식으로 넘어간다.

만들어지는 것
-----------------------------------------------------------------
    DAON/web/tts/<목소리>/0001.mp3 …
    DAON/web/tts/<목소리>/manifest.json     문구 → 파일 대응표

manifest 는 **문구 전체를 키로** 쓴다. 혜원님이 문구를 고치면 그 문구는
대응표에서 빠지므로 자동으로 기존 방식으로 넘어간다. 틀린 음성이 나갈 일이 없다.

쓰는 법 (DAON venv 에서)
-----------------------------------------------------------------
    cd C:\\Users\\hugh\\Desktop\\DAON
    .\\.venv\\Scripts\\Activate.ps1
    python "C:\\Users\\hugh\\Desktop\\audio_ai\\DAON_patch\\tts_생성.py"

    python ...\\tts_생성.py --목소리 nova      다른 목소리로 한 벌 더
    python ...\\tts_생성.py --확인             만들지 않고 목록만 보기
    python ...\\tts_생성.py --개수 10          앞 10개만 (시험 삼아)

중간에 끊겨도 다시 돌리면 **이미 만든 것은 건너뜁니다.**
247개에 20~30분, 비용은 몇 달러 수준입니다. 교수님 키로 호출됩니다.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

MARK = Path("modules") / "dialogue" / "src" / "responses.py"


def find_daon() -> Path:
    """이 파일이 저장소 안에 있든(modules/audio_ai/tools/) 밖에 있든 DAON 을 찾는다."""
    here = Path(__file__).resolve()
    for p in [here.parent, *here.parents]:
        if (p / MARK).exists():
            return p
        if (p / "DAON" / MARK).exists():               # 저장소와 나란히 둔 경우
            return p / "DAON"
    return Path(r"C:\Users\hugh\Desktop\DAON")


DAON = find_daon()
RESPONSES = DAON / MARK
WEB_TTS = DAON / "web" / "tts"

API = "https://api.openai.com/v1/audio/speech"
MODEL = "gpt-4o-mini-tts"

# 말투 — 상황에 따라 다르게 읽는다
TONE_WARM = ("따뜻하게",
             "혼자 사시는 어르신에게 말하듯, 따뜻하고 차분하게. "
             "또박또박 천천히, 다정하지만 과장되지 않게.")
TONE_CALM = ("침착하게",
             "침착하고 또렷하게. 걱정하는 마음은 묻어나되 겁주지 않게. "
             "서두르지 말고, 낮고 안정된 목소리로.")

# 긴급·안심 계열만 침착하게. 나머지는 따뜻하게
URGENT_NAMES = {"ESCALATE", "REASSURE", "REASSURE_CRISIS", "REASSURE_HAZARD", "HAZARD_HOLD"}

# 로봇이 통째로 말하지 않는 것들 — 다른 문장의 {슬롯} 에 끼워 넣는 조각·이름표.
# 확인하는 법: dialogue_algorithm.py 에서 그 상수가 "slots" 안에서만 쓰이면 조각이다.
SKIP_NAMES = {"DANGER_SAID", "TOPIC_NAME", "REPEAT_PREFIX", "CHECKIN_KINDS",
              "SYMPTOM_SAID", "RESCUE_LIKE_ASK",
              "REDIRECT_ACK",        # {ack} 로만 쓰인다
              "REDIRECT_TOPIC"}      # {anchor} 로만 쓰인다

HANGUL = re.compile(r"[가-힣]")


# ───────────────────────── 문구 모으기 ─────────────────────────
def collect() -> list[tuple[str, str, str]]:
    """responses.py 에서 (문구, 출처 상수, 말투이름) 목록을 만든다."""
    tree = ast.parse(RESPONSES.read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}          # 문구 → (출처, 말투)

    def strings(node):
        for n in ast.walk(node):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                yield n.value

    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        name = next((t.id for t in targets if isinstance(t, ast.Name)), None)
        if not name or name in SKIP_NAMES or node.value is None:
            continue
        tone = TONE_CALM[0] if name in URGENT_NAMES else TONE_WARM[0]
        for s in strings(node.value):
            t = s.strip()
            if len(t) < 8 or len(HANGUL.findall(t)) < 4:
                continue                      # 너무 짧거나 한국어가 아닌 것
            if "{" in t or "%" in t:
                continue                      # 치환자가 든 문장은 미리 못 만든다
            if s != s.rstrip():
                continue                      # 뒤에 공백 — 다른 말에 붙이는 조각
            out.setdefault(t, (name, tone))

    return sorted(((t, src, tone) for t, (src, tone) in out.items()),
                  key=lambda x: (x[2] != TONE_CALM[0], x[1], x[0]))


# ───────────────────────── OpenAI ─────────────────────────
def load_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        return key
    env = DAON / "modules" / "dialogue" / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "OPENAI_API_KEY":
                return v.strip().strip('"').strip("'")
    raise SystemExit(f"OPENAI_API_KEY 를 찾지 못했습니다 ({env})")


def speak(key: str, text: str, voice: str, instructions: str) -> bytes:
    body = {"model": MODEL, "voice": voice, "input": text,
            "response_format": "mp3", "instructions": instructions}
    req = urllib.request.Request(
        API, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def explain(e: urllib.error.HTTPError) -> str:
    try:
        msg = json.loads(e.read().decode("utf-8", "replace")).get("error", {}).get("message", "")
    except Exception:                                            # noqa: BLE001
        msg = ""
    hint = {401: "키가 잘못됐거나 만료됐습니다.",
            403: "이 키로는 음성 합성을 쓸 수 없습니다.",
            404: f"모델 이름을 못 찾았습니다 ({MODEL}).",
            429: "호출이 너무 잦거나 할당량이 끝났습니다. 잠시 뒤 다시 돌리면 이어서 합니다."}.get(e.code, "")
    return f"HTTP {e.code}  {hint}  {msg}"[:300]


# ───────────────────────── 본체 ─────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--목소리", dest="voice", default="cedar")
    ap.add_argument("--확인", dest="dry", action="store_true", help="만들지 않고 목록만")
    ap.add_argument("--개수", dest="limit", type=int, default=0, help="앞 N개만")
    args = ap.parse_args()

    if not RESPONSES.exists():
        raise SystemExit(f"responses.py 를 찾지 못했습니다: {RESPONSES}")

    items = collect()
    if args.limit:
        items = items[:args.limit]

    calm = sum(1 for _, _, tone in items if tone == TONE_CALM[0])
    chars = sum(len(t) for t, _, _ in items)

    print("=" * 74)
    print("로봇 음성 미리 만들기")
    print("=" * 74)
    print(f"문구     {len(items)}개 · {chars:,}자")
    print(f"           따뜻하게 {len(items)-calm}개 · 침착하게 {calm}개")
    print(f"목소리   {args.voice}   모델 {MODEL}")
    print(f"저장     {WEB_TTS / args.voice}")
    print()

    outdir = WEB_TTS / args.voice
    outdir.mkdir(parents=True, exist_ok=True)
    mpath = outdir / "manifest.json"

    # 이미 만들어 둔 것을 읽는다.
    # 파일 이름은 문구에 한 번 붙으면 그대로 간다. 순번으로 매기면 문구가 하나
    # 늘어날 때마다 뒤쪽 번호가 전부 밀려서 멀쩡한 음성을 다시 만들게 된다.
    old_map: dict[str, str] = {}
    if mpath.exists():
        try:
            old = json.loads(mpath.read_text(encoding="utf-8"))
            if old.get("voice") == args.voice:
                old_map = dict(old.get("map", {}))
        except Exception:                                        # noqa: BLE001
            pass

    need = {t for t, _, _ in items}
    keep = {t: f for t, f in old_map.items()
            if t in need and (outdir / f).exists() and (outdir / f).stat().st_size > 1000}
    버린문구 = [t for t in old_map if t not in need]              # 문구가 바뀌거나 빠진 것
    todo = [(t, src, tone) for t, src, tone in items if t not in keep]

    # 쓰이는 파일 이름을 피해서 다음 번호를 준다
    used = {int(m.group(1)) for f in keep.values()
            if (m := re.fullmatch(r"(\d+)\.mp3", f))}
    nxt = max(used, default=0)

    print(f"이미 있는 것 {len(keep)}개 · 새로 만들 것 {len(todo)}개"
          + (f" · 안 쓰이게 된 것 {len(버린문구)}개" if 버린문구 else ""))

    if args.dry:
        if 버린문구:
            print("\n■ 안 쓰이게 된 음성 (돌리면 지웁니다)")
            for t in 버린문구:
                print(f"   {old_map[t]}  {t[:58]}")
        print(f"\n■ 새로 만들 문구 {len(todo)}개 · {sum(len(t) for t, _, _ in todo):,}자")
        for t, src, tone in todo:
            print(f"   [{tone}] [{src}] {t[:58]}")
        print("\n(--확인 이라 만들지 않았습니다. 빼면 실제로 만듭니다)")
        return

    if 버린문구:
        for t in 버린문구:
            f = outdir / old_map[t]
            if f.exists():
                try:
                    f.unlink()
                    print(f"   지움 {f.name}  {t[:46]}")
                except OSError as e:
                    print(f"   못 지움 {f.name} ({e}) — 대응표에서만 뺍니다")
    if not todo:
        manifest = {"voice": args.voice, "model": MODEL,
                    "generated": datetime.now().isoformat(timespec="seconds"), "map": keep}
        mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        print("\n새로 만들 것이 없습니다. 대응표만 정리했습니다.")
        return
    print(f"새로 만들 글자 수 {sum(len(t) for t, _, _ in todo):,}자\n")

    key = load_key()
    manifest = {"voice": args.voice, "model": MODEL, "map": dict(keep)}
    tones = {TONE_WARM[0]: TONE_WARM[1], TONE_CALM[0]: TONE_CALM[1]}
    made = failed = 0
    t0 = time.time()

    def save():
        manifest["generated"] = datetime.now().isoformat(timespec="seconds")
        mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    for i, (text, src, tone) in enumerate(todo, 1):
        nxt += 1
        name = f"{nxt:04d}.mp3"
        path = outdir / name
        try:
            path.write_bytes(speak(key, text, args.voice, tones[tone]))
            manifest["map"][text] = name
            made += 1
            left = (time.time() - t0) / made * (len(todo) - i)
            print(f"  [{i}/{len(todo)}] {name} {tone} {path.stat().st_size/1024:5.0f}KB  "
                  f"남은 시간 약 {left/60:.0f}분   {text[:32]}")
        except urllib.error.HTTPError as e:
            print(f"  [{i}/{len(todo)}] ★ 실패 — {explain(e)}")
            failed += 1
            nxt -= 1                                   # 실패한 번호는 다시 쓴다
            if e.code in (401, 403, 404):
                print("\n   더 돌려도 같은 오류입니다. 멈춥니다.")
                break
            time.sleep(3)
        except Exception as e:                                   # noqa: BLE001
            print(f"  [{i}/{len(todo)}] ★ 실패 — {type(e).__name__}: {str(e)[:100]}")
            failed += 1
            nxt -= 1
        if made % 10 == 0:                   # 중간에 끊겨도 이어서 할 수 있게 자주 저장
            save()
        time.sleep(0.2)

    save()
    total = sum(f.stat().st_size for f in outdir.glob("*.mp3"))
    print()
    print(f"새로 만든 것 {made}개 · 그대로 쓴 것 {len(keep)}개"
          + (f" · 실패 {failed}개" if failed else ""))
    print(f"대응표  {mpath.name} · {len(manifest['map'])}개 문구")
    print(f"폴더 크기 {total/1024/1024:.1f}MB")
    if failed:
        print("\n실패한 것은 다시 돌리면 이어서 만듭니다.")


if __name__ == "__main__":
    main()
