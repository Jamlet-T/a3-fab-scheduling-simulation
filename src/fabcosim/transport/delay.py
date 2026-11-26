"""聚合运输档：一趟运输折算成一个到达时刻，不再逐车积分。

**这是显式声明的近似模型，不是精确模型的等价实现。** 保留"任务—车辆分配"这一层，
因此运输派工规则仍可对照；去掉的是一切运动细节：

- 车辆没有中间位置，只在起点或终点；
- 没有停车点、区域申请、闸门、排队与前车——**因此没有拥堵**；
- 没有加减速与原生停车，没有轨迹。

时长取"里程 ÷ 最大车速"，依据是精确档实测：载货段实测时长与里程÷速度的比值是
**1.007**（55,474趟），空驶段同车速折算也吻合。**不需要补拥堵项。**

有效域：车队利用率低、拥堵可忽略时成立。精确档实测阻塞占比0.03%—0.05%、车队大部分
时间在停车，既有场景下误差很小；车队规模或任务密度大幅提高后拥堵重新出现，此档不再成立。
**因此本档不得用于研究运输调度或拥堵，也不产生轨迹（必须显式关闭回放）。**
"""

from collections.abc import Callable
from pathlib import Path

import sumolib

from fabcosim.data.smat_network import build_smat_network
from fabcosim.transport.fleet import build_merge_network
from fabcosim.transport.sumo import route_between

POSITIONLESS = {"edge": "", "position_m": 0.0}
"""聚合档不给车辆中间位置；空edge使同轨间距统计不参与，而不是报出假的0间距。"""


class DelayTransport:
    def __init__(self, config: dict, output: Path, record: Callable[..., None]) -> None:
        if config.get("replay", {}).get("enabled", True):
            raise ValueError("transport.model=delay不产生轨迹，必须显式关闭replay")
        self.config, self.record = config, record
        # 建网同时把端口几何与车辆初始位置回写进config，后续寻路依赖它。
        real = config.get("network", {}).get("type") == "smat2022"
        path = build_smat_network(config, output) if real else build_merge_network(config, output)
        self.net = sumolib.net.readNet(str(path))
        self.net._fabcosim_route_cache_limit = config.get("performance", {}).get(
            "route_cache_size", 4096
        )
        self.speed_m_s = float(config["oht"]["max_speed_m_s"])
        self.ids = sorted(v["id"] for v in config["vehicles"])
        self.time_offset = 0.0
        self.master_time = 0.0
        self.parking: dict[str, dict] = {}
        self.collisions: list = []
        self.teleports: list = []
        self.geometry = {"lanes": [], "ports": {}}
        self._routes: dict[tuple, float] = {}
        self._parked: dict[str, dict] = {}
        self.trips = {
            v["id"]: {
                "location": {"edge": v["edge"], "position_m": v["position_m"]},
                "goal": None,
                "depart_s": 0.0,
                "arrive_s": 0.0,
                "travelled_m": 0.0,
                "leg_m": 0.0,
            }
            for v in config["vehicles"]
        }

    # ---- 主循环直接调用的接口 ----

    def route_distance(self, origin: dict, goal: dict) -> float:
        key = (origin["edge"], origin["position_m"], goal["edge"], goal["position_m"])
        if key not in self._routes:
            self._routes[key] = route_between(self.net, origin, goal)[1]
        return self._routes[key]

    def cached_distance_pairs(self) -> int:
        """资源观测用的已缓存起终点对数，不改变寻路或缓存策略。"""
        return len(self._routes)

    def distance(self, vid: str, goal: dict) -> float:
        """空驶取货里程，从车辆当前所在端口起算。"""
        return self.route_distance(self.trips[vid]["location"], goal)

    def plan(self, vid: str, goal: dict | None) -> None:
        trip = self.trips[vid]
        trip["travelled_m"] += trip["leg_m"]
        trip["leg_m"] = 0.0
        self.parking.pop(vid, None)
        self._parked.pop(vid, None)
        if goal is None:
            trip.update(goal=None, arrive_s=self.master_time)
            return
        distance = self.route_distance(trip["location"], goal)
        trip.update(
            goal=goal,
            depart_s=self.master_time,
            arrive_s=self.master_time + distance / self.speed_m_s,
            leg_m=distance,
        )

    def arrived(self, vid: str, goal: dict) -> bool:
        trip = self.trips[vid]
        if goal is self._parked.get(vid):
            return True
        return trip["goal"] is goal and self.master_time >= trip["arrive_s"] - 1e-9

    def park(self, vid: str) -> dict | None:
        """聚合档没有停车点：就地待命即视为已停稳。"""
        self._parked[vid] = POSITIONLESS
        self.parking[vid] = POSITIONLESS
        return POSITIONLESS

    def position(self, vid: str) -> dict:
        return POSITIONLESS

    def speed(self, vid: str) -> float:
        trip = self.trips[vid]
        if trip["goal"] is None or self.master_time >= trip["arrive_s"] - 1e-9:
            return 0.0
        return self.speed_m_s

    def snapshot(self, vid: str) -> dict:
        trip = self.trips[vid]
        elapsed = self.master_time - trip["depart_s"]
        span = trip["arrive_s"] - trip["depart_s"]
        fraction = 1.0 if span <= 0 else min(1.0, max(0.0, elapsed / span))
        return {
            "id": vid,
            "x": 0.0,
            "y": 0.0,
            "speed": self.speed(vid),
            **POSITIONLESS,
            # 按匀速折算已行驶里程，使统计层看到连续的里程增长而不是一步一跳。
            "distance_m": trip["travelled_m"] + fraction * trip["leg_m"],
            "zone_wait": None,
        }

    def present(self) -> bool:
        return True

    def leader(self, vid: str, distance_m: float) -> tuple[str, float] | None:
        return None

    def stops(self, vid: str) -> str:
        return ""

    def integrator_time(self) -> float:
        return self.master_time

    def zone_owners(self) -> dict[str, str]:
        return {}

    def zone_queues(self) -> dict[str, list[str]]:
        return {}

    def zone_state(self) -> dict[str, dict]:
        return {"owners": {}, "queues": {}}

    def stall_state(self) -> dict:
        return {"native_stops": {}, "leaders": {}, "owners": {}, "plans": {}}

    # ---- 时间推进 ----

    def step(self, now: float) -> None:
        self.master_time = now
        for trip in self.trips.values():
            if trip["goal"] is not None and now >= trip["arrive_s"] - 1e-9:
                trip["travelled_m"] += trip["leg_m"]
                trip["leg_m"] = 0.0
                trip["location"] = trip["goal"]

    def advance_stationary(self, now: float) -> None:
        self.step(now)

    def control(self, now: float) -> None:
        """聚合档没有区域控制。"""

    def next_arrival(self) -> float | None:
        """最早的在途到达时刻；没有在途车辆时返回None。"""
        pending = [
            trip["arrive_s"]
            for trip in self.trips.values()
            if trip["goal"] is not None and trip["arrive_s"] > self.master_time + 1e-9
        ]
        return min(pending) if pending else None

    def close(self) -> None:
        pass
