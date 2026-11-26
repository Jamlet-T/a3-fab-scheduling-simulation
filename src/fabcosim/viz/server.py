"""本地静态服务：只读文件系统，对外暴露两个 JSON 接口。

- ``GET /api/datasets``          登记数据集清单与可用性
- ``GET /api/dataset/<id>``      规范化后的视图模型
- ``GET /``、``/assets/*``       演示前端

不提供写入、上传或任意文件读取；静态资源限定在 ``web/`` 目录内。
"""

from __future__ import annotations

import json
import mimetypes
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .datasets import DATASETS, build_index, load_dataset

WEB_DIR = Path(__file__).resolve().parent / "web"

_CACHE: dict[tuple[str, str], dict] = {}
_CACHE_LOCK = threading.Lock()


class PlatformHandler(BaseHTTPRequestHandler):
    data_root: Path | None = None
    server_version = "fabcosim-viz/0.1"

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定的方法名
        route = urlparse(self.path).path
        try:
            if route == "/api/datasets":
                self._send_json(build_index(self.data_root))
            elif route.startswith("/api/dataset/"):
                dataset_id = unquote(route[len("/api/dataset/") :]).strip("/")
                self._send_dataset(dataset_id)
            elif route in ("/", "/index.html"):
                self._send_file(WEB_DIR / "index.html")
            elif route in ("/report", "/report.html"):
                self._send_file(WEB_DIR / "report.html")
            elif route.startswith("/assets/"):
                self._send_asset(route[len("/assets/") :])
            else:
                self._send_error_text(HTTPStatus.NOT_FOUND, "未找到该路径")
        except BrokenPipeError:
            pass

    def _send_dataset(self, dataset_id: str) -> None:
        if dataset_id not in DATASETS:
            self._send_error_text(HTTPStatus.NOT_FOUND, f"未登记的数据集：{dataset_id}")
            return
        key = (dataset_id, str(self.data_root or ""))
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
        if cached is None:
            try:
                cached = load_dataset(dataset_id, self.data_root)
            except FileNotFoundError as exc:
                self._send_json(
                    {
                        "id": dataset_id,
                        "available": False,
                        "error": f"分析产物不存在：{exc}",
                    },
                    status=HTTPStatus.CONFLICT,
                )
                return
            with _CACHE_LOCK:
                _CACHE[key] = cached
        self._send_json(cached)

    def _send_asset(self, relative: str) -> None:
        target = (WEB_DIR / relative).resolve()
        if WEB_DIR.resolve() not in target.parents and target != WEB_DIR.resolve():
            self._send_error_text(HTTPStatus.FORBIDDEN, "禁止访问该路径")
            return
        if not target.is_file():
            self._send_error_text(HTTPStatus.NOT_FOUND, "资源不存在")
            return
        self._send_file(target)

    def _send_file(self, path: Path) -> None:
        if not path.is_file():
            self._send_error_text(HTTPStatus.NOT_FOUND, "文件不存在")
            return
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
            "application/javascript",
            "application/json",
        ):
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_text(self, status: int, message: str) -> None:
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        # 演示服务保持安静，只在启动时报告地址。
        return


def serve(host: str, port: int, data_root: Path | None) -> ThreadingHTTPServer:
    handler = type("BoundPlatformHandler", (PlatformHandler,), {"data_root": data_root})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd
