"""调度策略注册与解析：按配置名称选择规则，未知名称直接报错。

配置结构（整段可选，省略即用默认）：

    "policies": {
      "destination": "nearest_least_loaded",
      "production": {"job": "fifo", "group": "earliest_ready"},
      "transport": {"task": "earliest_ready", "vehicle": "nearest_idle"}
    }

`DEFAULTS`等于抽出策略层之前的已验证行为，用于保证既有运行结果可逐字节复现。
不做静默回退：未知配置键或未知规则名都会直接报错。
"""

from fabcosim.policies import destination, dispatch, production

DEFAULTS = {
    "destination": "nearest_least_loaded",
    "production.job": "fifo",
    "production.group": "earliest_ready",
    "transport.task": "earliest_ready",
    "transport.vehicle": "nearest_idle",
}

RULES = {
    "destination": {
        "nearest_least_loaded": destination.nearest_least_loaded,
        "nearest_only": destination.nearest_only,
        "least_loaded": destination.least_loaded,
    },
    "production.job": {
        "fifo": production.fifo,
        "implant_setup_fifo": production.implant_setup_fifo,
        "implant_setup_fifo_3groups": production.implant_setup_fifo_3groups,
        "source_baseline": production.source_baseline,
        "spt": production.spt,
        "lpt": production.lpt,
        "edd": production.edd,
        "cr": production.cr,
        "priority": production.priority,
        "priority_edd": production.priority_edd,
    },
    "production.group": {
        "earliest_ready": production.earliest_ready,
        "largest_first": production.largest_first,
        "earliest_due_group": production.earliest_due_group,
    },
    "transport.task": {
        "earliest_ready": dispatch.earliest_ready,
        "earliest_request": dispatch.earliest_request,
        "shortest_haul": dispatch.shortest_haul,
    },
    "transport.vehicle": {
        "nearest_idle": dispatch.choose_vehicle,
        "first_idle": dispatch.first_idle,
        "farthest_idle": dispatch.farthest_idle,
    },
}

SECTIONS = {"destination": ("destination",), "production": ("production.job", "production.group"),
            "transport": ("transport.task", "transport.vehicle")}


def resolve_policies(config: dict) -> dict:
    """解析配置中的策略名称，返回`{"names": ..., "rules": ...}`，均按点分键索引。"""
    requested = config.get("policies", {})
    if not isinstance(requested, dict):
        raise ValueError("policies必须是对象")
    flat = dict(DEFAULTS)
    for section, value in requested.items():
        keys = SECTIONS.get(section)
        if keys is None:
            raise ValueError(f"未知policies段：{section}；可用：{sorted(SECTIONS)}")
        if section == "destination":
            _assign(flat, keys[0], value, section)
            continue
        if not isinstance(value, dict):
            raise ValueError(f"policies.{section}必须是对象")
        for label, name in value.items():
            key = f"{section}.{label}"
            if key not in keys:
                raise ValueError(f"未知policies键：{key}；可用：{list(keys)}")
            _assign(flat, key, name, key)
    names, rules = {}, {}
    for key, name in flat.items():
        if name not in RULES[key]:
            raise ValueError(f"{key}不支持的规则：{name}；可用：{sorted(RULES[key])}")
        names[key], rules[key] = name, RULES[key][name]
    return {"names": names, "rules": rules}


def _assign(flat: dict, key: str, name: object, label: str) -> None:
    if not isinstance(name, str):
        raise ValueError(f"policies.{label}必须是规则名字符串")
    flat[key] = name
