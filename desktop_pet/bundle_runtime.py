"""Select shipped worker runtimes without consulting the developer machine."""
from __future__ import annotations

import os
from pathlib import Path
import sys


def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)).resolve()


def bundled_python_executable() -> Path | None:
    if getattr(sys, "frozen", False):
        return resource_root() / "runtime" / "python" / "python.exe"
    return None


def bundled_node_executable() -> Path | None:
    if getattr(sys, "frozen", False):
        return resource_root() / "runtime" / "node" / "node.exe"
    return None


def worker_python_executable() -> Path:
    bundled = bundled_python_executable()
    if bundled is not None:
        return bundled
    executable = Path(sys.executable)
    return executable.with_name("python.exe") if executable.name.casefold() == "pythonw.exe" else executable


def frozen_workspace_root() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    profile = os.environ.get("JINGBAN_PROFILE_DIR", "")
    if profile:
        path = Path(profile)
        if not path.is_absolute():
            raise ValueError("Packaged profile must be absolute")
        return path.resolve() / "agent_workspace"
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return local / "AURA Desktop Pet" / "agent_workspace"
