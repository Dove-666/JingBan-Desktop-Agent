"""Read Node version metadata without starting a Windows process."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from functools import lru_cache
import os
from pathlib import Path
import re
import subprocess
import sys


def supported_node_version(version):
    """Match the DSH runner's fixed libuv ranges: ^24.20.0 || >=26.7.0."""
    return bool(version and ((version[0] == 24 and version >= (24, 20, 0))
                             or version >= (26, 7, 0)))


def node_version(executable):
    if executable is None:
        return None
    try:
        path = Path(executable).resolve(strict=True)
        stat = path.stat()
        if not path.is_file():
            return None
        return _version(str(path), stat.st_mtime_ns, stat.st_size)
    except (OSError, ValueError):
        return None


@lru_cache(maxsize=32)
def _version(path, modified_ns, size):
    del modified_ns, size
    if sys.platform == "win32":
        class FixedFileInfo(ctypes.Structure):
            _fields_ = [(name, wintypes.DWORD) for name in (
                "signature", "structure_version", "file_ms", "file_ls", "product_ms", "product_ls",
                "flags_mask", "flags", "os", "type", "subtype", "date_ms", "date_ls")]
        try:
            library = ctypes.WinDLL("version", use_last_error=True)
            library.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
            library.GetFileVersionInfoSizeW.restype = wintypes.DWORD
            library.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
            library.GetFileVersionInfoW.restype = wintypes.BOOL
            library.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                             ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
            library.VerQueryValueW.restype = wintypes.BOOL
            length = library.GetFileVersionInfoSizeW(path, None)
            if not 0 < length <= 1048576:
                return None
            data = ctypes.create_string_buffer(length)
            if not library.GetFileVersionInfoW(path, 0, length, data):
                return None
            pointer, count = ctypes.c_void_p(), wintypes.UINT()
            if not library.VerQueryValueW(data, "\\", ctypes.byref(pointer), ctypes.byref(count)):
                return None
            if count.value < ctypes.sizeof(FixedFileInfo):
                return None
            info = ctypes.cast(pointer, ctypes.POINTER(FixedFileInfo)).contents
            if info.signature != 0xFEEF04BD:
                return None
            return (info.file_ms >> 16, info.file_ms & 0xFFFF, info.file_ls >> 16)
        except (OSError, ValueError):
            return None
    environment = {name: value for name, value in os.environ.items()
                   if not re.search(r"KEY|TOKEN|SECRET|PASSWORD|NODE_OPTIONS|NODE_PATH", name, re.I)}
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True,
                                env=environment, timeout=5, check=True)
        match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)\s*", result.stdout)
        return tuple(map(int, match.groups())) if match else None
    except (OSError, subprocess.SubprocessError):
        return None


def node_supported(executable):
    return supported_node_version(node_version(executable))
