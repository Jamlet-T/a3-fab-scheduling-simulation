"""独立高频车辆轨迹：float64原值、按时间分块，停车只记录变化。"""

import json
import struct
from pathlib import Path

from fabcosim.transport.fleet import FleetTransport

RECORD = struct.Struct("<dIII5d")


class MotionRecorder:
    def __init__(
        self, output: Path, engine: FleetTransport, interval_s: float = 0.1, chunk_s: float = 600
    ) -> None:
        self.directory = output / "motion"
        step = engine.config["step_s"]
        if interval_s < step - 1e-8 or abs(interval_s / step - round(interval_s / step)) > 1e-7:
            raise ValueError("运动采样间隔必须为仿真步长的正整数倍")
        if chunk_s <= 0:
            raise ValueError("轨迹分块时长必须为正")
        self.directory.mkdir()
        self.interval, self.chunk_s = interval_s, chunk_s
        self.ids = list(engine.ids)
        self.edge_list = [
            {
                "id": e.getID(),
                "length": e.getLength(),
                "shape": e.getLane(0).getShape(),
                "from": e.getFromNode().getID(),
                "to": e.getToNode().getID(),
            }
            for e in engine.net.getEdges()
        ]
        self.edges = {e["id"]: i for i, e in enumerate(self.edge_list)}
        self.states, self.state_index = [], {}
        self.latest, self.signatures, self.last_written = {}, {}, {}
        self.entries, self.stream, self.window = [], None, None
        self.count, self.first_time, self.last_time = 0, 0.0, 0.0

    def _flush(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.entries.append(
                {
                    "window": self.window,
                    "start": self.window * self.chunk_s,
                    "first": self.first_time,
                    "last": self.last_time,
                    "file": f"motion/{self.window:07d}.bin",
                    "count": self.count,
                }
            )
            self.stream = None

    def record(self, time: float, snapshots: list[dict]) -> None:
        for index, snapshot in enumerate(snapshots):
            fields = ("phase", "task_id", "lot_id", "waiting_reason", "zone_wait")
            key = tuple(snapshot.get(k) for k in fields)
            if key not in self.state_index:
                self.state_index[key] = len(self.states)
                self.states.append(dict(zip(fields, key)))
            state_id = self.state_index[key]
            row = (
                time,
                index,
                self.edges[snapshot["edge"]],
                state_id,
                snapshot["x"],
                snapshot["y"],
                snapshot["speed"],
                snapshot["distance_m"],
                snapshot["position_m"],
            )
            signature = row[2:]
            if signature == self.signatures.get(index) and snapshot["speed"] < 1e-8:
                continue
            if time - self.last_written.get(index, -float("inf")) < self.interval - 1e-8:
                if (
                    state_id == self.latest.get(index, (None, None, None, None))[3]
                    and row[2] == self.latest.get(index, (None, None, None))[2]
                    and snapshot["speed"] > 1e-8
                ):
                    continue
            window = int(time // self.chunk_s)
            if window != self.window:
                self._flush()
                self.window = window
                self.stream = (self.directory / f"{window:07d}.bin").open("wb")
                self.count, self.first_time = 0, time
                # 保留入块状态，随机跳转及稀疏停车时段无需读取所有历史块。
                for previous in self.latest.values():
                    self.stream.write(RECORD.pack(*previous))
                    self.count += 1
            self.stream.write(RECORD.pack(*row))
            self.count += 1
            self.last_time = time
            self.latest[index], self.signatures[index] = row, signature
            self.last_written[index] = time

    def close(self) -> None:
        self._flush()
        manifest = {
            "version": 1,
            "record_bytes": RECORD.size,
            "interval_s": self.interval,
            "chunk_s": self.chunk_s,
            "vehicle_ids": self.ids,
            "edges": self.edge_list,
            "states": self.states,
            "chunks": self.entries,
            "fields": [
                "time",
                "vehicle",
                "edge",
                "state",
                "x",
                "y",
                "speed",
                "distance_m",
                "position_m",
            ],
        }
        (self.directory.parent / "motion-index.json").write_text(
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )
