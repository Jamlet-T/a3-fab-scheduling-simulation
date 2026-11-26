"""只读导入已冻结路线的连续片段；当前支持按lot、固定均值的串行加工。"""

import csv
import hashlib
from pathlib import Path


def load_route_segments(config: dict, root: Path) -> None:
    """将源工序和秒制加工时间写入运行配置，保留源行与未复现的级联参数。"""
    for route in config["routes"].values():
        source = root / route["source_csv"]
        with source.open(encoding="utf-8-sig", newline="") as stream:
            records = {
                int(row["STEP"]): row
                for row in csv.DictReader(stream)
                if row["ROUTE"] == route["source_route"]
            }
        steps = [op["source_step"] for op in route["operations"]]
        if len(steps) < 2:
            raise ValueError("生产—运输片段至少需要两道工序")
        if steps != list(range(steps[0], steps[-1] + 1)):
            raise ValueError("真实路线片段必须连续，不能跳过中间工序")
        if route["processing_policy"] != "nominal_mean_serial":
            raise ValueError("当前路线仅支持源均值、串行加工")
        route["source_csv_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        for op in route["operations"]:
            row = records[op["source_step"]]
            if (
                row["PROCESSING UNIT"] != "Lot"
                or row["_is_virtual_stage"] != "False"
                or row["SETUP"]
                or row["PROCESSING PROBABILITY in % (Sampling)"] not in ("", "100")
                or row["REWORK PROBABILITY in %"] not in ("", "0")
                or row["STEP FOR CRITICAL QUEUE TIME"]
                or row["STEP FOR LTL DEDICATION"]
            ):
                raise ValueError(f"当前片段不支持工序{op['source_step']}的生产语义")
            if row["PT UNITS"] != "min" or row["PROCESSINGTIME DISTRIBUTION"] != "uniform":
                raise ValueError("当前片段仅支持分钟制uniform源均值")
            op.update(
                processing_s=float(row["MEAN"]) * 60,
                tool_group=row["_mapped_toolgroup"],
                description=row["STEP DESCRIPTION"],
                source_sheet=row["_source_sheet"],
                source_row=int(row["_source_row"]),
                source_record=row,
            )
