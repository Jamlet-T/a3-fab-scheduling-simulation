"""两套已验收 analysis.json 的统一适配层。

仓库里 60 日与 730 日两套产物 schema 不同，这里各写一个适配器，
统一成前端可直接消费的视图模型。平台只接登记在 ``DATASETS`` 里的结果，
不做目录扫描——未通过分析流程的 run 不应出现在汇报页上。

所有统计只做等权汇总与极值透传，复算仅限原始数组直接求均值，
不引入新的显著性、插值或窗口口径。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parents[2]

RULES = ("fifo", "spt", "edd", "cr", "implant_setup_fifo_3groups", "c", "q2")
LABELS = {
    "fifo": "FIFO",
    "spt": "SPT",
    "edd": "EDD",
    "cr": "CR",
    "implant_setup_fifo_3groups": "三组换型",
    "c": "C",
    "q2": "修正版Q2",
}
COLORS = {
    "fifo": "#778899",
    "spt": "#4C78A8",
    "edd": "#8B6FB3",
    "cr": "#72A8B8",
    "implant_setup_fifo_3groups": "#D8A245",
    "c": "#2A8C7D",
    "q2": "#DA6245",
}

# 两套实验共用的主指标。scale 用于把原始比例换算成百分比展示。
COMMON_METRICS: list[dict[str, Any]] = [
    {"key": "completed", "label": "累计出厂", "short": "出厂", "unit": "lot", "precision": 0, "scale": 1},
    {
        "key": "main_throughput",
        "label": "主窗出厂率",
        "short": "出厂率",
        "unit": "lot/日",
        "precision": 2,
        "scale": 1,
    },
    {
        "key": "main_mean_wip",
        "label": "主窗平均在制",
        "short": "平均在制",
        "unit": "lot",
        "precision": 0,
        "scale": 1,
    },
    {
        "key": "main_cqt_rate",
        "label": "主窗CQT违例率",
        "short": "CQT违例率",
        "unit": "%",
        "precision": 2,
        "scale": 100,
    },
    {
        "key": "tail_wip_growth",
        "label": "末窗在制净增",
        "short": "末窗净增",
        "unit": "lot",
        "precision": 0,
        "scale": 1,
    },
]

DATASETS: dict[str, dict[str, Any]] = {
    "weekly-60d": {
        "order": 1,
        "path": Path("runs/20261117-weekly-cloud/final-analysis-02/analysis.json"),
        "adapter": "weekly",
    },
    "extended-730": {
        "order": 2,
        "path": Path("runs/20261118-extended-review/analysis-01/analysis.json"),
        "adapter": "extended",
    },
}


def _agg(stat: dict[str, Any], seeds: list[int]) -> dict[str, Any]:
    """把 {n,mean,minimum,maximum,values} 透传并绑定种子序号。"""
    values = [float(v) for v in stat.get("values", [])]
    return {
        "mean": float(stat.get("mean", sum(values) / len(values) if values else 0.0)),
        "min": float(stat.get("minimum", min(values) if values else 0.0)),
        "max": float(stat.get("maximum", max(values) if values else 0.0)),
        "values": [
            {"seed": seed, "value": value}
            for seed, value in zip(seeds, values, strict=False)
        ],
    }


def _rules_meta() -> list[dict[str, str]]:
    return [{"key": r, "label": LABELS[r], "color": COLORS[r]} for r in RULES]


def _normalize_weekly(raw: dict[str, Any]) -> dict[str, Any]:
    """60 日七方案三种子：aggregate 是 {rule: {metric: {...}}}。"""
    ctx = raw["context"]
    seeds = [int(s) for s in ctx["seeds"]]
    rules = [r for r in ctx["rules"]]
    # 指标键名映射到统一 key。
    key_map = {
        "completed": "completed",
        "main_throughput_lots_per_day": "main_throughput",
        "main_mean_wip": "main_mean_wip",
        "main_cqt_rate": "main_cqt_rate",
        "tail_wip_growth": "tail_wip_growth",
    }
    series: dict[str, dict[str, Any]] = {}
    for rule in rules:
        series[rule] = {}
        for raw_key, unified in key_map.items():
            stat = raw["aggregate"][rule][raw_key]
            series[rule][unified] = _agg(stat, seeds)

    cases = []
    for row in raw["cases"]:
        cases.append(
            {
                "rule": row["rule"],
                "seed": int(row["seed"]),
                "completed": row["completed"],
                "main_throughput": row["main_throughput_lots_per_day"],
                "main_mean_wip": row["main_mean_wip"],
                "main_cqt_rate": row["main_cqt_rate"],
                "tail_wip_growth": row["tail_wip_growth"],
                "main_cycle_days": row["main_cycle_days"],
                "main_late_fraction": row["main_late_fraction"],
                "end_overdue_wip": row["end_overdue_wip"],
                "end_wip": row["end_wip"],
            }
        )

    trends = [
        {
            "seed": int(row["seed"]),
            "rule": row["rule"],
            "day": float(row["day"]),
            "wip": row["wip"],
            "completed": row["completed"],
        }
        for row in raw["trends"]
    ]

    # 期末在制阶段：三组换型相关 phase 合并为 handling。
    handling = {"setup", "loading_tool", "unloading_tool"}
    phase_keys = (
        ("waiting_production_buffer", "输入准入等待"),
        ("waiting_tool", "已到站等待"),
        ("processing", "加工中"),
        ("handling", "换型/装卸"),
        ("production_transfer", "交接中"),
    )
    phases = []
    for rule in rules:
        for raw_phase, label in phase_keys:
            lots = sum(
                int(r["lots"])
                for r in raw["terminal_phases"]
                if r["rule"] == rule
                and (r["phase"] in handling if raw_phase == "handling" else r["phase"] == raw_phase)
            )
            phases.append({"rule": rule, "phase": raw_phase, "label": label, "lots": lots})

    pair = []
    by_seed = {int(c["seed"]): c for c in raw["cases"] if c["rule"] == "c"}
    q2_by_seed = {int(c["seed"]): c for c in raw["cases"] if c["rule"] == "q2"}
    for row in raw["c_q2_cqt"]:
        seed = int(row["seed"])
        c_case = by_seed.get(seed)
        q2_case = q2_by_seed.get(seed)
        if not c_case or not q2_case:
            continue
        pair.append(
            {
                "seed": seed,
                "common_checked": row["main"]["common_checked"],
                "c_violations": row["main"]["baseline_violations"],
                "q2_violations": row["main"]["candidate_violations"],
                "c_completed": c_case["completed"],
                "q2_completed": q2_case["completed"],
                "c_overdue": row["c_overdue_censored"],
                "q2_overdue": row["q2_overdue_censored"],
                "c_tail_wip_growth": c_case["tail_wip_growth"],
                "q2_tail_wip_growth": q2_case["tail_wip_growth"],
            }
        )

    return {
        "id": "weekly-60d",
        "order": 1,
        "kind": "60 日持续投片",
        "title": "七方案 × 三种子",
        "count": f"{len(rules)} 方案 × {len(seeds)} 种子 = {len(cases)} 例",
        "headline": "规则提高了出厂，但所有方案末期在制仍在增长",
        "summary": "同场景、同投片日历下比较七种派工规则。60 日尚不足以观察收敛，"
        "主窗(30,60]日、末窗(46,60]日只描述本轮有限观察。",
        "meta": {
            "source": "runs/20261117-weekly-cloud/final-analysis-02/analysis.json",
            "model_days": int(ctx["model_days"]),
            "main_window": ctx["primary_window_days"],
            "tail_window": ctx["tail_window_days"],
            "seeds": seeds,
            "rules": _rules_meta(),
            "notes": [
                ctx.get("uncertainty", ""),
                ctx.get("cqt_censoring", ""),
                ctx.get("cycle_censoring", ""),
            ],
        },
        "metrics": COMMON_METRICS,
        "series": series,
        "cases": cases,
        "trends": trends,
        "phases": phases,
        "phase_labels": {key: label for key, label in phase_keys},
        "pair": pair,
        "coverage": [
            {"rule": r, "n": len(seeds), "seeds": seeds} for r in rules
        ],
        "stats": [
            {"label": "实验例数", "value": str(len(cases)), "unit": "例"},
            {"label": "仿真时长", "value": str(int(ctx["model_days"])), "unit": "日"},
            {"label": "规则数", "value": str(len(rules)), "unit": "种"},
        ],
    }


def _normalize_extended(raw: dict[str, Any]) -> dict[str, Any]:
    """730 日部分结果：aggregate 是数组，样本数不一致。"""
    agg = {row["rule"]: row for row in raw["aggregate"]}
    rules = [r for r in RULES if r in agg]
    seeds_all = sorted({int(c["seed"]) for c in raw["passed_cases"]})

    key_map = {
        "completed": "completed",
        "main_throughput": "main_throughput",
        "main_mean_wip": "main_mean_wip",
        "main_cqt_rate": "main_cqt_rate",
        "last30_wip_growth": "tail_wip_growth",
    }
    series: dict[str, dict[str, Any]] = {}
    for rule in rules:
        row = agg[rule]
        seeds = [int(s) for s in row.get("seeds", [])]
        series[rule] = {}
        for raw_key, unified in key_map.items():
            series[rule][unified] = _agg(row["metrics"][raw_key], seeds)

    cases = []
    for row in raw["passed_cases"]:
        main = row["windows"]["main"]
        cases.append(
            {
                "rule": row["rule"],
                "seed": int(row["seed"]),
                "completed": row["completed"],
                "main_throughput": main["throughput_per_day"],
                "main_mean_wip": main["mean_wip_lots"],
                "main_cqt_rate": main["cqt_rate"],
                "tail_wip_growth": row["windows"]["last30d"]["wip_growth"],
                "main_wip_growth": main["wip_growth"],
                "end_wip": row["end_wip"],
            }
        )

    # 长期趋势与分块图沿用既有口径：只画已完整通过的第一种子，不混样本。
    lead_seed = 20261118
    has_lead = any(c["seed"] == lead_seed for c in cases)
    trend_seed = lead_seed if has_lead else max(seeds_all)
    trends = [
        {
            "seed": int(row["seed"]),
            "rule": row["rule"],
            "day": float(row["day"]),
            "wip": row["wip"],
        }
        for row in raw["trends"]
        if int(row["seed"]) == trend_seed
    ]
    blocks = [
        {
            "seed": int(row["seed"]),
            "rule": row["rule"],
            "start_day": float(row["start_day"]),
            "end_day": float(row["end_day"]),
            "throughput": row["throughput"],
            "mean_wip": row["mean_wip"],
        }
        for row in raw["blocks"]
        if int(row["seed"]) == trend_seed
    ]

    coverage = [
        {
            "rule": rule,
            "n": int(agg[rule].get("n", 0)),
            "seeds": [int(s) for s in agg[rule].get("seeds", [])],
        }
        for rule in rules
    ]

    # 设备组时间构成：main 窗口，跨已通过种子取等权均值，每规则保留等待 WIP 前 20 组。
    devices_by_group: dict[tuple[str, str], list[dict]] = {}
    for row in raw.get("devices", []):
        if row.get("window") != "main":
            continue
        devices_by_group.setdefault((row["rule"], row["group"]), []).append(row)

    devices: list[dict[str, Any]] = []
    for (rule, group), rows in devices_by_group.items():
        def _mean(key: str) -> float:
            return sum(float(r.get(key, 0.0)) for r in rows) / len(rows)

        devices.append(
            {
                "rule": rule,
                "group": group,
                "tools": max(int(r.get("tools", 0)) for r in rows),
                "waiting_mean_wip": _mean("waiting_mean_wip"),
                "queue_at_end": _mean("queue_at_end"),
                "processing": _mean("processing"),
                "setup": _mean("setup"),
                "loading": _mean("loading"),
                "unloading": _mean("unloading"),
                "down": _mean("down"),
                "idle": _mean("idle"),
                "seeds": len(rows),
            }
        )
    devices.sort(key=lambda d: (d["rule"], -d["waiting_mean_wip"]))
    devices = [
        row for rule in rules for row in [d for d in devices if d["rule"] == rule][:20]
    ]

    # 在制阶段构成：main 窗口，跨种子均值。
    phase_buckets: dict[tuple[str, str], list[float]] = {}
    for row in raw.get("phases", []):
        if row.get("window") != "main":
            continue
        phase_buckets.setdefault((row["rule"], row["phase"]), []).append(float(row["mean_wip"]))
    phase_mix = [
        {
            "rule": rule,
            "phase": phase,
            "mean_wip": sum(values) / len(values),
            "seeds": len(values),
        }
        for (rule, phase), values in phase_buckets.items()
    ]

    # 批优先级：main 窗口下完成数、逾期数与期末在制。
    priority_buckets: dict[tuple[str, str], list[dict]] = {}
    for row in raw.get("priority", []):
        if row.get("window") not in (None, "main"):
            continue
        priority_buckets.setdefault((row["rule"], row["group"]), []).append(row)
    priority = []
    for (rule, group), rows in priority_buckets.items():
        priority.append(
            {
                "rule": rule,
                "group": group,
                "completed": int(round(sum(int(r["completed"]) for r in rows) / len(rows))),
                "late": int(round(sum(int(r["late"]) for r in rows) / len(rows))),
                "end_wip": sum(int(r["end_wip"]) for r in rows) / len(rows),
                "overdue_wip": sum(int(r["overdue_wip"]) for r in rows) / len(rows),
                "seeds": len(rows),
            }
        )

    # 次年投片均值：取趋势种子的 FIFO 主窗投入，作为分块吞吐的对照线。
    release_rate = None
    for row in raw["passed_cases"]:
        if int(row["seed"]) == trend_seed and row["rule"] == "fifo":
            main = row["windows"]["main"]
            duration_days = main["window"]["duration_s"] / 86400.0
            release_rate = main["window_released_lots"] / duration_days
            break

    return {
        "id": "extended-730",
        "order": 2,
        "kind": "730 日长期运行",
        "title": "七方案 × 部分样本",
        "count": f"{len(cases)} 例已通过分析",
        "headline": "一年热身之后，流量与库存仍未收敛",
        "summary": "14 例全链条通过的 730 日实验。样本未齐，主图统一使用种子 "
        f"{trend_seed}，缺失点不补零、不混成三种子均值。",
        "meta": {
            "source": "runs/20261118-extended-review/analysis-01/analysis.json",
            "model_days": 730,
            "main_window": [365, 730],
            "tail_window": [700, 730],
            "seeds": seeds_all,
            "rules": _rules_meta()[: len(rules)],
            "notes": [
                "主窗(365,730]日，末窗(700,730]日。首 365 日仅作热身，不当作稳态。",
                "每个点是一例已通过的 730 日实验；缺失点表示未完成或未通过分析，不表示指标为 0。",
                "样本数不同，不能据此给出完整三种子排序，也不证明真实工厂固有产能。",
            ],
        },
        "metrics": COMMON_METRICS,
        "series": series,
        "cases": cases,
        "trends": trends,
        "blocks": blocks,
        "trend_seed": trend_seed,
        "release_rate": release_rate,
        "coverage": coverage,
        "devices": devices,
        "phase_mix": phase_mix,
        "priority": priority,
        "stats": [
            {"label": "已通过例数", "value": str(len(cases)), "unit": "例"},
            {"label": "仿真时长", "value": "730", "unit": "日"},
            {"label": "趋势种子", "value": str(trend_seed), "unit": ""},
        ],
    }


_ADAPTERS = {"weekly": _normalize_weekly, "extended": _normalize_extended}


def _resolve(path: Path, data_root: Path | None) -> Path:
    return path if path.is_absolute() else (data_root or REPO_ROOT) / path


def load_dataset(dataset_id: str, data_root: Path | None = None) -> dict[str, Any]:
    """加载并规范化单个数据集；文件缺失时抛出 FileNotFoundError。"""
    if dataset_id not in DATASETS:
        raise KeyError(dataset_id)
    spec = DATASETS[dataset_id]
    path = _resolve(spec["path"], data_root)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    raw = json.loads(path.read_text("utf-8"))
    view = _ADAPTERS[spec["adapter"]](raw)
    view["available"] = True
    return view


def build_index(data_root: Path | None = None) -> dict[str, Any]:
    """列出所有登记数据集及其可用性，供前端导航使用。"""
    items = []
    for dataset_id, spec in sorted(DATASETS.items(), key=lambda kv: kv[1]["order"]):
        path = _resolve(spec["path"], data_root)
        item: dict[str, Any] = {
            "id": dataset_id,
            "order": spec["order"],
            "available": path.is_file(),
            "path": str(path),
        }
        if path.is_file():
            stat = path.stat()
            item["size_mib"] = round(stat.st_size / 1048576, 2)
            item["modified"] = stat.st_mtime
        items.append(item)
    return {"datasets": items, "root": str(data_root or REPO_ROOT)}
