"""完整路线回放压缩；原始帧/事件保留，显示位置不插值、不修改。"""

import copy
import hashlib
import json
from pathlib import Path


def write_full_replay(
    config: dict, frames: list[dict], events: list[dict], geometry: dict, output: Path
) -> None:
    vehicle_fields = [
        "x",
        "y",
        "speed",
        "edge",
        "position_m",
        "distance_m",
        "phase",
        "waiting_reason",
        "task_id",
        "lot_id",
        "zone_wait",
    ]
    lot_fields = [
        "location",
        "phase",
        "operation_index",
        "source_step",
        "completed_operations",
        "passed_count",
        "equipment",
        "next_port",
    ]
    definitions = [
        {key: lot[key] for key in ("id", "route", "operation_count")} for lot in frames[0]["lots"]
    ]
    packed_frames = []
    strings, string_index = [], {}

    def intern(value):
        if value is None:
            return None
        if value not in string_index:
            string_index[value] = len(strings)
            strings.append(value)
        return string_index[value]

    vehicle_string_fields, lot_string_fields = [3, 6, 7, 8, 9, 10], [0, 1, 6, 7]
    previous_static = None
    for frame in frames:
        static = all(v["phase"] == "parked" and v["speed"] == 0 for v in frame["vehicles"])
        signature = (
            json.dumps(
                {
                    k: frame[k]
                    for k in (
                        "vehicles",
                        "lots",
                        "ports",
                        "zones",
                        "zone_queues",
                        "completed_lots",
                        "pending_tasks",
                    )
                },
                sort_keys=True,
            )
            if static
            else None
        )
        if signature is not None and signature == previous_static and frame is not frames[-1]:
            continue
        previous_static = signature
        packed_frames.append(
            {
                "time": frame["time"],
                "v": [
                    [
                        intern(v.get(key)) if index in vehicle_string_fields else v.get(key)
                        for index, key in enumerate(vehicle_fields)
                    ]
                    for v in frame["vehicles"]
                ],
                "l": [
                    [
                        intern(lot.get(key)) if index in lot_string_fields else lot.get(key)
                        for index, key in enumerate(lot_fields)
                    ]
                    for lot in frame["lots"]
                ],
                "p": [
                    [intern(name), intern(value["lot_id"]), intern(value["reserved_by"])]
                    for name, value in frame["ports"].items()
                ],
                "z": [[intern(name), intern(vid)] for name, vid in frame["zones"].items()],
                "q": [
                    [intern(name), [intern(vid) for vid in queue]]
                    for name, queue in frame["zone_queues"].items()
                ],
                "cl": frame["completed_lots"],
                "ct": frame["completed_tasks"],
                "pt": frame["pending_tasks"],
            }
        )
    packed_events, shapes, shape_index = [], [], {}
    start = config.get("replay", {}).get("start_s", 0)
    end = frames[-1]["time"]
    display_events = events
    if start > 0:
        previous_routes = {}
        for event in events:
            if event["kind"] == "vehicle.route" and event["time"] < start:
                previous_routes[event["vehicle_id"]] = event
        display_events = sorted(
            list(previous_routes.values()) + [e for e in events if start <= e["time"] <= end],
            key=lambda e: e["time"],
        )
    for event in display_events:
        if event["kind"] == "material.route" or event["kind"].startswith("module."):
            continue
        event = dict(event)
        if event["kind"] == "vehicle.route":
            shape = event.pop("shapes")
            digest = hashlib.sha256(json.dumps(shape).encode()).hexdigest()
            if digest not in shape_index:
                shape_index[digest] = len(shapes)
                shapes.append(shape)
            event["shape_ref"] = shape_index[digest]
        packed_events.append(event)
    display_config = copy.deepcopy(config)
    display_config["network"].pop("summary", None)
    data = {
        "config": display_config,
        "start_time": start,
        "frames": packed_frames,
        "events": packed_events,
        "geometry": geometry,
        "compact": True,
        "vehicle_fields": vehicle_fields,
        "vehicle_ids": [v["id"] for v in frames[0]["vehicles"]],
        "lot_fields": lot_fields,
        "lot_definitions": definitions,
        "route_shapes": shapes,
        "strings": strings,
        "vehicle_string_fields": vehicle_string_fields,
        "lot_string_fields": lot_string_fields,
    }
    template = Path(__file__).with_name("fleet_replay.html").read_text("utf-8")
    if (output / "motion-index.json").exists():
        data["motion_url"] = "motion-index.json"
        data["end_time"] = frames[-1]["time"]
        directory = output / "states"
        directory.mkdir(exist_ok=True)
        chunks, rows, previous, window = [], [], None, None

        def flush_state():
            if rows:
                filename = f"states/{window:07d}.json"
                (output / filename).write_text(
                    json.dumps(rows, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
                )
                chunks.append({"start": window * 600, "file": filename})

        for frame in packed_frames:
            current_window = int(frame["time"] // 600)
            if current_window != window:
                flush_state()
                rows = [previous] if previous is not None else []
                window = current_window
            rows.append(frame)
            previous = frame
        flush_state()
        data["state_chunks"] = chunks
        data["frames"] = packed_frames[:1]
        (output / "motion_player.js").write_text(
            Path(__file__).with_name("motion_player.js").read_text("utf-8"), encoding="utf-8"
        )
        (output / "replay-data.json").write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )
        data = {"external": True, "data_url": "replay-data.json"}
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    (output / "replay.html").write_text(template.replace("__RUN_DATA__", payload), encoding="utf-8")
