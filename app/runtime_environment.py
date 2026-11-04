"""采集实际 SQLite 原生库身份；不读取业务配置，也不改变运行时。"""
from contextlib import closing
import ctypes
import hashlib
import os
from pathlib import Path
import re
import sqlite3

import _sqlite3


def sqlite_wal_fixed(version: str) -> bool:
    """按官方主线和两个回补分支判断 WAL-reset 修复，未知格式拒绝。"""
    if not isinstance(version, str) or len(version) > 32 or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        return False
    parsed = tuple(int(part) for part in version.split("."))
    return (parsed >= (3, 51, 3) or (3, 44, 6) <= parsed < (3, 45, 0)
            or (3, 50, 7) <= parsed < (3, 51, 0))


def _file_digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def sqlite_runtime() -> dict[str, object]:
    """版本与 source id 来自已加载引擎；Windows DLL 通过进程模块句柄定位。"""
    with closing(sqlite3.connect(":memory:")) as connection:
        version, source_id = connection.execute("SELECT sqlite_version(), sqlite_source_id()").fetchone()
    library_digest = None
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        kernel.GetModuleHandleW.restype = ctypes.c_void_p
        kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
        kernel.GetModuleFileNameW.restype = ctypes.c_uint
        handle = kernel.GetModuleHandleW("sqlite3.dll")
        location = ctypes.create_unicode_buffer(32768)
        length = kernel.GetModuleFileNameW(handle, location, len(location)) if handle else 0
        if 0 < length < len(location):
            library_digest = _file_digest(Path(location.value))
    return {"version": version, "source_id": source_id, "threadsafety": sqlite3.threadsafety,
            "extension_sha256": _file_digest(Path(_sqlite3.__file__)), "library_sha256": library_digest}
