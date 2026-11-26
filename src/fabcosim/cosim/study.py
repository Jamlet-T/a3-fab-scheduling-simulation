"""读取实际完成的负载矩阵，输出可复现指标对比；不填补缺失案例。"""

import csv
import html
import json
from pathlib import Path

from fabcosim.cosim.event_log import existing_event_log


def write_comparison(study_path: Path, output: Path) -> list[dict]:
    study = json.loads(study_path.read_text("utf-8"))
    rows = []
    products = []
    periods = []
    for case in study["cases"]:
        directory = output / case.get("directory", case["name"])
        # 事件日志可以是events.json或压缩档events.json.gz；有两份时existing_event_log
        # 会报出歧义，而不是任取一份。
        if not all(
            (directory / name).is_file()
            for name in ("frames.json", "continuous-verification.json")
        ) or existing_event_log(directory) is None:
            raise ValueError(f"{case['name']}缺少完整事件/帧或独立核查，不生成正式对比")
        summary = json.loads((directory / "summary.json").read_text("utf-8"))
        config = json.loads((directory / "config.json").read_text("utf-8"))
        verified = json.loads((directory / "continuous-verification.json").read_text("utf-8"))
        if (
            verified["status"] != "passed"
            or verified["released_lots"] != summary["released_lots"]
            or verified["completed_lots"] != summary["completed_lots"]
            or verified["end_wip_lots"] != summary["wip_lots"]
        ):
            raise ValueError(f"{case['name']}独立核查未通过或与输出不一致")
        if summary["status"] != "window_completed":
            raise ValueError(f"{case['name']}没有完成观测，不生成正式对比")
        if (
            config["name"] != case["name"]
            or summary["vehicle_count"] != case["vehicles"]
            or config["release_stream"]["interval_s"] != case["interval_days"] * 86400
            or config["observation"]["warmup_s"] != study["warmup_days"] * 86400
            or config["observation"]["end_s"] != study["end_days"] * 86400
            or summary["collisions"]
            or summary["teleports"]
        ):
            raise ValueError(f"{case['name']}控制变量或原生安全结果不符合矩阵定义")
        metrics = summary["metrics"]
        traffic = metrics["traffic"]["fleet"]
        wait = metrics["transport_wait"]
        cycle = metrics["completed_cycle"]["mean_s"]
        exposure = case["vehicles"] * metrics["window"]["duration_s"]
        rows.append(
            {
                "case": case["name"],
                "interval_days": case["interval_days"],
                "vehicles": case["vehicles"],
                "released_lots": summary["released_lots"],
                "window_released_lots": metrics["window_released_lots"],
                "window_completed_lots": metrics["window_completed_lots"],
                "throughput_wafers_day": metrics["throughput_wafers_per_day"],
                "cycle_days": cycle / 86400 if cycle is not None else None,
                "mean_wip_lots": metrics["mean_wip_lots"],
                "end_wip_lots": metrics["end_wip_lots"],
                "pickup_wait_min": wait["uncensored"]["mean_s"] / 60
                if wait["uncensored"]["mean_s"] is not None
                else None,
                "wait_censored_count": wait["right_censored_count"],
                "wait_requested_count": wait["requested_count"],
                "pickup_wait_p95_min": wait["uncensored"]["p95_s"] / 60
                if wait["uncensored"]["p95_s"] is not None
                else None,
                "wait_observed_lower_bound_min": wait["all_observed_lower_bound"]["mean_s"] / 60
                if wait["all_observed_lower_bound"]["mean_s"] is not None
                else None,
                "empty_km": traffic["empty_distance_m"] / 1000,
                "loaded_km": traffic["loaded_distance_m"] / 1000,
                "idle_km": traffic["idle_distance_m"] / 1000,
                "empty_percent": traffic["empty_distance_fraction"] * 100
                if traffic["empty_distance_fraction"] is not None
                else None,
                "busy_percent": traffic["busy_fraction"] * 100,
                "stationary_wait_percent": traffic["blocked_fraction"] * 100,
                "zcu_wait_percent": traffic["zcu_wait_s"] / exposure * 100,
                "following_wait_percent": traffic["following_wait_s"] / exposure * 100,
                "unclassified_wait_percent": traffic["unclassified_wait_s"] / exposure * 100,
                "parked_percent": traffic["parked_s"] / exposure * 100,
                "minimum_gap_m": metrics["traffic"]["minimum_same_edge_gap_m"],
            }
        )
        for route, values in metrics["by_product"].items():
            cycle = values["completed_cycle"]
            products.append(
                {
                    "case": case["name"],
                    "product": route,
                    "completed_lots": values["window_completed_lots"],
                    "throughput_wafers_day": values["throughput_wafers_per_day"],
                    "cycle_days": cycle["mean_s"] / 86400 if cycle["mean_s"] is not None else None,
                    "end_wip_lots": values["end_wip_lots"],
                    "wip_age_days": values["end_wip_age"]["mean_s"] / 86400
                    if values["end_wip_age"]["mean_s"] is not None
                    else None,
                }
            )
        for name, values in summary.get("analysis_windows", {}).items():
            periods.append(
                {
                    "case": case["name"],
                    "period": name,
                    "start_day": values["window"]["start_exclusive_s"] / 86400,
                    "end_day": values["window"]["end_inclusive_s"] / 86400,
                    "completed_lots": values["window_completed_lots"],
                    "throughput_wafers_day": values["throughput_wafers_per_day"],
                    "mean_wip_lots": values["mean_wip_lots"],
                    "end_wip_lots": values["end_wip_lots"],
                    "wait_censored_count": values["transport_wait"]["right_censored_count"],
                }
            )
    output.mkdir(parents=True, exist_ok=True)
    (output / "comparison.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), "utf-8")
    with (output / "comparison.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    for name, values in (("by_product", products), ("by_period", periods)):
        (output / f"{name}.json").write_text(
            json.dumps(values, ensure_ascii=False, indent=2), "utf-8"
        )
        if values:
            with (output / f"{name}.csv").open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(values[0]))
                writer.writeheader()
                writer.writerows(values)
    columns = [
        ("case", "案例"),
        ("interval_days", "节拍/日"),
        ("vehicles", "OHT"),
        ("throughput_wafers_day", "出厂wafer/日"),
        ("cycle_days", "完工样本周期/日"),
        ("mean_wip_lots", "平均WIP/lot"),
        ("end_wip_lots", "末WIP/lot"),
        ("pickup_wait_min", "已取货等待/分"),
        ("wait_censored_count", "未取货删失请求"),
        ("empty_km", "空驶/km"),
        ("loaded_km", "载货/km"),
        ("idle_km", "闲车运动/km"),
        ("empty_percent", "空驶距离占比/%"),
        ("busy_percent", "车队作业时间/%"),
        ("stationary_wait_percent", "静止等待/%"),
    ]

    def text(value):
        return "—" if value is None else f"{value:.3f}" if isinstance(value, float) else str(value)

    note = (
        f"主窗口({study['warmup_days']},{study['end_days']}]仿真日；"
        f"各产品{config['release_stream']['bootstrap_lots_per_route']}个初始"
        f"{config['release_stream']['wafer_count']}-wafer lot，"
        "Product 1/2交替持续投片。共同种子、完整521/529步、真实路网与规则基线。"
        "完工周期只统计窗口内出厂样本，未完成WIP年龄与运输等待删失另见各案例summary。"
        "静止等待是非业务、非待命停等代理，不能单凭它证明拥堵成因。"
        "这是有限观察的单种子结果，尚不是稳态、统计显著性或原厂吞吐量校准结论。"
    )
    header = "| " + " | ".join(label for _, label in columns) + " |"
    markdown = [
        "# 持续投片与物流负载对比",
        "",
        note,
        "",
        header,
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        markdown.append("| " + " | ".join(text(row[key]) for key, _ in columns) + " |")
    extra_tables = []
    for title, values, fields in (
        (
            "运输等待与静止等待分解",
            rows,
            [
                ("case", "案例"),
                ("wait_requested_count", "提出请求"),
                ("wait_censored_count", "右删失请求"),
                ("pickup_wait_p95_min", "已取货等待P95/分钟"),
                ("wait_observed_lower_bound_min", "全请求已观察等待下界均值/分钟"),
                ("zcu_wait_percent", "区域等待/%"),
                ("following_wait_percent", "跟车等待/%"),
                ("unclassified_wait_percent", "未分类静止等待/%"),
                ("parked_percent", "停靠待命/%"),
            ],
        ),
        (
            "分产品统计",
            products,
            [
                ("case", "案例"),
                ("product", "产品"),
                ("completed_lots", "窗口出厂lot"),
                ("throughput_wafers_day", "出厂wafer/日"),
                ("cycle_days", "完工样本周期/日"),
                ("end_wip_lots", "末WIP/lot"),
                ("wip_age_days", "未完成WIP平均年龄/日"),
            ],
        ),
        (
            "分段窗口",
            periods,
            [
                ("case", "案例"),
                ("period", "窗口"),
                ("start_day", "起点/日（不含）"),
                ("end_day", "终点/日（含）"),
                ("completed_lots", "出厂lot"),
                ("throughput_wafers_day", "出厂wafer/日"),
                ("mean_wip_lots", "平均WIP/lot"),
                ("end_wip_lots", "末WIP/lot"),
                ("wait_censored_count", "未取货删失请求"),
            ],
        ),
    ):
        if not values:
            continue
        markdown += ["", f"## {title}", ""]
        markdown.append("| " + " | ".join(label for _, label in fields) + " |")
        markdown.append("| " + " | ".join("---" for _ in fields) + " |")
        head = "<tr>" + "".join(f"<th>{html.escape(label)}</th>" for _, label in fields) + "</tr>"
        body = ""
        for row in values:
            markdown.append("| " + " | ".join(text(row[key]) for key, _ in fields) + " |")
            body += (
                "<tr>"
                + "".join(f"<td>{html.escape(text(row[key]))}</td>" for key, _ in fields)
                + "</tr>"
            )
        extra_tables.append(
            f"<section><h2>{title}</h2><div class='scroll'><table>"
            f"<thead>{head}</thead><tbody>{body}</tbody></table></div></section>"
        )
    markdown += [
        "",
        "未完成WIP年龄是截至窗口终点的已观察年龄，不是完工周期。"
        "辅助截面没有车辆距离积分，不能当作零运动。"
        "空驶距离已包含闲车运动，闲车距离是其中的子集，不能重复相加；闲车运动还包括前往待命点。"
        "已取货等待均值/P95不包含右删失请求；全请求下界保留未取货请求截至终点的已观察等待。"
        "这些等待统计以窗口内提出的请求为队列，跨预热边界的旧请求不能据此当作零等待。"
        "区域、跟车和未分类等待占比均以车队总观察时长为分母；停靠待命单列。",
        "",
    ]
    (output / "report.md").write_text("\n".join(markdown), "utf-8")
    table = (
        "<table><thead><tr>"
        + "".join(f"<th>{html.escape(label)}</th>" for _, label in columns)
        + "</tr></thead><tbody>"
    )
    for row in rows:
        table += (
            "<tr>"
            + "".join(f"<td>{html.escape(text(row[key]))}</td>" for key, _ in columns)
            + "</tr>"
        )
    table += "</tbody></table>"
    charts = ""
    for key, label in (
        ("throughput_wafers_day", "窗口出厂吞吐量（wafer/日）"),
        ("end_wip_lots", "期末WIP（lot）"),
        ("idle_km", "闲车运动距离（km）"),
    ):
        maximum = max(row[key] for row in rows) or 1
        bars = ""
        for row in rows:
            bars += f"<div class='barrow'><span>{html.escape(row['case'])}</span><div class='track'><i style='width:{row[key] / maximum * 100:.3f}%'></i></div><b>{text(row[key])}</b></div>"
        charts += f"<section><h2>{label}</h2>{bars}</section>"
    page = "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>物流负载实验</title><style>body{font:14px system-ui;color:#20374b;background:#f3f6fa;margin:30px}main{max-width:1300px;margin:auto}section,.scroll{background:white;padding:20px;border-radius:10px;margin:20px 0}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;white-space:nowrap}th,td{padding:10px;border-bottom:1px solid #ddd}p{line-height:1.8}h2{font-size:18px}.barrow{display:flex;gap:12px;align-items:center;margin:13px 0}.barrow span{width:170px}.barrow b{width:90px}.track{flex:1;background:#edf2f7;height:16px}.track i{display:block;height:100%;background:#397ac6}@media(max-width:600px){body{margin:12px}.barrow span{width:125px;font-size:12px}.barrow b{width:65px}}</style><main><h1>持续投片与物流负载实验</h1>"
    page += f"<p>{html.escape(note)}</p><div class='scroll'>{table}</div>{charts}"
    page += "".join(extra_tables)
    representative_case = next(
        case for case in study["cases"] if case["name"] == study["representative_case"]
    )
    representative = representative_case.get("directory", representative_case["name"])
    page += f"<p><a href='{html.escape(representative)}/replay.html'>代表场景：{representative_case['vehicles']}车连续回放</a> · <a href='comparison.csv'>指标CSV</a> · <a href='report.md'>文字记录</a></p></main></html>"
    (output / "report.html").write_text(page, "utf-8")
    return rows
