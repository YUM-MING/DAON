"""음성 어댑터 (한정연 모듈): workers/audio_worker.py 를 띄우고 이벤트를 받는다.

음성 → 버스: audio_fall, speech, voice_unclear, device(mic)
버스 → 음성: mic_mute (TTS 재생 중 에코 방지 — 워커가 speech/voice_unclear 만 버리고 낙상 감지는 계속한다)
"""
from __future__ import annotations

from server.adapters.worker import WorkerAdapter
from server.config import MODULES, WORKERS


class AudioAdapter(WorkerAdapter):
    def __init__(self, ctx) -> None:
        m = ctx.config["modules"]["audio"]
        args = ["--stt-model", str(m["stt_model"])]
        if not m.get("stt", True):
            args.append("--no-stt")
        if m.get("input_device") is not None:
            args += ["--input-device", str(m["input_device"])]
        super().__init__(ctx, "audio", WORKERS / "audio_worker.py", MODULES / "audio_ai", args)
        ctx.bus.subscribe("mic_mute", lambda ev: self.send({"cmd": "mute" if ev.muted else "unmute"}))
