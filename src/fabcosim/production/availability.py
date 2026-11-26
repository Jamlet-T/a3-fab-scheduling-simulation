"""机台可用性调度：按源表参数产生维护与非计划停机。

**声明规则**（源表只给参数、不给停机与在制批次的交互细则，这里固定一种并记录）：

1. 停机在**机台空闲后、下一批开工前**执行，**不打断在制批次**——运行中的批次照常做完。
2. 故障与日历制PM由独立进程按日历申请机台闸门，空闲机台到期即执行；若有在制批，
   等其释放闸门后执行。计数制PM在累计片数达到阈值后的下一次取得机台准入时执行。
3. 故障（Breakdown）按**机台独立抽样**，参数取该机台所属区域的分布。
   `DOWN EVENT VALID FOR TYPE=area`表示参数按区域共用，**不表示整个区域同时停机**。
4. 各机台的随机流由`seed + 机台名`派生，可复现，且不占用生产/抽样/返工的随机流。

PM的时长分布是uniform(MEAN±OFFSET)，故障的TTF/TTR是exponential。
"""

import hashlib
import random


def machine_rng(seed: int, name: str) -> random.Random:
    """每台机台独立的随机流；与lot的生产/抽样/返工流分开。"""
    digest = hashlib.sha256(f"{seed}:availability:{name}".encode()).digest()
    return random.Random(digest)


class MachineAvailability:
    """一台机台的PM与非计划停机；日历事件与开工前计数检查分别推进。"""

    def __init__(self, name: str, params: dict | None, seed: int) -> None:
        self.name = name
        self.rng = machine_rng(seed, name)
        self.pm = list((params or {}).get("pm") or [])
        self.breakdown = (params or {}).get("breakdown")
        # 计数制PM：记上次执行时的累计片数。首次到期在`first`片，故初始计数回退一个周期。
        self.pm_last_run = [
            entry["first"] - entry["period"] if entry["counter"] else 0 for entry in self.pm
        ]
        # 日历制停机（故障与日历制PM）统一由独立进程按日历推进，与机台是否在用无关。
        self._entries: list[dict] = []
        for entry in self.pm:
            if entry["counter"]:
                continue
            self._entries.append(
                {
                    "kind": "pm",
                    "event": entry["event"],
                    "next": entry["first"],
                    "period": entry["period"],
                    "distribution": entry["distribution"],
                    "mean": entry["mean"],
                    "offset": entry["offset"],
                }
            )
        if self.breakdown is not None:
            self._entries.append(
                {
                    "kind": "breakdown",
                    "event": self.breakdown["event"],
                    # 首次时刻的分布是指数，FOA是它的均值，因此首次也是抽样而非定值。
                    "next": self.rng.expovariate(1.0 / self.breakdown["first"]),
                    "ttf": self.breakdown["ttf"],
                    "ttr": self.breakdown["ttr"],
                }
            )
        self.down_at = self._entries[-1]["next"] if self.breakdown is not None else None

    def _sample(self, distribution: str, mean: float, offset: float) -> float:
        if distribution == "uniform":
            return self.rng.uniform(mean - offset, mean + offset)
        return self.rng.expovariate(1.0 / mean)

    def has_calendar_events(self) -> bool:
        """是否有按日历推进的停机（故障，或日历制PM）。"""
        return bool(self._entries)

    def next_calendar_at(self) -> float | None:
        """下一个日历制停机的到期时刻；没有则None。"""
        times = [e["next"] for e in self._entries if e["next"] is not None]
        return min(times) if times else None

    def take_calendar(self, now: float) -> tuple[str, float]:
        """执行最逾期的那个日历制停机并推进它；调用前须确认`next_calendar_at`已到期。"""
        overdue = [e for e in self._entries if e["next"] is not None and now >= e["next"] - 1e-9]
        entry = max(overdue, key=lambda e: now - e["next"])
        if entry["kind"] == "breakdown":
            duration = self._sample("exponential", entry["ttr"], 0.0)
            # 修复结束才开始重新计TTF。
            entry["next"] = now + duration + self.rng.expovariate(1.0 / entry["ttf"])
        else:
            duration = self._sample(entry["distribution"], entry["mean"], entry["offset"])
            entry["next"] = now + entry["period"]
        return entry["event"], duration

    def counter_due(self, wafer_count: int) -> tuple[str, float] | None:
        """计数制PM：累计片数达到阈值就到期。由机台在开工前询问。

        多项同时到期时取最逾期的一个，避免周期短的一项永远抢先。
        """
        overdue = [
            (wafer_count - self.pm_last_run[i] - entry["period"], i)
            for i, entry in enumerate(self.pm)
            if entry["counter"] and wafer_count - self.pm_last_run[i] >= entry["period"]
        ]
        if not overdue:
            return None
        _, chosen = max(overdue)
        entry = self.pm[chosen]
        self.pm_last_run[chosen] = wafer_count
        return entry["event"], self._sample(entry["distribution"], entry["mean"], entry["offset"])
