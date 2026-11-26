"""完整路线与真实设施编译；原始文件和冻结基准只读。"""

import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from fabcosim.data.workbook import SMAT_SHEETS, read_tables


def table(path: Path, sheet: str, count: int | None = None) -> list[dict]:
    return read_tables(path, {sheet: count})[sheet]


def compile_full_routes(config: dict, root: Path) -> dict:
    manifest = json.loads((root / "03-数据/基准-v1/manifest.json").read_text("utf-8"))
    production = next(s for s in manifest["sources"] if "SMT_2020_Model_Data" in s["path"])
    source = root / production["path"]
    cache = config.get("performance", {}).get("workbook_cache", True)
    production_tables = read_tables(source, {"Toolgroups": 106, "Setups": 13}, cache)
    groups = {r["TOOLGROUP"]: r for r in production_tables["Toolgroups"]}
    groups["DefMet_FE_118"] = groups["DefMEt_FE_118"]
    config["toolgroups"] = groups
    config["setups"] = production_tables["Setups"]
    route_csv = root / "03-数据/基准-v1/route_operations.csv"
    with route_csv.open(encoding="utf-8-sig", newline="") as stream:
        all_rows = list(csv.DictReader(stream))
    config["source_catalog"] = {
        "baseline": manifest["baseline_id"],
        "production": production,
        "route_csv_sha256": hashlib.sha256(route_csv.read_bytes()).hexdigest(),
        "routes": dict(Counter(r["ROUTE"] for r in all_rows)),
    }
    config["routes"] = {}
    for name in config["production"]["routes"]:
        rows = [r for r in all_rows if r["ROUTE"] == name]
        if not rows or [int(r["STEP"]) for r in rows] != list(range(1, len(rows) + 1)):
            raise ValueError(f"{name}不是从首道到末道的完整连续路线")
        operations = [
            {
                "source_step": int(r["STEP"]),
                "tool_group": r["_mapped_toolgroup"],
                "description": r["STEP DESCRIPTION"],
                "source_sheet": r["_source_sheet"],
                "source_row": int(r["_source_row"]),
                "source_record": r,
                "processing_s": float(r["MEAN"]) * 60,
                "offset_s": float(r["OFFSET"] or 0) * 60,
                "unit": r["PROCESSING UNIT"],
                "virtual": r["_is_virtual_stage"] == "True",
                "interval_s": float(r["CASCADING INTERVAL"] or 0) * 60,
                "sample_probability": float(r["PROCESSING PROBABILITY in % (Sampling)"] or 100) / 100,
                "rework_probability": float(r["REWORK PROBABILITY in %"] or 0) / 100,
                "rework_step": int(r["STEP FOR REWORK"] or 0),
                "dedication_step": int(r["STEP FOR LTL DEDICATION"] or 0),
                "cqt_step": int(r["STEP FOR CRITICAL QUEUE TIME"] or 0),
                "cqt_s": float(r["CQT"] or 0) * 3600,
                "batch_min": int(r["BATCH MINIMUM"] or 0),
                "batch_max": int(r["BATCH MAXIMUM"] or 0),
                "setup": r["SETUP"],
                "setup_when": r["WHEN"],
                "setup_s": float(r["SETUP TIME"]) * 60 if r["SETUP TIME"] else None,
            }
            for r in rows
        ]
        # 剩余名义加工时间：从当前步到末步的源均值合计，仅用于交期紧迫度比较。
        suffix = [0.0] * (len(operations) + 1)
        for index in range(len(operations) - 1, -1, -1):
            suffix[index] = suffix[index + 1] + operations[index]["processing_s"]
        config["routes"][name] = {
            "source_route": name,
            "operations": operations,
            "rpt_s": suffix[0],
            "remaining_s": {
                op["source_step"]: suffix[index] for index, op in enumerate(operations)
            },
        }
    amhs = root / config["network"]["workbook"]
    # 纯生产只复用设备组与机台数，不读取轨道/车辆数据或构建运输路网。
    sheets = (
        {name: SMAT_SHEETS[name] for name in ("Equipment", "Port")}
        if config.get("transport", {}).get("model") == "production_only" else SMAT_SHEETS
    )
    spatial_tables = read_tables(amhs, sheets, cache)
    equipment = spatial_tables["Equipment"]
    source_ports = {r["NAME"]: r for r in spatial_tables["Port"]}
    used_groups = {
        op["tool_group"] for route in config["routes"].values() for op in route["operations"]
    }
    assets, candidates = {}, defaultdict(list)
    config["ports"] = {}
    for eq in equipment:
        group = eq["TOOL_GROUP"]
        if group not in used_groups and group not in ("STOCKER", "COMMIT", "COMPLETE"):
            continue
        ports = [p for p in source_ports.values() if p["EQP_NAME"] == eq["NAME"]]
        ports.sort(key=lambda p: p["NAME"])
        if not ports:
            continue
        kind = (
            "buffer"
            if group == "STOCKER"
            else group.lower()
            if group in ("COMMIT", "COMPLETE")
            else "tool"
        )
        input_name, output_name = eq["NAME"] + ".in", eq["NAME"] + ".out"
        config["ports"][input_name] = {"source_id": ports[0]["NAME"], "facility_kind": kind}
        config["ports"][output_name] = {"source_id": ports[-1]["NAME"], "facility_kind": kind}
        assets[eq["NAME"]] = {
            "id": eq["NAME"],
            "tool_group": group,
            "kind": kind,
            "input_port": input_name,
            "output_port": output_name,
        }
        candidates[group].append(eq["NAME"])
    candidates["Delay_32"] = candidates["STOCKER"]
    for group in used_groups:
        if not candidates[group]:
            raise ValueError(f"设备组{group}无真实设施")
    for route in config["routes"].values():
        for op in route["operations"]:
            asset = assets[sorted(candidates[op["tool_group"]])[0]]
            op.update(input_port=asset["input_port"], output_port=asset["output_port"])
    config["assets"], config["candidates"] = assets, dict(candidates)
    return config
