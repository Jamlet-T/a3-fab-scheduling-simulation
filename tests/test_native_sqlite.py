"""SQLite统计缓存不能破坏同进程的libsumo精确档。"""

import importlib.util
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or importlib.util.find_spec("libsumo") is None,
    reason="Windows/SUMO原生依赖专用",
)


def test_project_entry_allows_sqlite_then_native_sumo():
    code = """
import fabcosim
import sqlite3
import libsumo
assert sqlite3.connect(':memory:').execute('SELECT 1').fetchone() == (1,)
assert libsumo.getVersion()[0] > 0
print('both_backends_ok')
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "both_backends_ok" in result.stdout


def test_preloaded_incompatible_sqlite_is_explicitly_rejected():
    code = """
import sqlite3
import ctypes
kernel = ctypes.WinDLL('kernel32')
kernel.SetErrorMode(0x8003)
kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
kernel.GetModuleHandleW.restype = ctypes.c_void_p
kernel.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
kernel.GetProcAddress.restype = ctypes.c_void_p
handle = kernel.GetModuleHandleW('sqlite3.dll')
compatible = all(kernel.GetProcAddress(handle, name) for name in (
    b'sqlite3_column_database_name', b'sqlite3_column_table_name', b'sqlite3_column_origin_name'))
from fabcosim._native import check_sumo_sqlite
try:
    check_sumo_sqlite()
except RuntimeError as error:
    assert not compatible
    assert 'SQLite' in str(error)
    print('incompatible_backend_guarded')
else:
    assert compatible, '不兼容DLL被误放行'
    import libsumo
    assert libsumo.getVersion()[0] > 0
    print('already_compatible')
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "guarded" in result.stdout or "already_compatible" in result.stdout
