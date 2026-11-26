"""联合仿真命令入口。"""

import argparse
import json
from pathlib import Path

from fabcosim import __version__


def main() -> None:
    parser = argparse.ArgumentParser(description="晶圆厂生产—OHT联合仿真")
    parser.add_argument("--version", action="version", version=f"fabcosim {__version__}")
    commands = parser.add_subparsers(dest="command")
    demo = commands.add_parser("demo", help="运行两设施、一辆OHT、一个lot的小场景")
    root = Path(__file__).resolve().parents[2]
    demo.add_argument("--config", type=Path, default=root / "configs/two_tools.json")
    demo.add_argument("--output", type=Path, help="输出到新的目录，不覆盖已有结果")
    multi = commands.add_parser("multi", help="运行多车规则调度、区域通行与端口预留")
    multi.add_argument("--config", type=Path, default=root / "configs/multi_merge.json")
    multi.add_argument("--output", type=Path, help="输出到新的目录")
    full = commands.add_parser("full", help="运行首道到末道的完整生产路线与真实运输")
    full.add_argument("--config", type=Path, default=root / "configs/smat_full_product1.json")
    full.add_argument("--output", type=Path, help="输出到新的目录")
    full.add_argument("--resume", type=Path, help="从完整状态快照恢复，仅纯生产，输出必须为新目录")
    full.add_argument("--checkpoint-interval-s", type=float, help="按仿真秒定期保存完整状态")
    full.add_argument("--stop-after-checkpoint", action="store_true", help="首份完整快照写入后正常暂停")
    full.add_argument(
        "--lean-output", action="store_true",
        help="关闭回放及车辆路线几何，紧凑保存完整业务事件；不改变生产和交通模型",
    )
    full.add_argument(
        "--compress-events", action="store_true",
        help="完整事件数组改用gzip压缩（隐含--lean-output），输出events.json.gz",
    )
    compare = commands.add_parser("compare", help="汇总实际完成的持续投片负载矩阵")
    compare.add_argument("--study", type=Path, default=root / "configs/continuous_load_study.json")
    compare.add_argument(
        "--output", type=Path, required=True, help="包含各案例运行目录的矩阵根目录"
    )
    args = parser.parse_args()
    if args.command not in ("demo", "multi", "full", "compare"):
        parser.print_help()
        return
    if args.command == "compare":
        from fabcosim.cosim.study import write_comparison

        rows = write_comparison(args.study, args.output)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        print(f"实验对比：{(args.output / 'report.html').resolve()}")
        return
    from fabcosim.cosim.demo import default_output, load_config, run_demo

    if args.command == "full":
        from fabcosim.cosim.full import run_full

        if args.resume:
            from fabcosim.cosim.checkpoint_runtime import resume_run

            if args.lean_output or args.compress_events or args.checkpoint_interval_s or args.stop_after_checkpoint:
                parser.error("resume使用快照中原配置，不能同时覆盖日志/存档参数")
            output = (args.output or default_output(root, "production-resumed")).resolve()
            summary = resume_run(args.resume, output)
            print(json.dumps({k: v for k, v in summary.items() if k != "lot_results"}, ensure_ascii=False, indent=2))
            print(f"运行输出：{output}")
            return
        config = json.loads(args.config.read_text(encoding="utf-8"))
        if args.checkpoint_interval_s is not None:
            config["checkpoint"] = {"interval_s": args.checkpoint_interval_s,
                                    "stop_after_save": args.stop_after_checkpoint}
        elif args.stop_after_checkpoint:
            parser.error("stop-after-checkpoint需要checkpoint-interval-s")
        if args.lean_output or args.compress_events:
            config["replay"] = {"enabled": False}
            config.setdefault("performance", {}).update(
                route_event_geometry=False, compact_json=True, stream_events=True
            )
        if args.compress_events:
            config["performance"]["event_compression"] = "gzip"
        output = (args.output or default_output(root, config["name"])).resolve()
        summary = run_full(config, output)
        print(
            json.dumps(
                {k: v for k, v in summary.items() if k != "lot_results"},
                ensure_ascii=False,
                indent=2,
            )
        )
        print(f"运行输出：{output}")
        if (output / "replay.html").exists():
            print(f"轨迹回放：{output / 'replay.html'}")
        return

    if args.command == "multi":
        from fabcosim.cosim.multi import run_multi

        config = json.loads(args.config.read_text(encoding="utf-8"))
        output = (args.output or default_output(root, config["name"])).resolve()
        summary = run_multi(config, output)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"运行输出：{output}")
        print(f"轨迹回放：{output / 'replay.html'}")
        return
    config = load_config(args.config)
    name = "smat-network" if config.get("network", {}).get("type") == "smat2022" else "two-tools"
    output = (args.output or default_output(root, name)).resolve()
    summary = run_demo(config, output)
    display = {key: value for key, value in summary.items() if key not in ("route", "network")}
    display["route"] = {
        key: value if not isinstance(value, list) else len(value)
        for key, value in summary["route"].items()
    }
    if "network" in summary:
        display["network"] = {
            key: summary["network"][key]
            for key in (
                "node_count",
                "edge_count",
                "equipment_count",
                "port_count",
                "curve_chord_count",
            )
        }
    print(json.dumps(display, ensure_ascii=False, indent=2))
    print(f"运行输出：{output}")
    print(f"轨迹回放：{output / 'replay.html'}")


if __name__ == "__main__":
    main()
