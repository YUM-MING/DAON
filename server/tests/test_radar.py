"""레이더(HLK-LD2410B) 워커 파서 + 조정자 연동 + 융합 시나리오 테스트 — 실기기 없이 돈다.

프레임 바이트는 규격서(v1.06) 예시와 2026-11-10 실기기(COM3, 펌웨어 V2.44) 수신값이다.
"""
from __future__ import annotations

from server.tests.test_scenarios import Rig
from server.workers import radar_worker as rw

REAL = bytes.fromhex("f4f3f2f10d0002aa033600641e0064000055 00f8f7f6f5".replace(" ", ""))     # 실기기: 움직임+정지
DOC = bytes.fromhex("f4f3f2f10d0002aa0251000000003b000055 00f8f7f6f5".replace(" ", ""))      # 규격서 예시: 정지
ENG = bytes.fromhex("f4f3f2f1230001aa031e003c00003900000808 3c22050303040306050000391013060608046001"
                    "5500f8f7f6f5".replace(" ", ""))                                         # 규격서 예시: 엔지니어링 모드


def frame(state: int, mov=(0, 0), st=(0, 0)) -> bytes:
    body = bytes([0x02, 0xAA, state]) + mov[0].to_bytes(2, "little") + bytes([mov[1]]) \
        + st[0].to_bytes(2, "little") + bytes([st[1]]) + b"\x00\x00\x55\x00"
    return rw.HEAD + len(body).to_bytes(2, "little") + body + rw.TAIL


# ---------------- 보고 프레임 ----------------
def test_parse_real_frame_uses_target_distance_when_detection_distance_is_zero():
    (f,) = rw.parse_frames(bytearray(REAL))
    assert (f.state, f.mov_dist, f.mov_energy, f.st_dist, f.st_energy, f.det_dist) == (3, 54, 100, 30, 100, 0)
    assert f.motion == "moving" and f.distance_cm == 54          # 감지 거리 칸이 0 → 움직임 거리 사용
    (s,) = rw.parse_frames(bytearray(frame(2, st=(120, 40))))
    assert s.motion == "still" and s.distance_cm == 120
    (n,) = rw.parse_frames(bytearray(frame(0)))
    assert n.motion == "none" and n.distance_cm == 0


def test_parse_doc_examples_normal_and_engineering():
    (d,) = rw.parse_frames(bytearray(DOC))
    assert d.motion == "still" and d.st_energy == 0x3B
    (e,) = rw.parse_frames(bytearray(ENG))
    assert e.motion == "moving" and e.mov_dist == 30 and e.st_dist == 0


def test_parse_survives_garbage_split_and_broken_frames():
    buf = bytearray(b"\x00\x11\xF4\xF3" + REAL[:9])               # 쓰레기 + 가짜 헤더 조각 + 덜 온 프레임
    assert list(rw.parse_frames(buf)) == []
    buf += REAL[9:] + DOC[:-1] + b"\x00" + rw.HEAD + b"\xFF\xFF" + REAL   # 나머지 + 꼬리 깨짐 + 길이 깨짐 + 정상
    got = list(rw.parse_frames(buf))
    assert [f.motion for f in got] == ["moving", "moving"] and len(buf) <= 3


def test_summarize_majority_and_none():
    mv, st, no = rw.Frame(1, 80, 60, 0, 0, 0), rw.Frame(2, 0, 0, 150, 30, 0), rw.Frame(0, 0, 0, 0, 0, 0)
    assert rw.summarize([]) is None
    assert rw.summarize([st, st, st, mv, st])["motion"] == "still"        # 한 프레임 튐은 무시
    s = rw.summarize([mv, mv, st, mv, st])
    assert s == {"motion": "moving", "distance_cm": 80, "mov_energy": 60, "st_energy": 30, "state": 1}
    assert rw.summarize([st, no, no])["motion"] == "still"                # 한 번이라도 잡혔으면 사람 있음
    assert rw.summarize([no, no])["motion"] == "none"


def test_motion_filter_drops_flicker_measured_while_lying_on_bed():
    """2026-11-10 실측: 2.6m 침대에 누워 있는 동안(09:19:25~33)과 일어나 걸어 나올 때(09:19:33~)의 0.5초 단위 값."""
    lying = "S S M M S M S S M S S S S S S".split()
    getting_up = "M M M M M M".split()
    f = rw.MotionFilter(hold=3)
    out = [f.apply({"motion": {"S": "still", "M": "moving"}[x]})["motion"] for x in lying + getting_up]
    assert out[:len(lying)] == ["still"] * len(lying), "누워 있는 동안의 짧은 moving 은 걸러낸다"
    assert out[len(lying):] == ["still", "still", "moving", "moving", "moving", "moving"]   # 1.5초 이어지면 통과
    assert f.apply({"motion": "none"}) == {"motion": "none", "raw_motion": "none"}          # 없음은 늦추지 않는다
    assert rw.MotionFilter(hold=1).apply({"motion": "moving"})["motion"] == "moving"


