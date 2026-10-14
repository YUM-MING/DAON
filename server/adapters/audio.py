"""음성 어댑터 (한정연 모듈): workers/audio_worker.py 를 띄우고 이벤트를 받는다.

입력: 폰 브라우저가 보낸 16kHz PCM (source: phone) 또는 PC 마이크 (source: mic)
음성 → 버스: audio_fall, speech, voice_unclear, device(mic)
버스 → 음성: mic_mute (TTS 재생 중 에코 방지 — 워커가 speech/voice_unclear 만 버리고 낙상 감지는 계속한다)
"""
from __future__ import annotations

import base64

from server.adapters.worker import WorkerAdapter
from server.config import MODULES, WORKERS


class AudioAdapter(WorkerAdapter):
    def __init__(self, bus_for, config: dict) -> None:
        m = config["modules"]["audio"]
        args = ["--source", str(m.get("source", "phone")), "--stt-model", str(m["stt_model"])]
        if not m.get("stt", True):
            args.append("--no-stt")
        if m.get("input_device") is not None:
            args += ["--input-device", str(m["input_device"])]
        if m.get("debug", True):
            args.append("--debug")
        if m.get("record", True):
            args += ["--record-dir", config["logs"]["dir"]]
        super().__init__(bus_for, "audio", WORKERS / "audio_worker.py", MODULES / "audio_ai", args)

    def attach_home(self, rt) -> None:
        hid = rt.id
        rt.ctx.bus.subscribe("mic_mute", lambda ev: self.send({"cmd": "mute" if ev.muted else "unmute", "home": hid}))

    def send_pcm(self, home_id: int, pcm16: bytes, ts: float) -> None:
        self.send({"cmd": "pcm", "home": home_id, "ts": ts, "b64": base64.b64encode(pcm16).decode("ascii")})
