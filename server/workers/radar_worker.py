"""레이더 워커: HLK-LD2410B (24GHz 재실 감지, UART 256000bps 8N1) → motion 이벤트.

    python server/workers/radar_worker.py --port COM3                 # 서버가 띄우는 방식 (JSON lines 출력)
    python server/workers/radar_worker.py --port COM3 --info          # 모듈 설정(펌웨어·감도·무인 지속 시간)만 읽고 종료
    python server/workers/radar_worker.py --port COM3 --monitor       # 사람이 읽는 한 줄 출력 (설치 위치·감도 점검용)
    python server/workers/radar_worker.py --port COM3 --set-absent-sec 2   # 무인 지속 시간 변경 (모듈에 저장됨)

연결 (CP2102 USB-TTL): 모듈 VCC→5V, GND→GND, 모듈 TX→어댑터 RX, 모듈 RX→어댑터 TX. 신호 레벨 3.3V.
규격: HLK-LD2410B Serial communication protocol v1.06

보고 프레임 (기본 모드, 약 10Hz)
    F4 F3 F2 F1 | len(2) | type | AA | state | mov_dist(2) | mov_energy | st_dist(2) | st_energy | det_dist(2) | .. | 55 00 | F8 F7 F6 F5
    type : 0x02 기본, 0x01 엔지니어링(거리 구간별 에너지가 뒤에 붙음 — 앞부분 배치는 같다)
    state: 0x00 없음, 0x01 움직임, 0x02 정지(재실), 0x03 움직임+정지
    ※ 실기기(2026-11-10)에서 det_dist 칸은 항상 0 → 거리는 mov_dist / st_dist 에서 가져온다.

명령 프레임 (설정 읽기·쓰기)
    FD FC FB FA | len(2) | word(2) | value(N) | 04 03 02 01        ACK 는 word | 0x0100, 값 앞 2바이트가 상태(0 성공)
    '설정 시작'(0x00FF) ~ '설정 끝'(0x00FE) 사이에만 받으며, 그동안 보고 프레임은 멈춘다.

출력 (LABELS.md 3절)
    {"type":"radar","motion":"moving|still|none","distance_cm":..,"mov_energy":..,"st_energy":..,"state":0~3,"raw_motion":..}   0.5초마다
    motion 은 튐을 걸러낸 값(MotionFilter), raw_motion 은 걸러내기 전 값
    {"type":"device","radar":"ok|stale|error", ...}        stale = 포트는 열려 있는데 3초 넘게 프레임이 없음
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from typing import Iterator, NamedTuple, Optional

HEAD, TAIL = b"\xF4\xF3\xF2\xF1", b"\xF8\xF7\xF6\xF5"
CMD_HEAD, CMD_TAIL = b"\xFD\xFC\xFB\xFA", b"\x04\x03\x02\x01"
STATE = {0: "none", 1: "moving", 2: "still", 3: "moving"}
MAX_FRAME = 128                 # 정상 프레임은 13(기본)·35(엔지니어링)바이트. 이보다 긴 길이값은 깨진 것으로 본다
STALE_SEC = 3.0                 # 이 시간 동안 유효 프레임이 없으면 device radar=stale

CMD_ENABLE, CMD_END, CMD_SET_GATES, CMD_READ, CMD_FIRMWARE, CMD_RESOLUTION = 0x00FF, 0x00FE, 0x0060, 0x0061, 0x00A0, 0x00AB


class Frame(NamedTuple):
    state: int          # 0 없음 / 1 움직임 / 2 정지 / 3 움직임+정지
    mov_dist: int       # 움직임 표적 거리(cm)
    mov_energy: int     # 움직임 에너지 0~100
    st_dist: int        # 정지 표적 거리(cm)
    st_energy: int      # 정지 에너지 0~100
    det_dist: int       # 감지 거리(cm) — 펌웨어에 따라 0 으로만 온다

    @property
    def motion(self) -> str:
        return STATE.get(self.state, "none")

    @property
    def distance_cm(self) -> int:
        if self.det_dist:
            return self.det_dist
        if self.state in (1, 3):
            return self.mov_dist or self.st_dist
        if self.state == 2:
            return self.st_dist or self.mov_dist
        return 0


def emit(type: str, **data) -> None:
    print(json.dumps({"type": type, "ts": time.time(), **data}, ensure_ascii=False), flush=True)


def log(msg: str) -> None:
    print(f"[radar] {msg}", file=sys.stderr, flush=True)


# ---------------- 보고 프레임 ----------------
def parse_frames(buf: bytearray) -> Iterator[Frame]:
    """버퍼에서 완전한 보고 프레임을 꺼내 Frame 을 yield. 덜 온 프레임은 버퍼에 남기고, 깨진 것은 버린다."""
    while True:
        i = buf.find(HEAD)
        if i < 0:
            del buf[:-3]                    # 헤더 앞부분이 걸쳐 있을 수 있으니 끝 3바이트는 남긴다
            return
        if len(buf) < i + 6:
            del buf[:i]
            return
        length = int.from_bytes(buf[i + 4:i + 6], "little")
        if length > MAX_FRAME:              # 길이 칸이 깨짐 → 이 헤더는 버리고 다음 헤더를 찾는다
            del buf[:i + 4]
            continue
        end = i + 6 + length + 4
        if len(buf) < end:
            del buf[:i]
            return
        frame = bytes(buf[i + 6:i + 6 + length])
        if buf[i + 6 + length:end] != TAIL:
            del buf[:i + 4]
            continue
        del buf[:end]
        if len(frame) >= 13 and frame[0] in (0x01, 0x02) and frame[1] == 0xAA and frame[-2:] == b"\x55\x00":
            yield Frame(frame[2], int.from_bytes(frame[3:5], "little"), frame[5],
                        int.from_bytes(frame[6:8], "little"), frame[8], int.from_bytes(frame[9:11], "little"))


def summarize(frames: list[Frame]) -> Optional[dict]:
    """보고 주기(0.5초, 약 5프레임) 동안의 프레임을 이벤트 한 건으로 줄인다.

    한 프레임짜리 튐을 막기 위해 다수결: 사람이 잡힌 프레임 중 움직임이 절반 이상이면 moving, 아니면 still.
    사람이 잡힌 프레임이 하나도 없을 때만 none (모듈이 '무인 지속 시간' 동안은 재실로 보고하므로 none 은 이미 늦게 온다).
    """
    if not frames:
        return None
    present = [f for f in frames if f.state != 0]
    if not present:
        last = frames[-1]
        return {"motion": "none", "distance_cm": 0, "mov_energy": last.mov_energy, "st_energy": last.st_energy,
                "state": 0}
    moving = [f for f in present if f.motion == "moving"]
    motion = "moving" if len(moving) * 2 >= len(present) else "still"
    ref = (moving if motion == "moving" else [f for f in present if f.motion == "still"])[-1]
    return {"motion": motion, "distance_cm": ref.distance_cm,
            "mov_energy": max(f.mov_energy for f in present), "st_energy": max(f.st_energy for f in present),
            "state": ref.state}


class MotionFilter:
    """'움직임' 판정을 늦춰서 튐을 막는다: moving 이 hold 번(기본 3번 = 1.5초) 이어져야 moving 으로 내보낸다.

    실측(2026-11-10, 2.6m 침대에 누움): 가만히 누워 있어도 움직임 에너지가 감도 경계(30 안팎)를 오르내려
    0.5~1초짜리 moving 이 섞여 나온다. 대화 모듈은 낙상 뒤 moving 을 '스스로 움직임'(해제 근거)으로 보므로
    이 튐이 실제 낙상의 보호자 알림을 막을 수 있다. 걷기·일어나기는 moving 이 몇 초씩 이어져 그대로 통과한다.
    still·none 으로 바뀌는 쪽은 늦추지 않는다."""

    def __init__(self, hold: int = 3) -> None:
        self.hold, self.run = max(1, hold), 0

    def apply(self, s: dict) -> dict:
        raw = s["motion"]
        self.run = self.run + 1 if raw == "moving" else 0
        if raw == "moving" and self.run < self.hold:
            return {**s, "motion": "still", "raw_motion": raw}
        return {**s, "raw_motion": raw}


# ---------------- 명령 (설정 읽기·쓰기) ----------------
def build_command(word: int, value: bytes = b"") -> bytes:
    data = word.to_bytes(2, "little") + value
    return CMD_HEAD + len(data).to_bytes(2, "little") + data + CMD_TAIL


def parse_ack(buf: bytes, word: int) -> Optional[bytes]:
    """buf 에서 word 에 대한 ACK 를 찾아 상태(2바이트) 뒤의 값을 돌려준다. 없거나 실패 상태면 None."""
    want = (word | 0x0100).to_bytes(2, "little")
    i = 0
    while True:
        i = buf.find(CMD_HEAD, i)
        if i < 0 or len(buf) < i + 6:
            return None
        length = int.from_bytes(buf[i + 4:i + 6], "little")
        end = i + 6 + length
        if length > MAX_FRAME or len(buf) < end + 4 or buf[end:end + 4] != CMD_TAIL:
            i += 4
            continue
        data = buf[i + 6:end]
        if data[:2] == want and len(data) >= 4:
            return bytes(data[4:]) if data[2:4] == b"\x00\x00" else None
        i = end + 4


def command(ser, word: int, value: bytes = b"", timeout: float = 1.0) -> Optional[bytes]:
    ser.write(build_command(word, value))
    buf, deadline = bytearray(), time.time() + timeout
    while time.time() < deadline:
        buf += ser.read(64)
        ack = parse_ack(bytes(buf), word)
        if ack is not None:
            return ack
    return None


def parse_params(data: bytes) -> Optional[dict]:
    """'설정 읽기'(0x0061) 값: AA | 최대 구간 N | 움직임 최대 구간 | 정지 최대 구간 | 움직임 감도[N+1] | 정지 감도[N+1] | 무인 지속 시간(2)"""
    if len(data) < 4 or data[0] != 0xAA:
        return None
    n = data[1] + 1
    if len(data) < 4 + 2 * n + 2:
        return None
    return {"max_gate": data[1], "move_gate": data[2], "still_gate": data[3],
            "move_sensitivity": list(data[4:4 + n]), "still_sensitivity": list(data[4 + n:4 + 2 * n]),
            "absent_delay_sec": int.from_bytes(data[4 + 2 * n:6 + 2 * n], "little")}


def parse_firmware(data: bytes) -> Optional[str]:
    """'펌웨어 읽기'(0x00A0) 값: 종류(2) | 주 버전(2) | 부 버전(4), 예) 00 01 | 02 01 | 16 24 06 22 → V1.02.22062416"""
    if len(data) < 8:
        return None
    return f"V{data[3]:x}.{data[2]:02x}.{int.from_bytes(data[4:8], 'little'):08x}"   # 각 바이트를 16진 그대로 읽는다


def read_config(ser) -> dict:
    """설정 모드로 들어가 펌웨어·감도·무인 지속 시간·거리 해상도를 읽는다. 읽기만 하고 아무것도 바꾸지 않는다."""
    out: dict = {}
    if command(ser, CMD_ENABLE, b"\x01\x00") is None:
        return out
    try:
        fw = command(ser, CMD_FIRMWARE)
        if fw:
            out["firmware"] = parse_firmware(fw)
        p = parse_params(command(ser, CMD_READ) or b"")
        if p:
            out.update(p)
        res = command(ser, CMD_RESOLUTION)
        if res and len(res) >= 2:
            out["gate_cm"] = 20 if int.from_bytes(res[:2], "little") == 1 else 75
    finally:
        command(ser, CMD_END)               # 설정 모드를 닫아야 보고 프레임이 다시 나온다
        ser.reset_input_buffer()
    return out


def set_absent_delay(ser, seconds: int) -> bool:
    """무인 지속 시간(사람이 사라진 뒤 '없음'으로 바뀌기까지의 지연)을 바꾼다. 전원을 꺼도 유지된다.
    0x0060 은 최대 구간도 함께 써야 하므로 현재 값을 읽어 그대로 다시 쓴다."""
    if command(ser, CMD_ENABLE, b"\x01\x00") is None:
        return False
    try:
        p = parse_params(command(ser, CMD_READ) or b"")
        if not p:
            return False
        value = (b"\x00\x00" + p["move_gate"].to_bytes(4, "little") + b"\x01\x00" + p["still_gate"].to_bytes(4, "little")
                 + b"\x02\x00" + int(seconds).to_bytes(4, "little"))
        return command(ser, CMD_SET_GATES, value) is not None
    finally:
        command(ser, CMD_END)
        ser.reset_input_buffer()


# ---------------- 실행 ----------------
def _watch_stdin(stop: threading.Event) -> None:
    """서버가 보내는 {"cmd":"stop"} (또는 stdin 닫힘) 을 받으면 바로 끝낸다."""
    try:
        for line in sys.stdin:
            if '"stop"' in line:
                break
    except Exception:
        pass
    stop.set()


def run(ser, args) -> None:
    stop = threading.Event()
    if not args.monitor:
        threading.Thread(target=_watch_stdin, args=(stop,), daemon=True).start()
    buf, window = bytearray(), []
    filt = MotionFilter(args.moving_hold)
    now = time.time()
    last_emit, last_frame, stale = now, now, False
    while not stop.is_set():
        chunk = ser.read(256)
        now = time.time()
        if chunk:
            buf += chunk
            got = list(parse_frames(buf))
            if got:
                window += got
                last_frame = now
                if stale:
                    stale = False
                    emit("device", radar="ok", port=args.port)
        if not stale and now - last_frame > STALE_SEC:
            stale = True
            emit("device", radar="stale", detail=f"{STALE_SEC:.0f}초 넘게 프레임 없음 (전원·TX/RX 배선·보드레이트 확인)")
        if now - last_emit >= args.interval:
            s = summarize(window)
            window, last_emit = [], now
            if s is None:
                continue
            s = filt.apply(s)
            if args.monitor:
                held = "  (튐 걸러냄)" if s["raw_motion"] != s["motion"] else ""
                print(f"{time.strftime('%H:%M:%S')}  {s['motion']:<6}  {s['distance_cm']:>4}cm  "
                      f"움직임 {s['mov_energy']:>3}  정지 {s['st_energy']:>3}{held}", flush=True)
            else:
                emit("radar", **s)


def main() -> None:
    ap = argparse.ArgumentParser(description="HLK-LD2410B 레이더 워커")
    ap.add_argument("--port", required=True)
    ap.add_argument("--baud", type=int, default=256000)
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--moving-hold", type=int, default=3, help="moving 이 이 횟수만큼 이어져야 moving 으로 보고 (1 = 걸러내지 않음)")
    ap.add_argument("--info", action="store_true", help="모듈 설정만 읽어 출력하고 종료")
    ap.add_argument("--monitor", action="store_true", help="사람이 읽는 한 줄 출력")
    ap.add_argument("--set-absent-sec", type=int, default=None, help="무인 지속 시간(초)을 모듈에 저장")
    args = ap.parse_args()
    try:
        import serial
        ser = serial.Serial(args.port, args.baud, timeout=0.1)
    except Exception as e:
        emit("device", radar="error", detail=f"{type(e).__name__}: {e}")
        sys.exit(1)
    try:
        if args.set_absent_sec is not None:
            ok = set_absent_delay(ser, args.set_absent_sec)
            log(f"무인 지속 시간 {args.set_absent_sec}초 설정 {'완료' if ok else '실패'}")
        cfg = read_config(ser)
        if args.info:
            print(json.dumps(cfg, ensure_ascii=False, indent=2))
            sys.exit(0 if cfg else 1)
        log(f"{args.port} 연결, 설정 {cfg if cfg else '읽기 실패 (보고 프레임만 사용)'}")
        if not args.monitor:
            emit("device", radar="ok", port=args.port, **cfg)
        run(ser, args)
    except KeyboardInterrupt:
        pass
    except Exception as e:                  # USB 가 뽑히면 SerialException → 서버가 3초 뒤 다시 띄운다
        emit("device", radar="error", detail=f"{type(e).__name__}: {e}")
        sys.exit(1)
    finally:
        try:
            ser.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
