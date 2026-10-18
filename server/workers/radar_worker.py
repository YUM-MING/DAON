"""레이더 워커: HLK-LD2410B (UART 256000bps) 기본 보고 프레임을 읽어 motion 이벤트를 내보낸다.

    python server/workers/radar_worker.py --port COM3 --baud 256000

LD2410B 기본 모드 데이터 프레임
    F4 F3 F2 F1 | len(2) | 02 | AA | state | mov_dist(2) | mov_energy | st_dist(2) | st_energy | det_dist(2) | 55 00 | F8 F7 F6 F5
    state: 0x00 없음, 0x01 움직임, 0x02 정지(재실), 0x03 움직임+정지

출력: {"type":"radar","motion":"moving|still|none","distance_cm":..}  0.5초마다
      {"type":"device","radar":"ok|error"}
※ 담당자 미정 상태라 통합자가 임시로 작성. 실기기 검증 전.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

HEAD, TAIL = b"\xF4\xF3\xF2\xF1", b"\xF8\xF7\xF6\xF5"
STATE = {0: "none", 1: "moving", 2: "still", 3: "moving"}


def emit(type: str, **data) -> None:
    print(json.dumps({"type": type, "ts": time.time(), **data}, ensure_ascii=False), flush=True)


def parse_frames(buf: bytearray):
    """버퍼에서 완전한 프레임을 꺼내 (state, det_dist) 를 yield. 남은 바이트는 버퍼에 유지."""
    while True:
        i = buf.find(HEAD)
        if i < 0:
            del buf[:-3]
            return
        if len(buf) < i + 6:
            return
        length = int.from_bytes(buf[i + 4:i + 6], "little")
        end = i + 6 + length + 4
        if len(buf) < end:
            return
        frame = bytes(buf[i + 6:i + 6 + length])
        if buf[i + 6 + length:end] != TAIL:
            del buf[:i + 4]
            continue
        del buf[:end]
        if len(frame) >= 11 and frame[0] == 0x02 and frame[1] == 0xAA:
            yield frame[2], int.from_bytes(frame[9:11], "little")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--baud", type=int, default=256000)
    ap.add_argument("--interval", type=float, default=0.5)
    args = ap.parse_args()
    try:
        import serial
        ser = serial.Serial(args.port, args.baud, timeout=0.1)
    except Exception as e:
        emit("device", radar="error", detail=f"{type(e).__name__}: {e}")
        sys.exit(1)
    emit("device", radar="ok", port=args.port)

    buf, last_emit, latest = bytearray(), 0.0, None
    try:
        while True:
            chunk = ser.read(256)
            if chunk:
                buf += chunk
                for state, dist in parse_frames(buf):
                    latest = (STATE.get(state, "none"), dist)
            now = time.time()
            if latest and now - last_emit >= args.interval:
                emit("radar", motion=latest[0], distance_cm=latest[1])
                last_emit = now
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()


if __name__ == "__main__":
    main()
