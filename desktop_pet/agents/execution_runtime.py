"""Host-only opt-in for the fixed MXC project runtime; missing means disabled.

Import does not read configuration, launch a process or import Qt/credentials.
The two-field local file cannot select executables, paths, tools or OS policy.
This module alone does not register a model tool or enable execution.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

_ROOT = Path(os.environ.get("LOCALAPPDATA") or str(Path.home()/"AppData/Local"))/"AURA Desktop Pet/project_execution"
_RELATIVE = "runtime.json"
_CONFIG = _ROOT/_RELATIVE
_LIMIT = 4096
_ISSUER = object()


class ExecutionRuntimeConfigurationError(ValueError):
    def __init__(self): super().__init__("HOST_EXECUTION_CONFIGURATION_INVALID")


def _members(pairs):
    value = {}
    for key, item in pairs:
        if key in value: raise ExecutionRuntimeConfigurationError()
        value[key] = item
    return value


def _fixed_configuration(value):
    if (type(value) is not dict or set(value) != {"engine", "enabled"}
            or value["engine"] != "mxc" or type(value["enabled"]) is not bool):
        raise ExecutionRuntimeConfigurationError()
    from agent_backend.nanobot.mxc_execution import MxcPolicy, CODEX_SHA256, CODEX_VERSION, PYTHON_SHA256
    return {"schema": 1, "engine": "mxc", "enabled": value["enabled"], "policy": asdict(MxcPolicy()),
        "runtime_pins": {"codex_sha256": CODEX_SHA256, "codex_version": CODEX_VERSION, "python_sha256": PYTHON_SHA256}}


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _plain(info):
    return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= _LIMIT
        and not getattr(info, "st_file_attributes", 0) & 0x400)


def load_execution_runtime():
    # The current native isolation gate has not passed for redistribution.
    # A packaged tester must never inherit a developer's opt-in or absolute
    # runtime pins. Ordinary registered file/document tools remain available.
    if getattr(sys, "frozen", False) or os.environ.get("JINGBAN_PACKAGED") == "1":
        return None
    try: before = _CONFIG.lstat()
    except FileNotFoundError: return None
    except OSError: raise ExecutionRuntimeConfigurationError() from None
    try:
        if not _plain(before) or _CONFIG.resolve() != _ROOT.resolve()/_RELATIVE:
            raise ExecutionRuntimeConfigurationError()
        with _CONFIG.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not _plain(opened) or _identity(opened) != _identity(before): raise ExecutionRuntimeConfigurationError()
            raw = stream.read(_LIMIT+1)
            final_fd = os.fstat(stream.fileno())
        after = _CONFIG.lstat()
        if (len(raw) > _LIMIT or not _plain(after) or _identity(final_fd) != _identity(before)
                or _identity(after) != _identity(before) or _CONFIG.resolve() != _ROOT.resolve()/_RELATIVE):
            raise ExecutionRuntimeConfigurationError()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_members)
        return _fixed_configuration(value)
    except Exception: raise ExecutionRuntimeConfigurationError() from None


class ExecutionRuntimeSnapshot:
    __slots__ = ("_issuer", "_encoded", "_identity")
    def __new__(cls, *args, **kwargs): raise TypeError("HOST_EXECUTION_SNAPSHOT_REQUIRED")
    def __setattr__(self, *args): raise AttributeError("HOST_EXECUTION_SNAPSHOT_IMMUTABLE")
    def __reduce__(self): raise TypeError("HOST_EXECUTION_SNAPSHOT_NOT_SERIALIZABLE")
    def __repr__(self): return "ExecutionRuntimeSnapshot([host configuration])"
    @property
    def identity(self): return self._identity
    @property
    def configuration(self): return json.loads(self._encoded) if self._encoded is not None else None


def valid_execution_runtime_snapshot(value):
    return type(value) is ExecutionRuntimeSnapshot and getattr(value, "_issuer", None) is _ISSUER


def capture_execution_runtime():
    config = load_execution_runtime()
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False) if config is not None else None
    value = object.__new__(ExecutionRuntimeSnapshot)
    for field, item in (("_issuer", _ISSUER), ("_encoded", encoded),
                        ("_identity", hashlib.sha256(encoded.encode("ascii")).hexdigest() if encoded is not None else "")):
        object.__setattr__(value, field, item)
    return value
