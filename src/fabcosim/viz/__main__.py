"""命令行入口：``python -m fabcosim.viz``（或安装后 ``fabcosim-viz``）。"""

from __future__ import annotations

import argparse
import threading
import webbrowser
from pathlib import Path

from .datasets import DATASETS, build_index
from .server import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fabcosim-viz",
        description="仿真结果可视化平台（本地只读服务）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8770, help="监听端口，默认 8770")
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="runs/ 所在目录，默认仓库根目录",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="启动后不自动打开浏览器",
    )
    args = parser.parse_args(argv)

    index = build_index(args.data_root)
    missing = [d for d in index["datasets"] if not d["available"]]
    print(f"数据根目录：{index['root']}")
    for item in index["datasets"]:
        spec = DATASETS[item["id"]]
        state = "就绪" if item["available"] else "缺失"
        print(f"  [{state}] {item['id']:<14} {spec['path']}")
    if missing:
        print("提示：缺失的数据集在首页会显示为不可用，其余页面不受影响。")

    httpd = serve(args.host, args.port, args.data_root)
    url = f"http://{args.host}:{args.port}/"
    print(f"\n平台已启动：{url}\n按 Ctrl+C 停止。")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
