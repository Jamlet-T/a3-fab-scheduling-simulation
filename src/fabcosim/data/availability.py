"""机台可用性参数：从源工作簿的`PM`与`Breakdown`表读取维护与非计划停机。

源表语义（照抄，不作推断）：

- `PM`表按`toolgroup`定义，一个设备组可有多条（如`_MN`与`_QT`两种维护）。
  `PM TYPE`为`time based (calendar)`时`MTBeforePM`是日历周期（day）；
  为`counter based`时是累计加工片数（pieces）。时长分布`TTR`为uniform(MEAN±OFFSET)。
- `Breakdown`表按**区域**定义，`TTF`与`TTR`均为exponential。
  `DOWN EVENT VALID FOR TYPE=area`表示参数按区域共用，**不表示整个区域同时停机**；
  本实现按机台独立抽样，规则见开发记录。

未列出的设备组没有PM；区域未出现在Breakdown表中的机台没有非计划停机。
"""

from pathlib import Path

from fabcosim.data.workbook import read_tables

UNIT_SECONDS = {
    "min": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "hr": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "day": 86400.0,
    "days": 86400.0,
}


def _seconds(value, unit: str, where: str) -> float:
    if unit not in UNIT_SECONDS:
        raise ValueError(f"{where}的单位未知：{unit!r}")
    return float(value) * UNIT_SECONDS[unit]


def _duration(row: dict, columns: dict, where: str) -> tuple:
    """读一条时长分布：uniform返回(类型, 均值, 偏移)，exponential返回(类型, 均值, 0)。

    `columns`给出分布、均值、偏移、单位四列的名字——表里单位列是`TTR UNITS`，
    与均值列不同名。
    """
    distribution = (row[columns["distribution"]] or "").strip().lower()
    unit = row[columns["unit"]]
    mean = _seconds(row[columns["mean"]], unit, f"{where} {columns['mean']}")
    if distribution == "uniform":
        offset = _seconds(row[columns["offset"]], unit, f"{where} {columns['offset']}")
        return distribution, mean, offset
    if distribution == "exponential":
        return distribution, mean, 0.0
    raise ValueError(f"{where}的时长分布不支持：{distribution!r}")


def load_availability(source: Path, cache: bool) -> dict:
    """读取PM与Breakdown两表，返回按设备组与区域索引的参数。"""
    tables = read_tables(source, {"PM": None, "Breakdown": None}, cache)
    pm_by_group: dict[str, list[dict]] = {}
    for index, row in enumerate(tables["PM"]):
        if not row.get("TYPE NAME"):
            continue
        where = f"PM第{index + 2}行"
        if row["PM EVENT VALID FOR TYPE"] != "toolgroup":
            raise ValueError(f"{where}的适用范围不支持：{row['PM EVENT VALID FOR TYPE']!r}")
        kind = row["PM TYPE"].strip().lower()
        if kind not in ("time based (calendar)", "counter based"):
            raise ValueError(f"{where}的类型不支持：{row['PM TYPE']!r}")
        distribution, mean, offset = _duration(
            row,
            {
                "distribution": "TTR DISTRIBUTION",
                "mean": "MEAN",
                "offset": "OFFSET",
                "unit": "TTR UNITS",
            },
            where,
        )
        counter = kind == "counter based"
        unit = row["MTBPM UNITS"]
        if counter:
            if unit != "pieces":
                raise ValueError(f"{where}为计数制但单位是{unit!r}")
            period = int(row["MTBeforePM"])
        else:
            period = _seconds(row["MTBeforePM"], unit, f"{where} MTBeforePM")
        # 计数制行的`FOA`是片数、单位列留空；日历制行才带day等单位。
        if counter:
            if row["FOA UNITS"] not in (None, "", "pieces"):
                raise ValueError(f"{where}为计数制但FOA单位是{row['FOA UNITS']!r}")
            first = float(row["FOA"])
        else:
            first = _seconds(row["FOA"], row["FOA UNITS"], f"{where} FOA")
        if row["FIRST ONE AT DISTRIBUTION"] != "constant":
            raise ValueError(f"{where}的首次时刻分布不支持：{row['FIRST ONE AT DISTRIBUTION']!r}")
        pm_by_group.setdefault(row["TYPE NAME"], []).append(
            {
                "event": row["PM EVENT NAME"],
                "counter": counter,
                "period": period,
                "first": first,
                "distribution": distribution,
                "mean": mean,
                "offset": offset,
            }
        )
    breakdown_by_area = {}
    for index, row in enumerate(tables["Breakdown"]):
        if not row.get("TYPE NAME"):
            continue
        where = f"Breakdown第{index + 2}行"
        if row["DOWN EVENT VALID FOR TYPE"] != "area":
            raise ValueError(f"{where}的适用范围不支持：{row['DOWN EVENT VALID FOR TYPE']!r}")
        if row["TTF DISTRIBUTION"].strip().lower() != "exponential":
            raise ValueError(f"{where}的TTF分布不支持：{row['TTF DISTRIBUTION']!r}")
        if row["FIRST ONE AT DISTRIBUTION"].strip().lower() != "exponential":
            raise ValueError(f"{where}的首次时刻分布不支持：{row['FIRST ONE AT DISTRIBUTION']!r}")
        breakdown_by_area[row["TYPE NAME"]] = {
            "event": row["DOWN EVENT NAME"],
            "ttf": _seconds(row["MTTF"], row["MTTF UNITS"], f"{where} MTTF"),
            "ttr": _seconds(row["MTTR"], row["MTTR UNITS"], f"{where} MTTR"),
            "first": _seconds(row["FOA"], row["FOA UNITS"], f"{where} FOA"),
        }
    return {"pm_by_group": pm_by_group, "breakdown_by_area": breakdown_by_area}
