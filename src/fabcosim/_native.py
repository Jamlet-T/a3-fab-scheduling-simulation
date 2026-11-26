"""Windows中SUMO与Python SQLite的同名DLL兼容，仿真参数不受影响。"""

import ctypes
import sys
from importlib.metadata import PackageNotFoundError, distribution


def _sqlite_handle() -> int | None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel.GetModuleHandleW.restype = ctypes.c_void_p
    return kernel.GetModuleHandleW("sqlite3.dll")


def prepare_sqlite() -> ctypes.CDLL | None:
    """若安装了SUMO，在首次SQLite加载前选择它携带的兼容构建。"""
    if sys.platform != "win32" or _sqlite_handle():
        return None
    try:
        path = distribution("sumo-data").locate_file("sumo_data/bin/sqlite3.dll")
    except PackageNotFoundError:
        return None
    return ctypes.WinDLL(str(path)) if path.is_file() else None


def check_sumo_sqlite() -> None:
    """不卸载调用者已经使用的SQLite；在原生入口前明确拒绝不兼容构建。"""
    if sys.platform != "win32":
        return
    handle = _sqlite_handle()
    if not handle:
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    kernel.GetProcAddress.restype = ctypes.c_void_p
    required = ("sqlite3_column_database_name", "sqlite3_column_table_name", "sqlite3_column_origin_name")
    if any(not kernel.GetProcAddress(handle, name.encode()) for name in required):
        raise RuntimeError(
            "已加载的SQLite缺少SUMO需要的列元数据接口；请在独立Python进程中先import fabcosim，"
            "再import sqlite3，或使用项目CLI。不会卸载调用者已经使用的DLL。"
        )
