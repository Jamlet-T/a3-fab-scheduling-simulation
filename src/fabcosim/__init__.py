"""晶圆厂生产—OHT联合仿真。"""

from fabcosim._native import prepare_sqlite

# 同进程保留兼容DLL句柄；无SUMO依赖或非Windows时不加载原生库。
_sqlite_runtime = prepare_sqlite()

__version__ = "0.1.0"