# ---------------- 명령 (설정 읽기) ----------------
def test_command_and_ack_match_spec():
    assert rw.build_command(rw.CMD_ENABLE, b"\x01\x00").hex() == "fdfcfbfa0400ff00010004030201"
    assert rw.build_command(rw.CMD_READ).hex() == "fdfcfbfa0200610004030201"
    ack = bytes.fromhex("fdfcfbfa0c00a00100000001020116240622 04030201".replace(" ", ""))
    assert rw.parse_firmware(rw.parse_ack(REAL + ack, rw.CMD_FIRMWARE)) == "V1.02.22062416"
    assert rw.parse_ack(ack, rw.CMD_READ) is None                         # 다른 명령의 ACK
    fail = bytes.fromhex("fdfcfbfa0400610101000403 0201".replace(" ", ""))
    assert rw.parse_ack(fail, rw.CMD_READ) is None                        # 상태 1 = 실패


def test_parse_params_spec_example():
    ack = bytes.fromhex("fdfcfbfa1c0061010000aa080808" + "14" * 9 + "19" * 9 + "050004030201")
    p = rw.parse_params(rw.parse_ack(ack, rw.CMD_READ))
    assert p == {"max_gate": 8, "move_gate": 8, "still_gate": 8, "move_sensitivity": [20] * 9,
                 "still_sensitivity": [25] * 9, "absent_delay_sec": 5}
    assert rw.parse_params(b"\xAA\x08") is None


# ---------------- 조정자: 기기 상태 ----------------
def test_radar_goes_stale_then_recovers():
    rig = Rig()
    rig.coord.device["radar"] = "starting"                                # worker 모드로 뜬 직후
    rig.bus.publish("radar", ts=rig.clock(), motion="still", distance_cm=120)
    assert rig.coord.device["radar"] == "ok" and rig.coord.snapshot()["radar"]["distance_cm"] == 120
    rig.clock.t += 6
    rig.coord.tick()
    assert rig.coord.device["radar"] == "stale" and rig.coord.radar_motion is None      # 오래된 '정지'는 버린다
    assert rig.guardian("device_status")[-1]["radar"] == "stale"
    rig.bus.publish("radar", ts=rig.clock(), motion="moving", distance_cm=90)
    assert rig.coord.device["radar"] == "ok" and rig.coord.radar_motion == "moving"


def test_radar_worker_error_drops_last_value():
    rig = Rig()
    rig.bus.publish("radar", ts=rig.clock(), motion="still", distance_cm=120)
    rig.bus.publish("device", ts=rig.clock(), radar="error", detail="SerialException")   # USB 뽑힘
    assert rig.coord.device["radar"] == "error" and rig.coord.radar_motion is None
    assert rig.coord._sensor_labels(rig.clock())["radar"] == "unknown"


# ---------------- 융합: 레이더가 안전 점수에 들어가는 경로 ----------------
def _run(rig: Rig, seconds: int, events) -> None:
    for i in range(seconds):
        rig.clock.t += 1
        for type, data in events(i):
            rig.bus.publish(type, ts=rig.clock(), **data)
        rig.step()
        rig.finish_tts()


def test_offscreen_fall_sound_plus_radar_still_alerts_guardian():
    """카메라 밖에서 넘어짐: 포즈는 없고 낙상음 + 레이더 '정지' → 확인 후 무응답이면 보호자 알림.
    레이더가 없으면 낙상음 단독(34점 - 5)이라 확인조차 시작되지 않는다."""
    def events(radar: bool):
        def at(i):
            ev = [("person_status", {"present": i < 3, "posture": "sitting" if i < 3 else "unknown"})]
            if i < 3:
                ev.append(("pose_pred", {"pred": "no_fall", "conf": 0.9}))
            if radar:
                ev.append(("radar", {"motion": "moving" if i < 3 else "still", "distance_cm": 180}))
            if i == 3:
                ev.append(("audio_fall", {"confidence": 0.88}))
            return ev
        return at

    rig = Rig()
    _run(rig, 30, events(radar=True))
    created = rig.guardian("alert_created")
    assert created, "낙상음 + 레이더 정지 + 무응답 → 보호자 알림"
    assert created[0]["sensors"] == {"pose": "no_fall", "audio": "fall", "radar": "no_motion"}
    assert created[0]["ts"] - 1_760_000_004 <= 20

    no_radar = Rig()
    _run(no_radar, 30, events(radar=False))
    assert not no_radar.guardian("alert_created")


def test_radar_moving_after_pose_fall_does_not_alert():
    """카메라가 낙상으로 봤지만 레이더가 계속 움직임을 본다 → 확인은 하되 보호자 알림까지 가지 않는다."""
    rig = Rig()
    _run(rig, 30, lambda i: [("person_status", {"present": True, "posture": "sitting" if i < 3 else "lying"}),
                             ("pose_pred", {"pred": "no_fall" if i < 3 else "fall", "conf": 0.9}),
                             ("radar", {"motion": "moving", "distance_cm": 150})])
    assert any(m.get("state") == "alert" for m in rig.broker.robot), "괜찮으세요? 확인은 한다"
    assert not rig.guardian("alert_created")


def test_vision_motion_ignored_while_radar_is_fresh():
    rig = Rig()
    seen = []
    feed = rig.dialogue.runner.feed
    rig.dialogue.runner.feed = lambda batch: (seen.append(batch.get("motion")), feed(batch))[1]
    rig.bus.publish("radar", ts=rig.clock(), motion="still", distance_cm=150)
    rig.bus.publish("motion", ts=rig.clock() + 1, motion="moving", source="vision")      # 레이더 5초 안 → 버림
    rig.step()
    rig.bus.publish("motion", ts=rig.clock() + 6, motion="moving", source="vision")      # 레이더 끊긴 뒤 → 비전 사용
    rig.step()
    assert [m for m in seen if m] == ["still", "moving"]
