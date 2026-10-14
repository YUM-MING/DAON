"""집(home) 런타임: 집 하나 = 버스 + 브로커 + 조정자 + 대화 엔진 + 가짜 센서 주입기.

비전·음성 워커는 서버 전체에 하나씩(공유)이고, 이벤트의 `home` 필드로 어느 집의 버스로 갈지 정해진다.
"""
from __future__ import annotations

import asyncio
import logging
import time

from server.core.broker import Broker
from server.core.context import Context
from server.core.coordinator import Coordinator

log = logging.getLogger("daon.home")


class HomeStore:
    """조정자가 기록할 때 쓰는 얇은 저장 훅 (DB + home_id)."""

    def __init__(self, db, home_id: int) -> None:
        self.db, self.home_id = db, home_id

    def record(self, type: str, data: dict | None = None, ts: float | None = None) -> None:
        try:
            self.db.record_event(self.home_id, type, data, ts)
        except Exception:
            log.exception("record_event 실패")

    def alert_created(self, alert_id: str, ts: float, cause: str, title: str, sensors: dict, fused: dict) -> None:
        self.db.alert_create(self.home_id, alert_id, ts, cause, title, sensors, fused)

    def alert_resolved(self, alert_id: str, ts: float, resolution: str, handled_by: str) -> None:
        self.db.alert_resolve(self.home_id, alert_id, ts, resolution, handled_by)


class HomeRuntime:
    def __init__(self, config: dict, home: dict, db=None) -> None:
        self.home = home
        self.id, self.code, self.name = int(home["id"]), str(home["code"]), home["name"]
        self.ctx = Context(config, self.id, self.code)
        self.broker = Broker(self.ctx)
        self.store = HomeStore(db, self.id) if db is not None else None
        self.coord = Coordinator(self.ctx, self.broker, store=self.store)
        self.dialogue = None
        self.mock = None
        self.started = time.time()

    async def start(self) -> None:
        self.ctx.bus.attach(asyncio.get_running_loop())
        m = self.ctx.config["modules"]
        if m["dialogue"]["mode"] == "real":
            from server.adapters.dialogue import DialogueAdapter
            self.dialogue = DialogueAdapter(self.ctx).start()
        from server.mock.generator import MockSensors
        self.mock = MockSensors(self.ctx).start()      # 주입기는 항상 (개발자 도구), 주기 루프는 mode=mock 일 때만
        log.info("home %s(%s) started", self.name, self.code)

    def tick(self) -> None:
        self.coord.tick()

    async def stop(self) -> None:
        if self.mock:
            self.mock.stop()
        if self.dialogue:
            self.dialogue.stop()


class HomeManager:
    """집 런타임을 필요할 때 만들고(lazy) 보관한다."""

    def __init__(self, config: dict, db, on_create=None) -> None:
        self.config, self.db, self.on_create = config, db, on_create
        self.runtimes: dict[int, HomeRuntime] = {}

    async def get(self, home_id: int) -> HomeRuntime | None:
        rt = self.runtimes.get(home_id)
        if rt:
            return rt
        home = self.db.get_home(home_id)
        if not home:
            return None
        rt = HomeRuntime(self.config, home, self.db)
        await rt.start()
        self.runtimes[home_id] = rt
        if self.on_create:
            self.on_create(rt)
        return rt

    def peek(self, home_id: int) -> HomeRuntime | None:
        return self.runtimes.get(home_id)

    def bus_for(self, home_id: int | None):
        """워커 이벤트 라우팅: home 이 없으면 첫 번째(또는 유일한) 집으로."""
        if home_id is not None and home_id in self.runtimes:
            return self.runtimes[home_id].ctx.bus
        if home_id is None and self.runtimes:
            return next(iter(self.runtimes.values())).ctx.bus
        return None

    def tick_all(self) -> None:
        for rt in list(self.runtimes.values()):
            try:
                rt.tick()
            except Exception:
                log.exception("tick error (%s)", rt.code)

    async def stop_all(self) -> None:
        for rt in list(self.runtimes.values()):
            await rt.stop()
